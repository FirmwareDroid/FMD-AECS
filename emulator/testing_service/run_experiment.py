"""
Experiment script that will run the full experiment pipeline for testing Android apps on a connected adb device.
"""

import subprocess
import sys
import os
import logging
import argparse
import datetime
import json
import shutil
import atexit
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTALL_APPIUM = os.path.join(BASE_DIR, 'appium', 'install_appium.py')
RUN_PCAPDROID = os.path.join(BASE_DIR, 'appium', 'run_pcapdroid_on_all.py')
DROIDRUN_AGENT = os.path.join(BASE_DIR, 'bots', 'droidrun_agent_cli.py')
ACVTOOL = os.path.join(BASE_DIR, 'coverage', 'acvtool_wrapper.py')
LOGCAT_COLLECTOR = os.path.join(BASE_DIR, 'coverage', 'collect_logcat.py')
INSTALL_APPS = os.path.join(BASE_DIR, 'install_apps.py')
START_APPS_BASIC = os.path.join(BASE_DIR, 'start_apps.py')
LAUNCHER_TEST = os.path.join(BASE_DIR, 'launcher_test.py')
CONNECTIVITY_TEST = os.path.join(BASE_DIR, 'connectivity_test.py')

import glob
try:
    import crash_watcher
except Exception:
    crash_watcher = None
    
OUT_DIR = os.path.join(BASE_DIR, 'out')

def parse_args():
    parser = argparse.ArgumentParser(description='Run experiment pipeline')
    parser.add_argument('--mode', choices=['basic', 'droidrun', 'single', 'monkey'], default='single',
                        help='Test mode: "basic" runs the START_APPS_BASIC start/stop test;'
                             '"droidrun" runs the Droidrun agent (default: basic);'
                             '"single" runs a simple test cycle (for development/debugging)')
    parser.add_argument('--test-only-one', action='store_true', help='If set, only the first app in the list will be tested')
    parser.add_argument('--skip-setup', action='store_true', help='Skip device setup steps (installing Appium/PCAPdroid/Droidrun)')
    # pcap_http_port=args.pcap_http_port, socks5_address=args.socks5_address
    parser.add_argument('--pcap-http-port', type=int, help='Port to use for pcap http server')
    parser.add_argument('--socks5-address', type=str, help='The SOCKS5 proxy address')
    return parser.parse_args()

