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

def parse_args():
    parser = argparse.ArgumentParser(description='Run experiment pipeline')
    parser.add_argument('--mode', choices=['basic', 'droidrun', 'single', 'monkey'], default='single',
                        help='Test mode: "basic" runs the START_APPS_BASIC start/stop test;'
                             '"droidrun" runs the Droidrun agent (default: basic);'
                             '"single" runs a simple test cycle (for development/debugging)')
    parser.add_argument('--test-only-one', action='store_true', help='If set, only the first app in the list will be tested')
    parser.add_argument('--skip-setup', action='store_true', help='Skip device setup steps (installing Appium/PCAPdroid/Droidrun)')
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


def setup_devices(mode='basic'):
    logging.info(f"Setting up devices...")
    # Install Appium driver
    run_script_capture(INSTALL_APPIUM, args=["--all"], description="Install Appium driver on all devices")
    # Configure PCAPdroid on all devices
    run_script_capture(RUN_PCAPDROID, description="Configure PCAPdroid on all devices")
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


def execute_app_with_coverage(package, mode):
    logging.info(f"Executing appium with package: {package}, mode: {mode}")
    run_script_capture(ACVTOOL, args=["flush", package], description="Run ACVTool to activate coverage measurement.")
    run_script_capture(ACVTOOL, args=["activate", package], description="Run ACVTool to activate coverage measurement.")
    if mode == 'droidrun':
        run_script_capture(DROIDRUN_AGENT, args=["run"], description="Run Droidrun agent to test apps.")
    elif mode == 'monkey':
        run_script_capture(START_APPS_BASIC, args=["-m", "1", "--monkey-seed", "1337", "--monkey-randomize-throttle", "-p", package])
    else:
        run_script_capture(START_APPS_BASIC, args=[package], description=f"Run basic start/stop test for {package}")
    run_script_capture(ACVTOOL, args=["snap", package], description="Run ACVTool to get coverage measurement")
    run_script_capture(ACVTOOL, args=["cover-pickles", package],
                       description="Run ACVTool to deserialize coverage measurement")
    run_script_capture(ACVTOOL, args=["report", package],
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


def main():
    args = parse_args()
    # Start a local Appium server for the experiment and ensure it is stopped at the end
    appium_proc = start_appium_server()
    if appium_proc:
        # register atexit cleanup as a safety net
        atexit.register(stop_appium_server, appium_proc)
    try:
        if args.skip_setup:
            logging.info('Skipping device setup as requested (--skip-setup)')
        else:
            setup_devices(mode=args.mode)
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
