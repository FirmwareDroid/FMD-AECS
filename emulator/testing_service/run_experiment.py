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
import glob
import re
try:
    import crash_watcher
except Exception:
    crash_watcher = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTALL_APPIUM = os.path.join(BASE_DIR, 'appium', 'install_appium.py')
RUN_PCAPDROID = os.path.join(BASE_DIR, 'appium', 'run_pcapdroid_on_all.py')
ACVTOOL = os.path.join(BASE_DIR, 'coverage', 'acvtool_wrapper.py')
LOGCAT_COLLECTOR = os.path.join(BASE_DIR, 'coverage', 'collect_logcat.py')
INSTALL_APPS = os.path.join(BASE_DIR, 'install_apps.py')
LAUNCHER_TEST = os.path.join(BASE_DIR, 'launcher_test.py')
CONNECTIVITY_TEST = os.path.join(BASE_DIR, 'connectivity_test.py')

# App testing tool wrappers (app_testing_tools/)
START_APPS_BASIC = os.path.join(BASE_DIR, 'app_testing_tools', 'run_apps_start_stop.py')
RUN_APE = os.path.join(BASE_DIR, 'app_testing_tools', 'run_ape.py')
RUN_FASTBOT = os.path.join(BASE_DIR, 'app_testing_tools', 'run_fastbot.py')
RUN_KEA2 = os.path.join(BASE_DIR, 'app_testing_tools', 'run_kea2.py')
RUN_DROIDRUN = os.path.join(BASE_DIR, 'app_testing_tools', 'droidrun_agent_cli.py')

OUT_DIR = os.path.join(BASE_DIR, 'out')

# If a tools venv was created by install_tools.py, prefer using it for launching
# app-testing scripts and ensure its bin/ directory is on PATH so commands installed
# into the venv are discoverable when run via shell.
VENV_PYTHON = None
try:
    _tools_venv = os.path.join(BASE_DIR, 'app_testing_tools', 'tools', 'venv')
    if os.path.isdir(_tools_venv):
        for pyname in ('python3', 'python'):
            candidate = os.path.join(_tools_venv, 'bin', pyname)
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                VENV_PYTHON = candidate
                break
        venv_bin = os.path.join(_tools_venv, 'bin')
        if os.path.isdir(venv_bin):
            # Prepend venv bin to PATH so shell-invoked commands find venv-installed tools
            os.environ['PATH'] = venv_bin + os.pathsep + os.environ.get('PATH', '')
            logging.info('Prepended tools venv bin to PATH: %s', venv_bin)
        if VENV_PYTHON:
            logging.info('Using tools venv python for helper scripts: %s', VENV_PYTHON)
except Exception:
    logging.exception('Failed to detect/apply tools venv')


def configure_logging(out_dir: str, log_filename: str = 'run_experiment.log', level: int = logging.INFO):
    """Configure root logger to log to both stdout and a file in out_dir.

    Ensures out_dir exists and creates/uses the file 'log_filename' inside it.
    """
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, log_filename)

    logger = logging.getLogger()
    logger.setLevel(level)

    # Remove any existing handlers to avoid duplicate logs when re-importing
    for h in list(logger.handlers):
        logger.removeHandler(h)

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # Console handler (stdout)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    # File handler (append)
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.setLevel(level)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    logger.info('Logging initialized: stdout + %s', log_path)


# Initialize logging now that BASE_DIR/OUT_DIR are known
configure_logging(OUT_DIR)

def parse_args():
    parser = argparse.ArgumentParser(description='Run experiment pipeline')
    parser.add_argument(
        '--mode',
        choices=['basic', 'droidrun', 'single', 'monkey', 'ape', 'fastbot', 'kea2', 'pipeline'],
        default='single',
        help=(
            'Test mode: '
            '"basic" runs the START_APPS_BASIC start/stop test; '
            '"monkey" runs Android Monkey; '
            '"droidrun" runs the Droidrun LLM agent; '
            '"ape" runs the Ape search-based testing tool; '
            '"fastbot" runs Fastbot2.0 model-based testing; '
            '"kea2" runs Kea2 property-based testing; '
            '"single" runs a simple test cycle (default); '
            '"pipeline" runs Fastbot -> Kea2 -> Ape -> Monkey -> basic in sequence'
        ),
    )
    parser.add_argument('--test-only-one', action='store_true', help='If set, only the first app in the list will be tested')
    parser.add_argument('--skip-setup', action='store_true', help='Skip device setup steps (installing Appium/PCAPdroid/Droidrun)')
    # pcap_http_port=args.pcap_http_port, socks5_address=args.socks5_address
    parser.add_argument('--pcapdroid', action='store_true', help='Enable PCAPdroid setup on connected devices')
    parser.add_argument('--pcap-http-port', type=int, default=54320, help='Port to use for pcap http server (used when --pcapdroid set)')
    parser.add_argument('--socks5-address', type=str, default='127.0.0.1', help='The SOCKS5 proxy address (used when --pcapdroid set)')
    parser.add_argument('--retries', type=int, default=10, help='Number of times to retry the full experiment on failure (default: 1)')
    parser.add_argument('--retry-delay', type=int, default=30, help='Seconds to wait between retry attempts (default: 10)')
    parser.add_argument('--skip-install', action='store_true', help='Skip installing APKs on devices (do not run INSTALL_APPS)')
    return parser.parse_args()