def run_script(script_path, args=None, description=None):
    cmd = [sys.executable, script_path]
    if args:
        cmd.extend(args)
    logging.info(f"Running: {description or script_path}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    logging.info(result.stdout)
    if result.stderr:
        logging.error(result.stderr)
    if result.returncode != 0:
        logging.error(f"Failed: {description or script_path} (exit code {result.returncode})")
        sys.exit(result.returncode)

def run_script_capture(script_path, args=None, description=None):
    """Run a python script and capture detailed result without exiting the process.

    Returns a dict with keys: script, args, description, returncode, stdout, stderr, start_time, end_time, duration
    """
    cmd = [sys.executable, script_path]
    if args:
        cmd.extend(args)
    start = datetime.datetime.now(datetime.timezone.utc).isoformat() + 'Z'
    t0 = datetime.datetime.now(datetime.timezone.utc)
    logging.info(f"Running (capture): {description or script_path}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    t1 = datetime.datetime.now(datetime.timezone.utc)
    end = t1.isoformat() + 'Z'
    duration = (t1 - t0).total_seconds()
    res = {
        'script': script_path,
        'args': args or [],
        'description': description or script_path,
        'returncode': proc.returncode,
        'stdout': proc.stdout,
        'stderr': proc.stderr,
        'start_time': start,
        'end_time': end,
        'duration_seconds': duration,
    }
    if proc.stdout:
        logging.info(proc.stdout)
    if proc.stderr:
        logging.error(proc.stderr)
    return res

def run_command(cmd, description=None):
    logging.info(f"Running: {description or cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    logging.info(result.stdout)
    if result.stderr:
        logging.error(result.stderr)
    if result.returncode != 0:
        logging.error(f"Failed: {description or cmd} (exit code {result.returncode})")
        sys.exit(result.returncode)

def start_appium_server():
    logging.info("Starting Appium server...")
    # Start Appium server in the background if available on PATH
    if not shutil.which('appium'):
        logging.warning('appium binary not found in PATH; skipping Appium start')
        return None
    try:
        proc = subprocess.Popen(["appium"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # give it a short moment to start
        time.sleep(3)
        logging.info('Appium server started (pid=%s)', getattr(proc, 'pid', None))
        return proc
    except Exception as e:
        logging.error('Failed to start Appium server: %s', e)
        return None


def stop_appium_server(proc, timeout=5):
    """Terminate the Appium server process started by this script.

    Attempts a graceful terminate, waits `timeout` seconds, then kills if still alive.
    """
    if not proc:
        return
    try:
        logging.info('Stopping Appium server (pid=%s)...', getattr(proc, 'pid', None))
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
            logging.info('Appium server exited')
        except subprocess.TimeoutExpired:
            logging.warning('Appium did not exit within %ss, killing...', timeout)
            proc.kill()
            proc.wait()
            logging.info('Appium server killed')
    except Exception as e:
        logging.error('Error stopping Appium server: %s', e)


def setup_devices(mode='basic', pcap_http_port=54320, socks5_address="127.0.0.1"):
    logging.info(f"Setting up devices...")
    # Install Appium driver
    run_script_capture(INSTALL_APPIUM, args=["--all"], description="Install Appium driver on all devices")
    # Configure PCAPdroid on all devices

    cmd_clear_pcapdroid = "adb shell pm clear com.emanuelef.remote_capture"
    run_script_capture(cmd_clear_pcapdroid, description="Clear PCAPdroid data on all devices before configuration")
    run_script_capture(RUN_PCAPDROID, args=["--http-port", str(pcap_http_port), "--socks5-address", socks5_address],
                       description="Configure PCAPdroid on all devices")
    if mode == 'droidrun':
        # Install Droidrun on all devices
        run_command("droidrun setup --latest", description="Install Droidrun on all devices")


def get_testing_apps():
    app_package_names = ["com.android.settings"]
    return app_package_names

def get_installed_packages():
    """Get a list of installed package names on the connected device(s) using adb."""
    result = subprocess.run(["adb", "shell", "pm", "list", "packages"], capture_output=True, text=True)
    if result.returncode != 0:
        logging.error(f"Failed to get installed packages: {result.stderr}")
        return []
    lines = result.stdout.strip().splitlines()
    packages = [line.replace("package:", "").strip() for line in lines if line.startswith("package:")]
    return packages


def _adb_base_cmd():
    """
    Build the base adb command list; include -s <serial> if ANDROID_SERIAL or ADB_SERIAL
    environment variable is set so this works with multiple devices.
    """
    serial = os.environ.get('ANDROID_SERIAL') or os.environ.get('ADB_SERIAL')
    cmd = ['adb']
    if serial:
        cmd.extend(['-s', serial])
    return cmd


def wait_for_bootanim_stop(max_wait_seconds=300, sleep_seconds=30):
    """
    Poll 'adb shell getprop init.svc.bootanim' and wait while it is 'running'.

    - Sleeps sleep_seconds between checks.
    - Stops waiting after max_wait_seconds and returns False (timed out).
    - Returns True if the property is observed not 'running' before timeout.
    """
    adb_cmd_base = _adb_base_cmd()
    max_tries = max(1, int(max_wait_seconds // sleep_seconds))
    tries = 0

    logging.info("Waiting for init.svc.bootanim to stop (max %s seconds, interval %s seconds)...",
                 max_wait_seconds, sleep_seconds)

    while True:
        try:
            proc = subprocess.run(adb_cmd_base + ['shell', 'getprop', 'init.svc.bootanim'],
                                  capture_output=True, text=True, timeout=10)
            value = (proc.stdout or "").strip().strip('\r\n')
        except Exception as e:
            logging.warning("Failed to query adb for bootanim state: %s", e)
            value = ""

        if value.lower() != 'running':
            logging.info("init.svc.bootanim reported as %r -> proceeding", value)
            is_running = False
            break

        tries += 1
        if tries >= max_tries:
            logging.warning("init.svc.bootanim remained 'running' after %s seconds (max wait).", max_wait_seconds)
            is_running = True
            break

        logging.info("init.svc.bootanim is 'running' (try %d/%d). Sleeping %s seconds...", tries, max_tries, sleep_seconds)
        time.sleep(sleep_seconds)
    return is_running


def execute_app_with_coverage(package, mode):
    logging.info(f"Executing appium with package: {package}, mode: {mode}")
    run_script_capture(ACVTOOL, args=["flush", package, "--wd", OUT_DIR], description="Run ACVTool to flush coverage measurement.")
    run_script_capture(ACVTOOL, args=["activate", package, "--wd", OUT_DIR], description="Run ACVTool to activate coverage measurement.")
    if mode == 'droidrun':
        run_script_capture(DROIDRUN_AGENT, args=["run"], description="Run Droidrun agent to test apps.")
    elif mode == 'monkey':
        run_script_capture(START_APPS_BASIC, args=["-m", "1", "--monkey-seed", "1337", "--monkey-randomize-throttle", "-p", package])
    else:
        run_script_capture(START_APPS_BASIC, args=[package], description=f"Run basic start/stop test for {package}")
    run_script_capture(ACVTOOL, args=["snap", package, "--wd", OUT_DIR], description="Run ACVTool to get coverage measurement")
    run_script_capture(ACVTOOL, args=["cover-pickles", package, "--wd", OUT_DIR],
                       description="Run ACVTool to deserialize coverage measurement")
    run_script_capture(ACVTOOL, args=["report", package, "--wd", OUT_DIR],
                       description="Run ACVTool to generate html coverage report")


def start_experiment(mode='single', test_only_one=False):
    app_package_names = get_installed_packages()
    if not app_package_names:
        logging.info('No packages found to test')
        return

    if test_only_one:
        app_package_names = get_testing_apps()
        first_pkg = app_package_names[0]
        logging.info('Test-only-one enabled; testing only first package: %s', first_pkg)
        run_script_capture(INSTALL_APPS, args=["--package", first_pkg], description=f"Install app {first_pkg} on devices.")
        execute_app_with_coverage(first_pkg, mode)
    else:
        run_script_capture(INSTALL_APPS, args=["-a"], description=f"Install all apps on devices.")
        for package in app_package_names:
            logging.info(f"Starting {package}")
            #TODO Filter apps that have an Activity
            execute_app_with_coverage(package, mode)

    run_script_capture(LOGCAT_COLLECTOR, args=["--full-dump"], description="Collect all logcat logs")

def run_launcher_test(results_dir):
    logging.info('Running launcher preflight test before any device setup')

    preflight_name = 'preflight_launcher'
    res = run_script_capture(LAUNCHER_TEST, args=['--output-dir', results_dir, '--name', preflight_name], description='Run launcher preflight test')

    # find the newest JSON file produced (preflight_*.json)
    json_matches = glob.glob(os.path.join(results_dir, f"{preflight_name}_*.json"))
    if json_matches:
        json_path = max(json_matches, key=os.path.getmtime)
        logging.info('Found launcher test JSON output: %s', json_path)
        try:
            with open(json_path, 'r', encoding='utf-8') as jf:
                jdata = json.load(jf)
        except Exception as e:
            logging.error('Failed to read launcher test JSON: %s', e)
            jdata = None
    else:
        logging.error('No launcher test JSON output found in %s', results_dir)
        jdata = None

    if jdata and isinstance(jdata, dict):
        preflight_ok = bool(jdata.get('success'))
    else:
        # fallback: use returncode of the launcher_test process
        preflight_ok = (res.get('returncode', 1) == 0)
    return preflight_ok

def run_connectivity_test(results_dir):
    logging.info('Running connectivity test before any device setup')
    # Run the connectivity test script and capture its output; write a small JSON report
    args = ['--retries', '3', '--timeout', '10']
    res = run_script_capture(CONNECTIVITY_TEST, args=args, description='Run connectivity test')

    # Save the captured result for debugging/recording
    os.makedirs(results_dir, exist_ok=True)
    out_file = os.path.join(results_dir, 'connectivity_results.json')
    try:
        with open(out_file, 'w', encoding='utf-8') as of:
            json.dump(res, of, indent=2)
        logging.info('Wrote connectivity test results to %s', out_file)
    except Exception:
        logging.exception('Failed to write connectivity test results')

    # Return True if the connectivity test reported success (exit code 0)
    return (res.get('returncode', 1) == 0)


def main():
    args = parse_args()

    # Start crash watcher in background to dismiss random ANR/crash dialogs during the pipeline
    if crash_watcher:
        try:
            crash_watcher.start_crash_watcher(device=None, interval=5.0)
            atexit.register(crash_watcher.stop_crash_watcher)
            logging.info('Started crash watcher (background)')
        except Exception:
            logging.exception('Failed to start crash watcher')

    results_dir = os.path.join(BASE_DIR, 'out', 'launcher_test_results')
    os.makedirs(results_dir, exist_ok=True)



    # Wait for boot animation (init.svc.bootanim) to stop (max 5 minutes) before running the launcher test
    preflight_ok = False
    try:
        is_running = wait_for_bootanim_stop(max_wait_seconds=600, sleep_seconds=10)
        if not is_running:
            preflight_ok = run_launcher_test(results_dir)
    except Exception:
        logging.exception('Error while waiting for boot animation to stop;')

    # First: connectivity test
    try:
        if not run_connectivity_test(results_dir):
            logging.error('Connectivity test failed; aborting experiment pipeline')
            out_file = os.path.join(results_dir, 'connectivity_results.json')
            if os.path.exists(out_file):
                with open(out_file, 'r', encoding='utf-8') as cf:
                    logging.error('Connectivity details:\n%s', cf.read())
            sys.exit(2)
    except Exception:
        logging.exception('Error while running connectivity test; aborting')
        sys.exit(2)

    disable_anr_message = "adb shell settings put global show_annoying_receivers_in_background 0"
    run_command(disable_anr_message, description='Disable show_annoying_receivers_in_background: This suppresses the "Application Not Responding" dialogs')
    disable_anr_message = "adb shell settings put global anr_show_background 0"
    run_command(disable_anr_message, description='Disable anr_show_background: This suppresses the "Application Not Responding" dialogs')
    disable_anr_message = "adb shell settings put global hide_error_dialogs 1"
    run_command(disable_anr_message, description='Disable hide_error_dialogs: This suppresses the "Application Not Responding" dialogs')

    if not preflight_ok:
        logging.error('Launcher preflight test failed; aborting experiment pipeline')
        out_file = os.path.join(results_dir, 'launcher_test_results.json')
        with open(out_file, 'w', encoding='utf-8') as of:
            json.dump({
                'success': False,
                'error': 'Launcher preflight test failed or did not produce valid output',
            }, of, indent=2)
        sys.exit(2)

    logging.info('Launcher preflight succeeded; continuing with Appium start and device setup')

    # Start a local Appium server for the experiment and ensure it is stopped at the end
    appium_proc = start_appium_server()
    if appium_proc:
        # register atexit cleanup as a safety net
        atexit.register(stop_appium_server, appium_proc)

    try:
        if args.skip_setup:
            logging.info('Skipping device setup as requested (--skip-setup)')
        else:
            setup_devices(mode=args.mode, pcap_http_port=args.pcap_http_port, socks5_address=args.socks5_address)
        start_experiment(mode=args.mode, test_only_one=getattr(args, 'test-only-one', False) or getattr(args, 'test_only_one', False) or args.test_only_one)
    except KeyboardInterrupt:
        logging.info('Interrupted by user')
    finally:
        # Ensure Appium is stopped before exit
        stop_appium_server(appium_proc)


if __name__ == "__main__":
    main()

## Live Network Traffic Debugging
# If you want to inspect the live network traffic from the device while running the experiment, you can use the
# following commands to forward the PCAPdroid traffic to your local machine and open it in Wireshark:
# adb forward tcp:54320 tcp:54320
# curl -sNL http://127.0.0.1:54320 | /Applications/Wireshark.app/Contents/MacOS/Wireshark -k -i -