def run_script(script_path, args=None, description=None):
    interpreter = VENV_PYTHON or sys.executable
    cmd = [interpreter, script_path]
    if args:
        cmd.extend(args)
    logging.info(f"Running: {description or script_path}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    out = (result.stdout or '').strip()
    err = (result.stderr or '').strip()
    if out:
        logging.info(out)
    if err:
        # Some tools (e.g. ACVTool) log informational messages to stderr while
        # still returning exit code 0. Treat stderr as INFO when returncode==0.
        if result.returncode == 0:
            logging.info(err)
        else:
            logging.error(err)
    if result.returncode != 0:
        logging.error(f"Failed: {description or script_path} (exit code {result.returncode})")
        sys.exit(result.returncode)

def run_script_capture(script_path, args=None, description=None):
    """Run a python script and capture detailed result without exiting the process.

    Returns a dict with keys: script, args, description, returncode, stdout, stderr, start_time, end_time, duration
    """
    interpreter = VENV_PYTHON or sys.executable
    cmd = [interpreter, script_path]
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
    out = (proc.stdout or '').strip()
    err = (proc.stderr or '').strip()
    if out:
        logging.info(out)
    if err:
        # Some CLI tools write informational logs to stderr but still succeed.
        # Log stderr as INFO when the command succeeded (returncode==0), otherwise
        # treat it as an error.
        if proc.returncode == 0:
            logging.info(err)
        else:
            logging.error(err)
    # Detect common adb/device transient failures in tool output and raise
    # a RuntimeError so the outer experiment retry loop can re-run the full
    # attempt. We look for phrases such as "device offline" or "device 'X' not found".
    try:
        combined = (proc.stdout or '') + '\n' + (proc.stderr or '')
        low = combined.lower()
        # Patterns indicating transient adb/device availability issues
        offline_patterns = [r'device offline', r"device '\w+' not found", r"device '.*' not found", r'error: device not found', r'failed to get feature set: device offline']
        for pat in offline_patterns:
            if re.search(pat, low):
                logging.error('Detected adb/device availability error in %s output; will treat as transient and retry full experiment: %s', script_path, pat)
                # Include some context in the exception
                snippet = '\n'.join((combined or '').splitlines()[-20:])
                raise RuntimeError(f'ADB/device offline detected while running {script_path}: {snippet}')
    except RuntimeError:
        # propagate to be handled by outer retry loop
        raise
    except Exception:
        logging.debug('Error while checking tool output for adb/device offline patterns', exc_info=True)

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


def setup_devices(mode='basic', pcapdroid=False, pcap_http_port=54320, socks5_address="127.0.0.1"):
    logging.info(f"Setting up devices...")
    # Optionally install Appium and configure PCAPdroid if requested
    if pcapdroid:
        appium_proc = start_appium_server()
        if appium_proc:
            # register atexit cleanup as a safety net
            atexit.register(stop_appium_server, appium_proc)
            try:
                logging.info('PCAPdroid enabled: installing Appium and configuring PCAPdroid on devices')
                run_script_capture(INSTALL_APPIUM, args=["--all"], description="Install Appium driver on all devices")
                # Configure PCAPdroid on all devices
                # Clear PCAPdroid app data on the selected device
                run_adb_shell(['pm', 'clear', 'com.emanuelef.remote_capture'], description='Clear PCAPdroid data on all devices before configuration', check=False)
                run_script_capture(RUN_PCAPDROID, args=["--http-port", str(pcap_http_port), "--socks5-address", socks5_address],
                                   description="Configure PCAPdroid on all devices")
            finally:
                stop_appium_server(appium_proc)
        else:
            logging.error('Failed to start Appium server; skipping PCAPdroid configuration')
    else:
        logging.info('PCAPdroid not enabled: skipping Appium install and PCAPdroid configuration')

    if mode == 'droidrun':
        # Install Droidrun on all devices
        run_command("droidrun setup --latest", description="Install Droidrun on all devices")
    elif mode == 'ape':
        # Verify Ape binaries are present (they will be pushed per-app in execute_app_with_coverage)
        ape_jar = os.path.join(BASE_DIR, 'app_testing_tools', 'tools', 'ape-bin', 'ape.jar')
        if os.path.exists(ape_jar):
            logging.info("Ape binaries verified at %s", ape_jar)
        else:
            logging.warning("Ape binaries not found at %s. Run install_tools.py.", ape_jar)
    elif mode == 'fastbot':
        # Verify Fastbot binaries are present (they will be pushed per-app in execute_app_with_coverage)
        fastbot_jar = os.path.join(BASE_DIR, 'app_testing_tools', 'tools', 'Fastbot_Android', 'monkeyq.jar')
        if os.path.exists(fastbot_jar):
            logging.info("Fastbot2.0 binaries verified at %s", fastbot_jar)
        else:
            logging.warning("Fastbot2.0 binaries not found at %s. Run install_tools.py.", fastbot_jar)
    elif mode == 'kea2':
        logging.info("Kea2 setup: verifying kea2 is available…")
        run_command("kea2 -h", description="Verify Kea2 installation")


def get_testing_apps():
    app_package_names = ["com.android.settings"]
    return app_package_names

def get_installed_packages():
    """Get a list of installed package names on the connected device(s) using adb."""
    try:
        result = run_adb_shell(['pm', 'list', 'packages'], description='pm list packages', check=False)
    except Exception:
        return []
    if result.returncode != 0:
        stderr = (result.stderr or '').lower()
        # If adb reports no devices available, treat this as a fatal condition for
        # this attempt so the top-level retry loop can re-run the whole experiment.
        if 'no devices' in stderr or 'no devices/emulators' in stderr or 'no devices/emulator' in stderr:
            logging.error('No adb devices found while listing packages: %s', result.stderr or result.stdout)
            # Raise an exception so the outer retry loop in main() will catch and retry
            raise RuntimeError('No adb devices found')

        if 'more than one device' in stderr or 'more than one device/emulator' in stderr:
            # find first connected device and retry with explicit -s
            try:
                proc = subprocess.run(['adb', 'devices'], capture_output=True, text=True, timeout=5)
                lines = [l.strip() for l in (proc.stdout or '').splitlines()]
                serial = None
                for l in lines[1:]:
                    if not l:
                        continue
                    parts = l.split()
                    if len(parts) >= 2 and parts[1] == 'device':
                        serial = parts[0]
                        break
                if serial:
                    logging.info('Multiple adb devices present; auto-selecting first device %s for package listing', serial)
                    res2 = subprocess.run(['adb', '-s', serial, 'shell', 'pm', 'list', 'packages'], capture_output=True, text=True, timeout=15)
                    if res2.returncode != 0:
                        logging.error('Failed to get installed packages from device %s: %s', serial, res2.stderr or res2.stdout)
                        return []
                    lines = (res2.stdout or '').strip().splitlines()
                else:
                    logging.error('adb reported multiple devices but no available device found in `adb devices` output')
                    return []
            except Exception:
                logging.exception('Error while attempting to auto-select first adb device')
                return []
        else:
            logging.error(f"Failed to get installed packages: {result.stderr}")
            return []
    else:
        lines = (result.stdout or '').strip().splitlines()
    packages = [line.replace("package:", "").strip() for line in lines if line.startswith("package:")]
    return packages


def _adb_base_cmd():
    """
    Build the base adb command list; include -s <serial> if ANDROID_SERIAL or ADB_SERIAL
    environment variable is set so this works with multiple devices.
    """
    serial = os.environ.get('ANDROID_SERIAL') or os.environ.get('ADB_SERIAL')
    cmd = ['adb']

    if not serial:
        # If no explicit serial is provided, try to pick the first connected device
        # This avoids 'adb shell' failing with "error: more than one device/emulator".
        try:
            res = subprocess.run(['adb', 'devices'], capture_output=True, text=True, timeout=5)
            out = (res.stdout or '')
            for l in out.splitlines():
                l = l.strip()
                if not l or l.startswith('List of devices'):
                    continue
                parts = l.split()
                if len(parts) >= 2 and parts[1] == 'device':
                    serial = parts[0]
                    logging.debug('Auto-selected adb serial: %s', serial)
                    break
        except Exception as e:
            logging.debug('Failed to auto-detect adb serial: %s', e)

    if serial:
        cmd.extend(['-s', serial])
    return cmd


def run_adb_shell(args_list, description=None, check=True, timeout=30):
    """Run an adb shell command against the selected device.

    args_list: list of arguments to pass after 'shell', e.g. ['pm', 'list', 'packages']
    check: if True, raise SystemExit on non-zero returncode (same behavior as older run_command)
    Returns CompletedProcess
    """
    adb_cmd = _adb_base_cmd() + ['shell'] + list(args_list)
    logging.info('Running adb shell: %s', ' '.join(adb_cmd))
    try:
        res = subprocess.run(adb_cmd, capture_output=True, text=True, timeout=timeout)
        if res.stdout:
            logging.info(res.stdout)
        if res.stderr:
            logging.warning(res.stderr)
        if check and res.returncode != 0:
            logging.error('Failed: %s (exit code %s)', description or 'adb shell', res.returncode)
            sys.exit(res.returncode)
        return res
    except Exception as e:
        logging.exception('Exception while running adb shell %s: %s', args_list, e)
        if check:
            sys.exit(2)
        raise


def wait_for_boot_completed(max_wait_seconds=300, sleep_seconds=30):
    """
    Poll 'adb shell getprop init.svc.bootanim' and wait while it is 'running'.

    Additionally checks the "boot complete" flag `sys.boot_completed` and treats the
    device as successfully booted if that property is set (e.g., '1' or 'true'), even if
    the boot animation property still reports 'running'. This helps when some devices
    finish boot but the boot animation remains in a running state longer.

    - Sleeps sleep_seconds between checks.
    - Stops waiting after max_wait_seconds and returns a tuple:
        (timed_out_or_still_running: bool, last_value: str, last_error: Optional[str])
      where the boolean is True when the wait ended due to timeout (bootanim still running),
      False when the property was observed not 'running' OR the boot-complete flag is set
      (i.e., boot finished).
    """
    adb_cmd_base = _adb_base_cmd()
    max_tries = max(1, int(max_wait_seconds // sleep_seconds))
    tries = 0
    last_error = None
    last_value = ''

    logging.info("Waiting for init.svc.bootanim to stop (max %s seconds, interval %s seconds)...",
                 max_wait_seconds, sleep_seconds)

    while True:
        bootanim_value = ''
        boot_completed_value = ''

        # Query both properties in a single adb shell invocation to avoid session differences
        combined_cmd = "getprop init.svc.bootanim; getprop sys.boot_completed"
        try:
            proc = subprocess.run(adb_cmd_base + ['shell', combined_cmd], capture_output=True, text=True, timeout=10)
            out = (proc.stdout or '')
            # Normalize and split into lines; ignore empty lines
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            if len(lines) >= 1:
                bootanim_value = lines[0]
            if len(lines) >= 2:
                boot_completed_value = lines[1]
            if proc.stderr:
                # capture stderr for diagnostics but don't treat as fatal
                logging.warning('adb stderr while querying boot props: %s', proc.stderr.strip())
        except Exception as e:
            last_error = str(e)
            logging.warning("Failed to query adb for boot properties: %s", e)

        # Prepare a combined last_value for diagnostics
        last_value = f"init.svc.bootanim={bootanim_value}; sys.boot_completed={boot_completed_value}"

        # Determine success: either bootanim stopped OR boot_completed indicates boot finished
        if bootanim_value and bootanim_value.strip().lower() == 'stopped':
            logging.info("Bootanim reported 'stopped'")
            is_running = False
            break

        if boot_completed_value and boot_completed_value.strip().lower() in ('1', 'true'):
            logging.info("sys.boot_completed indicates boot finished (value=%s). Treating as success.", boot_completed_value)
            is_running = False
            break

        tries += 1
        if tries >= max_tries:
            logging.warning("init.svc.bootanim remained '%s' after %s seconds (max wait).", last_value, max_wait_seconds)
            is_running = True
            break

        logging.info("init.svc.bootanim is '%s' (try %d/%d). Sleeping %s seconds...", last_value, tries, max_tries, sleep_seconds)
        time.sleep(sleep_seconds)

    return is_running, last_value, last_error


def wait_for_adb_available(max_wait_seconds=600, sleep_seconds=5):
    """
    Wait for adb to become available and for at least one device to be connected.

    - Polls `adb devices` every `sleep_seconds` seconds up to `max_wait_seconds`.
    - Returns True if at least one device in state 'device' is observed before timeout.
    - Returns False on timeout.
    """
    max_tries = max(1, int(max_wait_seconds // sleep_seconds))
    tries = 0
    logging.info("Waiting for adb to be available and show at least one device (max %s seconds)...", max_wait_seconds)

    # Ensure adb binary exists on PATH before polling. If it's missing, fail fast with a clear error.
    if not shutil.which('adb'):
        logging.error("adb binary not found in PATH; please install Android platform-tools and ensure 'adb' is on your PATH")
        return False

    while True:
        try:
            proc = subprocess.run(['adb', 'devices'], capture_output=True, text=True, timeout=10)
            out = (proc.stdout or '').strip()
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            device_lines = []
            for l in lines:
                if l.startswith('List of devices attached'):
                    continue
                parts = l.split()
                if len(parts) >= 2 and parts[1] == 'device':
                    device_lines.append(parts[0])

            if device_lines:
                logging.info('Found adb device(s): %s', device_lines)
                return True
        except Exception as e:
            logging.debug('adb devices check failed: %s', e)

        tries += 1
        if tries >= max_tries:
            logging.error('Timed out waiting for adb/devices after %s seconds', max_wait_seconds)
            return False

        logging.info('No adb device yet (try %d/%d). Sleeping %s seconds...', tries, max_tries, sleep_seconds)
        time.sleep(sleep_seconds)


def execute_app_with_coverage(package, mode, skip_install=False):
    logging.info(f"Executing app test with package: {package}, mode: {mode}")
    run_script_capture(ACVTOOL, args=["flush", package, "--wd", OUT_DIR], description="Run ACVTool to flush coverage measurement.")
    run_script_capture(ACVTOOL, args=["activate", package], description="Run ACVTool to activate coverage measurement.")
    if mode == 'droidrun':
        run_script_capture(RUN_DROIDRUN, args=["run"], description="Run Droidrun agent to test apps.")
    elif mode == 'monkey':
        run_script_capture(START_APPS_BASIC, args=["-m", "5000", "--monkey-seed", "1337", "--monkey-randomize-throttle", "-p", package])
    elif mode == 'ape':
        run_script_capture(RUN_APE, args=["-p", package], description=f"Run Ape search-based testing for {package}")
    elif mode == 'fastbot':
        run_script_capture(RUN_FASTBOT, args=["-p", package], description=f"Run Fastbot2.0 model-based testing for {package}")
    elif mode == 'kea2':
        run_script_capture(RUN_KEA2, args=["-p", package], description=f"Run Kea2 property-based testing for {package}")
    elif mode == 'pipeline':
        # Run tools sequentially: Fastbot -> Kea2 -> Ape -> Monkey -> basic
        logging.info('Running pipeline: Fastbot -> Kea2 -> Ape -> Monkey -> basic for %s', package)
        run_script_capture(RUN_FASTBOT, args=["-p", package], description=f"Run Fastbot2.0 model-based testing for {package}")
        run_script_capture(RUN_KEA2, args=["-p", package], description=f"Run Kea2 property-based testing for {package}")
        run_script_capture(RUN_APE, args=["-p", package], description=f"Run Ape search-based testing for {package}")
        # Monkey: use a small number of events to try to exercise the launcher
        run_script_capture(START_APPS_BASIC, args=["-m", "5000", "--monkey-seed", "1337", "--monkey-randomize-throttle", "-p", package], description=f"Run Monkey for {package}")
        # Basic start/stop
        run_script_capture(START_APPS_BASIC, args=["-p", package], description=f"Run basic start/stop test for {package}")
    else:
        run_script_capture(START_APPS_BASIC, args=["-p", package], description=f"Run basic start/stop test for {package}")

    # Optionally skip installation-related steps if requested
    if not skip_install:
        run_script_capture(ACVTOOL, args=["snap", package, "--wd", OUT_DIR], description="Run ACVTool to get coverage measurement")
        run_script_capture(ACVTOOL, args=["cover-pickles", package, "--wd", OUT_DIR],
                           description="Run ACVTool to deserialize coverage measurement")
        run_script_capture(ACVTOOL, args=["report", package, "--wd", OUT_DIR],
                           description="Run ACVTool to generate html coverage report")
    else:
        logging.info('Skipping ACVTool snap/cover-pickles/report because --skip-install was requested')



def start_experiment(mode='single', test_only_one=False, skip_install=False):
    install_output_path = os.path.join(OUT_DIR, 'install_results.json')
    if test_only_one:
        app_package_names = get_testing_apps()
        first_pkg = app_package_names[0]
        logging.info('Test-only-one enabled; testing only first package: %s', first_pkg)
        if not skip_install:
            run_script_capture(INSTALL_APPS, args=["--package", first_pkg, "--output", install_output_path], description=f"Install app {first_pkg} on devices.")
        else:
            logging.info('Skipping installation of %s due to --skip-install', first_pkg)
        execute_app_with_coverage(first_pkg, mode, skip_install=skip_install)
    else:
        if not skip_install:
            run_script_capture(INSTALL_APPS, args=["-a", "--output", install_output_path], description=f"Install all apps on devices.")
        else:
            logging.info('Skipping installation of apps due to --skip-install')
        app_package_names = get_installed_packages()
        # TODO Filter apps that have an Activity
        logging.info('Testing all packages; total count: %d', len(app_package_names))
        if not app_package_names:
            logging.info('No packages found to test')
            return
        for package in app_package_names:
            logging.info(f"Starting {package}")
            execute_app_with_coverage(package, mode)
    logging.info('Starting logcat collector')
    run_script_capture(LOGCAT_COLLECTOR, args=["--full-dump"], description="Collect all logcat logs")

def run_launcher_test(results_dir):
    logging.info('Running launcher preflight test before any device setup')

    preflight_name = 'preflight_launcher'
    res = run_script_capture(LAUNCHER_TEST, args=['--output-dir', results_dir, '--name', preflight_name], description='Run launcher preflight test')

    # find the newest JSON file produced (preflight_*.json)
    json_matches = glob.glob(os.path.join(results_dir, f"{preflight_name}.json"))
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
        logging.error('No %s JSON output found in %s', preflight_name, results_dir)
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
        # Add an explicit success field based on the returncode (0 -> success)
        res['success'] = (res.get('returncode', 1) == 0)
        with open(out_file, 'w', encoding='utf-8') as of:
            json.dump(res, of, indent=2)
        logging.info('Wrote connectivity test results to %s', out_file)
    except Exception:
        logging.exception('Failed to write connectivity test results')

    # Return True if the connectivity test reported success (exit code 0)
    return (res.get('returncode', 1) == 0)


def start_tcpdump():
    """Configure device to forward packets to NFLOG and start tcpdump on the device.

    - Attempts to run `adb root` (may fail on production devices but OK to try).
    - Installs iptables mangle rule to send OUTPUT to NFLOG group 1.
    - Starts tcpdump on the device writing to /storage/emulated/0/Download/tcpdump.pcap
      and records the remote pid to OUT_DIR/tcpdump_device.pid.

    This function logs warnings/errors but does not abort the experiment on failure.
    Returns True if tcpdump was started successfully, False otherwise.
    """
    logging.info('Setting up tcpdump')

    # quick checks
    if not shutil.which('adb'):
        logging.error('adb binary not found in PATH; cannot configure tcpdump on device')
        return False

    remote_pcap = '/storage/emulated/0/Download/tcpdump.pcap'
    pid_file = os.path.join(OUT_DIR, 'tcpdump_device.pid')

    # Retry loop: sometimes devices take a moment to be ready or tcpdump fails to start
    max_start_attempts = 30
    per_attempt_wait_seconds = 30.0
    check_sleep = 30
    check_attempts = max(1, int(per_attempt_wait_seconds / check_sleep))

    for attempt in range(1, max_start_attempts + 1):
        logging.info('tcpdump start attempt %d/%d', attempt, max_start_attempts)

        # If multiple devices are connected, pick the first one from `adb devices`.
        try:
            out = subprocess.run(['adb', 'devices'], capture_output=True, text=True, timeout=10)
            lines = [l.strip() for l in (out.stdout or '').splitlines() if l.strip()]
            serial = None
            for l in lines:
                if l.startswith('List of devices'):
                    continue
                parts = l.split()
                if len(parts) >= 2 and parts[1] == 'device':
                    serial = parts[0]
                    break
            if not serial:
                logging.error('No adb device found to start tcpdump (attempt %d/%d)', attempt, max_start_attempts)
                # If not the last attempt, wait and retry device selection
                if attempt < max_start_attempts:
                    time.sleep(2.0)
                    continue
                return False
            logging.info('Selected first adb device: %s', serial)
        except Exception:
            logging.exception('Failed to run adb devices to select device')
            if attempt < max_start_attempts:
                time.sleep(2.0)
                continue
            return False

        # try to become root (best-effort) on selected device
        try:
            adb_root_cmd = ['adb', '-s', serial, 'root']
            res = subprocess.run(adb_root_cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                logging.info('adb root: OK')
            else:
                logging.warning('adb root returned non-zero: %s. Continuing (may still work if device already has privileges).', res.stderr.strip() or res.stdout.strip())
        except Exception as e:
            logging.warning('adb root failed: %s', e)

        # Add iptables rule to send OUTPUT to NFLOG group 1 (best-effort)
        ipt_cmd = 'iptables -t mangle -I OUTPUT 1 -j NFLOG --nflog-group 1'
        try:
            adb_ipt_cmd = ['adb', '-s', serial, 'shell', ipt_cmd]
            res = subprocess.run(adb_ipt_cmd, capture_output=True, text=True, timeout=10)
            if res.returncode != 0:
                logging.warning('Failed to add iptables NFLOG rule: %s', (res.stderr or res.stdout).strip())
            else:
                logging.info('Installed iptables NFLOG rule')
        except Exception:
            logging.exception('Exception while installing iptables NFLOG rule')

        # Start tcpdump in background on the device and capture its pid
        start_cmd = f"nohup tcpdump -i nflog:1 -w {remote_pcap} &"
        try:
            adb_start_cmd = ['adb', '-s', serial, 'shell', start_cmd]
            res = subprocess.run(adb_start_cmd, capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                out = (res.stdout or '').strip()
                # stdout may contain extra messages; take last line as pid
                pid = None
                if out:
                    pid = out.splitlines()[-1].strip()

                # Write pid file if we can determine pid (best-effort)
                if pid and pid.isdigit():
                    try:
                        os.makedirs(OUT_DIR, exist_ok=True)
                        with open(pid_file, 'w', encoding='utf-8') as f:
                            f.write(pid + '\n')
                        logging.info('Started tcpdump on device (pid=%s), pid written to %s', pid, pid_file)
                    except Exception:
                        logging.exception('Failed to write tcpdump pid file')
                else:
                    logging.warning('Could not determine tcpdump pid from adb output: %s', out or '(empty)')

                # Verify that the remote pcap file exists. tcpdump should create the file
                # shortly after starting; poll for a short period to allow for delays.
                pcap_exists = False
                for attempt_check in range(1, check_attempts + 1):
                    try:
                        # Use ls to check existence; ls returns 0 when file present
                        adb_ls = ['adb', '-s', serial, 'shell', 'ls', '-l', remote_pcap]
                        ls_res = subprocess.run(adb_ls, capture_output=True, text=True, timeout=5)
                        if ls_res.returncode == 0 and (ls_res.stdout or '').strip():
                            pcap_exists = True
                            logging.info('Remote pcap file exists: %s (check %d/%d)', remote_pcap, attempt_check, check_attempts)
                            break
                        else:
                            logging.debug('Remote pcap not yet present (check %d/%d): %s', attempt_check, check_attempts, (ls_res.stderr or ls_res.stdout).strip())
                    except Exception:
                        logging.debug('Exception while checking remote pcap existence (attempt %d)', attempt_check)
                    time.sleep(check_sleep)

                if pcap_exists:
                    return True
                else:
                    logging.error('tcpdump did not create remote pcap %s within %.1f seconds (attempt %d/%d)', remote_pcap, check_attempts * check_sleep, attempt, max_start_attempts)
                    # Try to clean up any running tcpdump before retrying (best-effort)
                    try:
                        adb_pkill = ['adb', '-s', serial, 'shell', 'pkill -2 tcpdump']
                        subprocess.run(adb_pkill, capture_output=True, text=True, timeout=10)
                    except Exception:
                        logging.debug('pkill attempt during cleanup failed')

                    # Remove pid file if present
                    try:
                        if os.path.exists(pid_file):
                            os.remove(pid_file)
                    except Exception:
                        logging.debug('Failed to remove pid file during cleanup')

                    # If not last attempt, wait a bit and retry
                    if attempt < max_start_attempts:
                        time.sleep(2.0)
                        continue
                    return False
            else:
                logging.error('Failed to start tcpdump on device: %s', (res.stderr or res.stdout).strip())
                if attempt < max_start_attempts:
                    time.sleep(2.0)
                    continue
                return False
        except Exception:
            logging.exception('Exception while starting tcpdump on device')
            if attempt < max_start_attempts:
                time.sleep(2.0)
                continue
            return False

    return False

def stop_tcpdump():
    logging.info('Stopping tcpdump')

    # Determine first connected device (same selection as start_tcpdump)
    try:
        out = subprocess.run(['adb', 'devices'], capture_output=True, text=True, timeout=10)
        lines = [l.strip() for l in (out.stdout or '').splitlines() if l.strip()]
        serial = None
        for l in lines:
            if l.startswith('List of devices'):
                continue
            parts = l.split()
            if len(parts) >= 2 and parts[1] == 'device':
                serial = parts[0]
                break
        if not serial:
            logging.warning('No adb device found to stop tcpdump')
            return False
    except Exception:
        logging.exception('Failed to run adb devices to select device for stop_tcpdump')
        return False

    pid_file = os.path.join(OUT_DIR, 'tcpdump_device.pid')
    pid = None
    if os.path.exists(pid_file):
        try:
            with open(pid_file, 'r', encoding='utf-8') as f:
                pid = f.read().strip().splitlines()[0].strip()
        except Exception:
            logging.exception('Failed to read tcpdump pid file %s', pid_file)

    # Try to stop tcpdump on device. Prefer pkill, then kill by pid if available.
    try:
        # Try to become root on the device so pkill/kill have permission to send signals
        try:
            adb_root_cmd = ['adb', '-s', serial, 'root']
            root_res = subprocess.run(adb_root_cmd, capture_output=True, text=True, timeout=10)
            if root_res.returncode == 0:
                logging.info('adb root: OK on device %s', serial)
            else:
                logging.debug('adb root returned non-zero on device %s: %s', serial, (root_res.stderr or root_res.stdout).strip())
        except Exception:
            logging.exception('adb root attempt failed')

        adb_pkill = ['adb', '-s', serial, 'shell', 'pkill -2 tcpdump']
        res = subprocess.run(adb_pkill, shell=False, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            logging.info('Requested tcpdump termination via pkill on device %s', serial)
        else:
            # If pkill failed due to permission, try running via su on-device if available
            stderr_out = (res.stderr or res.stdout).strip()
            logging.info('pkill returned non-zero (may be fine): %s', stderr_out)
            try:
                adb_su_pkill = ['adb', '-s', serial, 'shell', 'su', '-c', 'pkill -2 tcpdump']
                su_res = subprocess.run(adb_su_pkill, shell=False, capture_output=True, text=True, timeout=10)
                if su_res.returncode == 0:
                    logging.info('Requested tcpdump termination via su+pkill on device %s', serial)
                else:
                    logging.debug('su+pkill returned non-zero on device %s: %s', serial, (su_res.stderr or su_res.stdout).strip())
            except Exception:
                logging.exception('su+pkill attempt failed')
    except Exception:
        logging.exception('pkill on device failed')

    if pid:
        try:
            adb_kill = ['adb', '-s', serial, 'shell', f'kill -2 {pid}']
            res = subprocess.run(adb_kill, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                logging.info('Sent SIGINT to tcpdump pid %s on device %s', pid, serial)
            else:
                logging.info('kill returned non-zero (may be fine): %s', (res.stderr or res.stdout).strip())
        except Exception:
            logging.exception('Failed to kill tcpdump pid on device')

    # Wait briefly for device to flush pcap
    time.sleep(1)

    # Attempt to pull pcap from device to OUT_DIR
    remote_pcap = '/storage/emulated/0/Download/tcpdump.pcap'
    local_pcap = os.path.join(OUT_DIR, "pcaps", f'tcpdump_{serial}.pcap')
    try:
        adb_pull = ['adb', '-s', serial, 'pull', remote_pcap, local_pcap]
        res = subprocess.run(adb_pull, capture_output=True, text=True, timeout=60)
        if res.returncode == 0:
            logging.info('Pulled tcpdump pcap to %s', local_pcap)
        else:
            logging.warning('Failed to pull pcap (%s). adb pull output: %s', remote_pcap, (res.stderr or res.stdout).strip())
    except Exception:
        logging.exception('Exception while pulling pcap from device')

    # Attempt to pull SSL key log from common locations on the device. Save into OUT_DIR/pcaps
    try:
        ssl_dir = os.path.join(OUT_DIR, 'pcaps')
        os.makedirs(ssl_dir, exist_ok=True)
        # prefer a per-device filename to avoid collisions
        local_ssl = os.path.join(ssl_dir, f'sslkeylog_{serial}.log')

        remote_candidates = [
            '/storage/emulated/0/Download/sslkeylog.log',
            '/sdcard/Download/sslkeylog.log',
            '/sdcard/sslkeylog.log',
            '/data/local/tmp/sslkeylog.log',
            '/data/misc/ssl/sslkeylog.log',
        ]
        pulled = False
        for remote in remote_candidates:
            try:
                # Check existence
                ls_cmd = ['adb', '-s', serial, 'shell', 'ls', '-l', remote]
                ls_res = subprocess.run(ls_cmd, capture_output=True, text=True, timeout=5)
                if ls_res.returncode != 0:
                    continue
                # Pull the file
                pull_cmd = ['adb', '-s', serial, 'pull', remote, local_ssl]
                pull_res = subprocess.run(pull_cmd, capture_output=True, text=True, timeout=20)
                if pull_res.returncode == 0:
                    logging.info('Pulled SSL keylog from device %s -> %s (remote=%s)', serial, local_ssl, remote)
                    pulled = True
                    break
                else:
                    logging.debug('adb pull of %s failed: %s', remote, (pull_res.stderr or pull_res.stdout).strip())
            except Exception:
                logging.debug('Exception while attempting to pull SSL key from %s', remote, exc_info=True)

        if not pulled:
            logging.info('No SSL keylog found on device %s at known locations; skipped pulling ssl keys', serial)
    except Exception:
        logging.exception('Failed to pull SSL keylog from device')

    # Retrieve package -> UID mapping from device
    try:
        adb_pm = ['adb', '-s', serial, 'shell', 'pm', 'list', 'packages', '-U']
        res = subprocess.run(adb_pm, capture_output=True, text=True, timeout=20)
        pkg_map = {}
        pm_success = (res.returncode == 0)
        pm_error = None
        if pm_success:
            out = res.stdout or ''
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                # Example line: package:com.example uid:12345
                pkg = None
                uid = None
                if line.startswith('package:'):
                    # split by spaces
                    parts = line.split()
                    for p in parts:
                        if p.startswith('package:'):
                            pkg = p.split('package:', 1)[1]
                        if p.startswith('uid:'):
                            try:
                                uid = int(p.split('uid:', 1)[1])
                            except Exception:
                                uid = None
                    if pkg:
                        pkg_map[pkg] = uid
        else:
            pm_error = (res.stderr or res.stdout or '').strip()
            logging.warning('pm list packages -U returned non-zero: %s', pm_error)

        # Prepare JSON content with diagnostics so the file is never left empty and
        # contains useful information when the pm command failed.
        mapping_content = {
            'success': pm_success,
            'error': pm_error,
            'mapping': pkg_map,
        }

        # Write mapping to OUT_DIR (retry and write atomically to avoid partial writes)
        try:
            os.makedirs(OUT_DIR, exist_ok=True)
            mapping_file = os.path.join(OUT_DIR, f'package_uids_{serial}.json')
            tmp_path = mapping_file + '.tmp'
            write_ok = False
            for attempt_write in range(1, 4):
                try:
                    with open(tmp_path, 'w', encoding='utf-8') as mf:
                        json.dump(mapping_content, mf, indent=2)
                        mf.flush()
                        try:
                            os.fsync(mf.fileno())
                        except Exception:
                            # best-effort; some filesystems may not support fsync on writer
                            pass
                    os.replace(tmp_path, mapping_file)
                    logging.info('Wrote package->UID mapping to %s (attempt %d)', mapping_file, attempt_write)
                    write_ok = True
                    break
                except Exception as e:
                    logging.exception('Attempt %d: Failed to write package->UID mapping to %s: %s', attempt_write, mapping_file, e)
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass
                    time.sleep(0.5 * attempt_write)
            if not write_ok:
                logging.error('Failed to write package->UID mapping to %s after multiple attempts', mapping_file)
        except Exception:
            logging.exception('Failed to prepare directory for package->UID mapping')
    except Exception:
        logging.exception('Failed to retrieve package UID mapping from device')

    # Optionally remove pid file
    try:
        if os.path.exists(pid_file):
            os.remove(pid_file)
    except Exception:
        logging.debug('Failed to remove pid file %s', pid_file)

    return True


def _write_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
    except Exception:
        logging.exception('Failed to write JSON to %s', path)


def _read_json_if_exists(path):
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        logging.exception('Failed to read JSON from %s', path)
    return None


def _determine_success_from_result(obj):
    """Try to determine a boolean success value from a result JSON object.

    Heuristics used (in order):
    - If 'success' key present and boolean -> use it
    - If 'returncode' present -> success when returncode == 0
    - If 'success_count' present -> success when success_count > 0
    - Otherwise return None
    """
    if not isinstance(obj, dict):
        return None
    if 'success' in obj:
        val = obj.get('success')
        if isinstance(val, bool):
            return val
        # sometimes success might be numeric
        try:
            return bool(int(val))
        except Exception:
            pass
    if 'returncode' in obj:
        try:
            return int(obj.get('returncode', 1)) == 0
        except Exception:
            pass
    if 'success_count' in obj:
        try:
            return int(obj.get('success_count', 0)) > 0
        except Exception:
            pass
    return None


def write_experiment_summary(results_dir, overall_success, attempt=None, attempts=None):
    """Collect main test results and write a concise experiment summary JSON.

    This will only be called when the experiment succeeded (overall_success=True)
    or when all attempts have been exhausted (attempt == attempts).
    """
    summary = {
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat() + 'Z',
        'overall_success': bool(overall_success),
        'attempt': attempt,
        'attempts': attempts,
        'tests': {},
    }

    # Known/top-level result files we want to summarise if present
    base_candidates = [
        'adb_availability.json',
        'bootanim_results.json',
        'preflight_launcher.json',
        'connectivity_results.json',
        'install_results.json',
    ]

    # Also include additional known result locations that may live outside results_dir
    extra_paths = [
        os.path.join(BASE_DIR, 'app_testing_tools', 'out', 'app_start_summary.json'),
    ]

    # Collect JSON files to include: start with base_candidates under results_dir,
    # then discover any JSON files under results_dir and app_testing_tools/out.
    paths = set()
    for fname in base_candidates:
        paths.add(os.path.join(results_dir, fname))
    for p in extra_paths:
        paths.add(p)

    # Discover JSON files in results_dir and in app_testing_tools/out (if present)
    try:
        search_dirs = [results_dir, os.path.join(BASE_DIR, 'app_testing_tools', 'out')]
        for sd in search_dirs:
            if os.path.isdir(sd):
                for match in glob.glob(os.path.join(sd, '**', '*.json'), recursive=True):
                    paths.add(os.path.abspath(match))
    except Exception:
        logging.exception('Error while discovering additional result JSON files')

    # Process each discovered path and add a concise entry to the summary
    for path in sorted(paths):
        try:
            # Compute a friendly key for the summary: prefer relative to results_dir, else relative to BASE_DIR
            try:
                if os.path.commonpath([os.path.abspath(path), os.path.abspath(results_dir)]) == os.path.abspath(results_dir):
                    key = os.path.relpath(path, results_dir)
                else:
                    key = os.path.relpath(path, BASE_DIR)
            except Exception:
                key = os.path.basename(path)
            # Normalize key separators to '/'
            key = key.replace(os.path.sep, '/')

            entry = {
                'exists': os.path.exists(path),
            }

            if os.path.exists(path):
                try:
                    stat = os.stat(path)
                    entry['size_bytes'] = stat.st_size
                    entry['modified_time'] = datetime.datetime.fromtimestamp(stat.st_mtime, datetime.timezone.utc).isoformat() + 'Z'
                except Exception:
                    logging.debug('Failed to stat %s', path)

                # Heuristic: avoid loading extremely large JSON blobs into the summary
                try:
                    size = os.path.getsize(path)
                except Exception:
                    size = 0

                obj = None
                if size > 200 * 1024:
                    entry['raw_skipped_due_to_size'] = True
                else:
                    obj = _read_json_if_exists(path)

                if obj is not None:
                    entry['raw'] = {}
                    if isinstance(obj, dict):
                        # include a few informative fields but avoid dumping huge blobs
                        for key_field in ('success', 'returncode', 'error', 'stderr', 'stdout', 'success_count', 'failures_count', 'total_app_count'):
                            if key_field in obj:
                                try:
                                    entry['raw'][key_field] = obj.get(key_field)
                                except Exception:
                                    entry['raw'][key_field] = str(obj.get(key_field))
                        # Also include top-level small scalar fields (non-list/dict)
                        for k, v in obj.items():
                            if k in entry['raw']:
                                continue
                            if isinstance(v, (str, int, float, bool)):
                                # limit string length
                                if isinstance(v, str) and len(v) > 400:
                                    entry['raw'][k] = v[:400] + '...'
                                else:
                                    entry['raw'][k] = v
                    else:
                        entry['raw']['value'] = obj

                    entry['success'] = _determine_success_from_result(obj)
                else:
                    # if object could not be read, leave success unknown
                    if 'raw_skipped_due_to_size' not in entry:
                        entry['success'] = None
            else:
                entry['success'] = None

            summary['tests'][key] = entry
        except Exception:
            logging.exception('Failed to include result file %s in summary', path)

    # Atomically write the summary JSON to results_dir/experiment_summary.json
    out_file = os.path.join(results_dir, 'experiment_summary.json')
    _write_json(out_file, summary)
    logging.info('Wrote experiment summary to %s', out_file)
def ensure_adb_available(results_dir):
    """Ensure adb is available and at least one device is connected.

    Writes adb_availability.json into results_dir. Raises RuntimeError on failure.
    """
    adb_ok = wait_for_adb_available(max_wait_seconds=300, sleep_seconds=5)
    out_file = os.path.join(results_dir, 'adb_availability.json')
    try:
        _write_json(out_file, {'success': bool(adb_ok)})
        logging.info('Wrote adb availability result to %s', out_file)
    except Exception:
        logging.exception('Failed to write adb availability result')
    if not adb_ok:
        raise RuntimeError('ADB not available')


def start_background_services():
    """Start best-effort background services such as tcpdump and crash watcher."""
    tcp_ok = start_tcpdump()
    if not tcp_ok:
        logging.error('Failed to start tcpdump; aborting this attempt so the experiment retry loop can retry')
        raise RuntimeError('Failed to start tcpdump')
    if crash_watcher:
        try:
            crash_watcher.start_crash_watcher(device=None, interval=5.0)
            atexit.register(crash_watcher.stop_crash_watcher)
            logging.info('Started crash watcher (background)')
        except Exception:
            logging.exception('Failed to start crash watcher')


def stop_background_services():
    """Stop background services started by start_background_services()."""
    try:
        stop_tcpdump()
    except Exception:
        logging.exception('Error while stopping tcpdump during cleanup')

    if crash_watcher:
        try:
            crash_watcher.stop_crash_watcher()
            logging.info('Stopped crash watcher')
        except Exception:
            logging.debug('Crash watcher stop failed (may not have been started)')


def do_preflight(results_dir):
    """Perform boot wait, launcher preflight and connectivity checks.

    Raises RuntimeError on any preflight failure.
    """
    # Wait for boot animation to finish (or sys.boot_completed)
    is_running, last_value, last_error = wait_for_boot_completed(max_wait_seconds=600, sleep_seconds=10)
    bootanim_val = ''
    boot_completed_val = ''
    try:
        parts = [p.strip() for p in (last_value or '').split(';') if p.strip()]
        for p in parts:
            if p.startswith('init.svc.bootanim='):
                bootanim_val = p.split('=', 1)[1]
            if p.startswith('sys.boot_completed='):
                boot_completed_val = p.split('=', 1)[1]
    except Exception:
        logging.debug('Failed to parse last_value for boot properties: %s', last_value)

    message = {
        'success': (not is_running),
        'bootanim_timed_out': bool(is_running),
        'bootanim': bootanim_val,
        'boot_completed': boot_completed_val,
        'last_value': last_value,
        'error': last_error,
    }
    out_file = os.path.join(results_dir, 'bootanim_results.json')
    _write_json(out_file, message)
    if is_running:
        raise RuntimeError('Boot animation did not stop')

    preflight_ok = run_launcher_test(results_dir)
    if not preflight_ok:
        raise RuntimeError('Launcher preflight failed')

    # Connectivity
    ok = run_connectivity_test(results_dir)
    if not ok:
        raise RuntimeError('Connectivity test failed')

    # Disable ANR / error dialogs on the selected device (best-effort)
    run_adb_shell(['settings', 'put', 'global', 'show_annoying_receivers_in_background', '0'],
                  description='Disable show_annoying_receivers_in_background', check=False)
    run_adb_shell(['settings', 'put', 'global', 'anr_show_background', '0'],
                  description='Disable anr_show_background', check=False)
    run_adb_shell(['settings', 'put', 'global', 'hide_error_dialogs', '1'],
                  description='Disable hide_error_dialogs', check=False)


def setup_and_run_experiment(args):
    """Run setup_devices (unless skipped) and start_experiment."""
    if args.skip_setup:
        logging.info('Skipping device setup as requested (--skip-setup)')
    else:
        setup_devices(mode=args.mode, pcapdroid=getattr(args, 'pcapdroid', False), pcap_http_port=args.pcap_http_port, socks5_address=args.socks5_address)

    start_experiment(mode=args.mode, test_only_one=(getattr(args, 'test-only-one', False) or getattr(args, 'test_only_one', False) or args.test_only_one), skip_install=getattr(args, 'skip_install', False))

def main():
    args = parse_args()
    results_dir = os.path.join(BASE_DIR, 'out')
    os.makedirs(results_dir, exist_ok=True)

    attempts = max(1, int(getattr(args, 'retries', 30)))
    retry_delay = int(getattr(args, 'retry_delay', 15))

    # Single unified retry loop: perform the entire preflight and experiment in one attempt
    for attempt in range(1, attempts + 1):
        logging.info('Full-run attempt %d/%d starting...', attempt, attempts)
        try:
            # 1) Ensure adb available
            ensure_adb_available(results_dir)

            # 2) Start background helpers (tcpdump, crash watcher)
            start_background_services()

            # 3) Preflight checks: boot, launcher, connectivity
            do_preflight(results_dir)

            # 4) Setup devices and run experiment
            setup_and_run_experiment(args)

            # Success: cleanup, write summary and exit
            stop_background_services()
            logging.info('Full-run attempt %d/%d completed successfully', attempt, attempts)
            try:
                write_experiment_summary(results_dir, overall_success=True, attempt=attempt, attempts=attempts)
            except Exception:
                logging.exception('Failed to write experiment summary on success')
            return

        except KeyboardInterrupt:
            logging.info('Interrupted by user; stopping background services and aborting')
            stop_background_services()
            raise
        except Exception:
            logging.exception('Full-run attempt %d/%d failed', attempt, attempts)
            stop_background_services()
            if attempt < attempts:
                logging.info('Retrying full-run after %s seconds (attempt %d/%d)', retry_delay, attempt + 1, attempts)
                time.sleep(retry_delay)
                # try to restart tcpdump before next attempt
                try:
                    start_tcpdump()
                except Exception:
                    logging.exception('Failed to restart tcpdump before next attempt')
                continue
            else:
                logging.error('Experiment failed after %d attempt(s). Aborting.', attempts)
                try:
                    # Write final summary indicating overall failure
                    write_experiment_summary(results_dir, overall_success=False, attempt=attempt, attempts=attempts)
                except Exception:
                    logging.exception('Failed to write experiment summary on final failure')
                sys.exit(2)



if __name__ == "__main__":
    main()

## Live Network Traffic Debugging
# If you want to inspect the live network traffic from the device while running the experiment, you can use the
# following commands to forward the PCAPdroid traffic to your local machine and open it in Wireshark:
# adb forward tcp:54320 tcp:54320
# curl -sNL http://127.0.0.1:54320 | /Applications/Wireshark.app/Contents/MacOS/Wireshark -k -i -
