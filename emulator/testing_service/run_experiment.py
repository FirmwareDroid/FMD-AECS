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
RUN_MONKEY = os.path.join(BASE_DIR, 'app_testing_tools', 'run_monkey.py')
RUN_APE = os.path.join(BASE_DIR, 'app_testing_tools', 'run_ape.py')
RUN_FASTBOT = os.path.join(BASE_DIR, 'app_testing_tools', 'run_fastbot.py')
RUN_KEA2 = os.path.join(BASE_DIR, 'app_testing_tools', 'run_kea2.py')
RUN_MOBILERUN = os.path.join(BASE_DIR, 'app_testing_tools', 'mobilerun', 'mobilerun_agent_cli.py')
COLLECT_DEVICE_INFO = os.path.join(BASE_DIR, 'collect_device_info.py')

OUT_DIR = os.path.join(BASE_DIR, 'out')
PID_FILE_PATH = os.path.join(OUT_DIR, 'tcpdump_device.pid')
REMOTE_PCAP_PATH = "/data/local/tmp/tcpdump.pcap"
# Global timeout control (set by main)
GLOBAL_START_TIME = None
GLOBAL_MAX_SECONDS = None


class GlobalTimeoutReached(Exception):
    """Raised when the global runtime limit has been exceeded.

    Attributes:
        attempt: current attempt index (optional)
        attempts: total attempts configured (optional)
    """
    def __init__(self, message="Global timeout reached", attempt=None, attempts=None):
        super().__init__(message)
        self.attempt = attempt
        self.attempts = attempts

def init_global_timeout(start_time_val, max_seconds_val):
    global GLOBAL_START_TIME, GLOBAL_MAX_SECONDS
    GLOBAL_START_TIME = start_time_val
    GLOBAL_MAX_SECONDS = max_seconds_val

def remaining_seconds():
    """Return remaining seconds until global timeout, or None if no timeout set."""
    if GLOBAL_START_TIME is None or GLOBAL_MAX_SECONDS is None:
        return None
    rem = GLOBAL_MAX_SECONDS - (time.time() - GLOBAL_START_TIME)
    return max(0.0, rem)

def get_effective_timeout(requested_timeout):
    """Given a requested timeout (seconds or None), return an effective timeout
    that does not exceed remaining global time. Returns None when no timeout.
    If remaining_seconds() is 0, returns 0.0 which will cause immediate Timeout.
    """
    rem = remaining_seconds()
    if rem is None:
        return requested_timeout
    # If no time left, indicate zero
    if rem <= 0:
        return 0.0
    if requested_timeout is None:
        return rem
    try:
        return min(float(requested_timeout), rem)
    except Exception:
        return rem

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
        choices=['basic', 'mobilerun', 'single', 'monkey', 'ape', 'fastbot', 'kea2', 'pipeline'],
        default='single',
        help=(
            'Test mode: '
            '"basic" runs the START_APPS_BASIC start/stop test; '
            '"monkey" runs Android Monkey; '
            '"mobilerun" runs the mobilerun LLM agent; '
            '"ape" runs the Ape search-based testing tool; '
            '"fastbot" runs Fastbot2.0 model-based testing; '
            '"kea2" runs Kea2 property-based testing; '
            '"single" runs a simple test cycle (default); '
            '"pipeline" runs the testing tools in sequence'
        ),
    )
    parser.add_argument('--test-only-one', action='store_true', help='If set, only the first app in the list will be tested')
    parser.add_argument('--skip-setup', action='store_true', help='Skip device setup steps (installing Appium/PCAPdroid, etc.)')
    # pcap_http_port=args.pcap_http_port, socks5_address=args.socks5_address
    parser.add_argument('--pcapdroid', action='store_true', help='Enable PCAPdroid setup on connected devices')
    parser.add_argument('--pcap-http-port', type=int, default=54320, help='Port to use for pcap http server (used when --pcapdroid set)')
    parser.add_argument('--socks5-address', type=str, default='127.0.0.1', help='The SOCKS5 proxy address (used when --pcapdroid set)')
    parser.add_argument('--retries', type=int, default=10, help='Number of times to retry the full experiment on failure (default: 1)')
    parser.add_argument('--retry-delay', type=int, default=30, help='Seconds to wait between retry attempts (default: 10)')
    parser.add_argument('--skip-install', action='store_true', help='Skip installing APKs on devices (do not run INSTALL_APPS)')
    parser.add_argument('--device-info-override', '-D', action='append', default=[],
                        help='Override key=value passed to device info collector (can be repeated)')
    parser.add_argument('--device-info-set-defaults', action='store_true',
                        help='Set OCTOPUS* default values on the device before collection', default=False)
    parser.add_argument('--max-duration-hours', type=float, default=24.0,
                        help='Maximum total runtime for this script in hours (default: 24)')
    return parser.parse_args()

def run_script(script_path, args=None, description=None):
    interpreter = VENV_PYTHON or sys.executable
    cmd = [interpreter, script_path]
    if args:
        cmd.extend(args)
    logging.info(f"Running: {description or script_path}")
    # Apply global timeout if configured
    eff_timeout = get_effective_timeout(None)
    if eff_timeout == 0.0:
        logging.error('Global timeout reached before running script %s', script_path)
        raise GlobalTimeoutReached('Global timeout reached before running script', attempt=None, attempts=None)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=eff_timeout)
    except subprocess.TimeoutExpired as e:
        logging.error('Script %s timed out after %.1f seconds', script_path, get_effective_timeout(None) or 0.0)
        # mimic non-zero return code and print stderr
        if e.stdout:
            logging.info(e.stdout)
        if e.stderr:
            logging.error(e.stderr)
        raise GlobalTimeoutReached(f'Script timed out: {script_path}', attempt=None, attempts=None)
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
        raise RuntimeError(f"Script failed: {description or script_path} (exit code {result.returncode})")


def run_script_capture(script_path, args=None, description=None, logfile_path=None):
    """Run a python script, streaming stdout/stderr directly to a logfile,

    then inspects the file for transient errors and returns execution metadata.
    """
    if not logfile_path:
        script_name = os.path.basename(script_path)
        logfile_path = f"/android/testing_service/out/{script_name}.log"

    interpreter = VENV_PYTHON or sys.executable
    cmd = [interpreter, script_path]
    if args:
        cmd.extend(args)

    start = datetime.datetime.now(datetime.timezone.utc).isoformat() + 'Z'
    t0 = datetime.datetime.now(datetime.timezone.utc)
    logging.info(f"Running (direct-to-file): {description or script_path}")

    eff_timeout = get_effective_timeout(None)
    if eff_timeout == 0.0:
        raise GlobalTimeoutReached('Global timeout reached before starting subprocess')

    # Ensure the directory for the logfile exists
    os.makedirs(os.path.dirname(os.path.abspath(logfile_path)), exist_ok=True)

    returncode = -1
    duration = 0.0

    # Open the file to stream stdout and stderr directly to disk
    try:
        with open(logfile_path, 'w', encoding='utf-8') as log_file:
            # Metadata header inside the log file
            log_file.write(f"=== Command: {' '.join(cmd)} ===\n")
            log_file.write(f"=== Started: {start} ===\n\n")
            log_file.flush()  # Ensure header is written before process starts

            # stdout and stderr both point to the same file descriptor
            proc = subprocess.run(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=eff_timeout
            )

            t1 = datetime.datetime.now(datetime.timezone.utc)
            duration = (t1 - t0).total_seconds()
            returncode = proc.returncode

    except subprocess.TimeoutExpired:
        t1 = datetime.datetime.now(datetime.timezone.utc)
        duration = (t1 - t0).total_seconds()
        returncode = -2

        # Append timeout info to the log file
        with open(logfile_path, 'a', encoding='utf-8') as log_file:
            log_file.write(f"\n\n[timeout after {duration:.1f}s]\n")

        logging.error('Command timed out after %.1f seconds: %s', duration, script_path)
        raise GlobalTimeoutReached(f'Subprocess timed out: {script_path}')

    finally:
        end = datetime.datetime.now(datetime.timezone.utc).isoformat() + 'Z'

    # --- Inspection Phase ---
    # Read the logfile back from disk to perform regex checks
    try:
        with open(logfile_path, 'r', encoding='utf-8') as log_file:
            combined_content = log_file.read()

        low = combined_content.lower()
        offline_patterns = [
            r'device offline',
            r"device '\w+' not found",
            r"device '.*' not found",
            r'error: device not found',
            r'failed to get feature set: device offline',
            r'no adb device found',
            r'no connected devices found',
            r'no connected devices',
            r'no devices found',
        ]
        for pat in offline_patterns:
            if re.search(pat, low):
                logging.error('Detected adb/device availability error in logfile %s; retrying full experiment.',
                              logfile_path)
                # Grab the last 20 lines for the error snippet context
                snippet = '\n'.join(combined_content.splitlines()[-20:])
                raise RuntimeError(f'ADB/device offline detected in log: {snippet}')

    except RuntimeError:
        raise
    except Exception:
        logging.debug('Error while checking logfile for adb/device offline patterns', exc_info=True)

    # Return metadata (excluding huge stdout/stderr blocks from memory)
    return {
        'script': script_path,
        'args': args or [],
        'description': description or script_path,
        'returncode': returncode,
        'logfile': logfile_path,
        'start_time': start,
        'end_time': end,
        'duration_seconds': duration,
    }

def run_command(cmd, description=None):
    logging.info(f"Running: {description or cmd}")
    # Apply global timeout if configured
    eff_timeout = get_effective_timeout(None)
    if eff_timeout == 0.0:
        logging.error('Global timeout reached before running command %s', cmd)
        raise GlobalTimeoutReached('Global timeout reached before running command')
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=eff_timeout)
    except subprocess.TimeoutExpired:
        logging.error('Command timed out: %s', cmd)
        raise GlobalTimeoutReached(f'Command timed out: {cmd}')
    logging.info(result.stdout)
    if result.stderr:
        logging.error(result.stderr)
    if result.returncode != 0:
        logging.error(f"Failed: {description or cmd} (exit code {result.returncode})")
        raise RuntimeError(f"Command failed: {description or cmd} (exit code {result.returncode})")

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

    if mode == 'mobilerun':
        run_command("mobilerun setup", description="Install mobilerun on all devices")
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
    # Respect global remaining time: shorten the requested timeout if necessary
    eff_timeout = get_effective_timeout(timeout)
    if eff_timeout == 0.0:
        logging.error('Global timeout reached before running adb shell: %s', args_list)
        raise GlobalTimeoutReached('Global timeout reached before running adb shell', attempt=None, attempts=None)
    try:
        res = subprocess.run(adb_cmd, capture_output=True, text=True, timeout=eff_timeout)
        if res.stdout:
            logging.info(res.stdout)
        if res.stderr:
            logging.warning(res.stderr)
        if check and res.returncode != 0:
            logging.error('Failed: %s (exit code %s)', description or 'adb shell', res.returncode)
            raise RuntimeError(f"adb shell failed: {description or 'adb shell'} (exit code {res.returncode})")
        return res
    except Exception as e:
        logging.exception('Exception while running adb shell %s: %s', args_list, e)
        if check:
            raise
        raise


def _is_valid_component(candidate: str) -> bool:
    """Return True if the provided string looks like a valid Android component name.

    Accept forms like 'com.example/.MainActivity' or 'com.example/com.example.MainActivity'.
    Reject short/diagnostic strings like 'No' or strings that don't contain a '/'.
    """
    if not candidate or not isinstance(candidate, str):
        return False
    s = candidate.strip()
    if '/' not in s:
        return False
    ls = s.lower()
    if ls == 'no' or ls.startswith('no ') or 'no activity' in ls:
        return False
    if ls.startswith('component=') or ls.startswith('package:'):
        return False
    return True


def package_has_activity(package: str):
    """Return (has_activity: bool, resolved_component: Optional[str]).

    Uses `adb shell cmd package resolve-activity --components <package>` and
    falls back to `--brief` when needed. Returns (False, resolved_output) when
    no activity is found or resolution fails to return a valid component.
    """
    try:
        res = run_adb_shell(['cmd', 'package', 'resolve-activity', '--components', package], description=f'probe activities for {package}', check=False)
    except Exception as e:
        # If the call itself fails, propagate the exception so callers can decide
        # to retry or treat as inconclusive. For our use we will let the caller
        # handle exceptions and default to allowing the pipeline to run.
        raise
    out = (res.stdout or '')
    for line in out.splitlines():
        if 'component=' in line:
            part = line.split('component=', 1)[1].strip()
            if not part:
                continue
            token = part.split()[0].strip()
            if _is_valid_component(token):
                return True, token
    # fallback: brief resolve
    try:
        res2 = run_adb_shell(['cmd', 'package', 'resolve-activity', '--brief', package], description=f'brief probe activities for {package}', check=False)
    except Exception:
        return False, None
    out2 = (res2.stdout or '').strip()
    if out2:
        first = out2.splitlines()[0].strip()
        candidate = first.split()[0].strip()
        if _is_valid_component(candidate):
            return True, candidate
    # Additional heuristic: inspect dumpsys package output for mentions of
    # "Activity" (case-insensitive). Some packages may expose activity
    # information in dumpsys even when resolve-activity did not return a
    # sanitized component token.
    try:
        ds = run_adb_shell(['dumpsys', 'package', package], description=f'dumpsys package for {package}', check=False)
    except Exception:
        return False, None
    dumpsys_out = (ds.stdout or '') + '\n' + (ds.stderr or '')
    if dumpsys_out and re.search(r'(?i)Activity', dumpsys_out):
        return True, 'dumpsys_contains_Activity'
    return False, None


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

def execute_apps_with_coverage(app_package_names, mode):
    for package in app_package_names:
        run_script_capture(ACVTOOL, args=["activate", package],
                           description="Run ACVTool to create coverage folder.")

    for package in app_package_names:
        logging.info(f"Starting {package}")
        exec_app_testers(package, mode)

    for package in app_package_names:
        acv_out_dir = os.path.join(OUT_DIR, 'acv_snaps', f"{package}")
        os.makedirs(acv_out_dir, exist_ok=True)
        run_script_capture(ACVTOOL, args=["snap", package, "--wd", acv_out_dir],
                           description="Run ACVTool to get coverage measurement")

def exec_app_testers(package, mode, skip_install=False):
    logging.info(f"Executing app test with package: {package}, mode: {mode}")
    if mode == 'mobilerun':
        run_script_capture(RUN_MOBILERUN, args=["run"], description="Run mobilerun agent to test apps.")
    elif mode == 'monkey':
        run_script_capture(RUN_MONKEY, args=["-m", "5000", "--monkey-seed", "1337", "--monkey-randomize-throttle", "-p", package])
    elif mode == 'ape':
        run_script_capture(RUN_APE, args=["-p", package], description=f"Run Ape search-based testing for {package}")
    elif mode == 'fastbot':
        run_script_capture(RUN_FASTBOT, args=["-p", package], description=f"Run Fastbot2.0 model-based testing for {package}")
    elif mode == 'kea2':
        run_script_capture(RUN_KEA2, args=["-p", package], description=f"Run Kea2 property-based testing for {package}")
    elif mode == 'pipeline':
        logging.info('Running pipeline: Fastbot -> Kea2 -> Ape -> Monkey for %s', package)
        run_script_capture(RUN_MONKEY, args=["-p", package], description=f"Run basic start/stop test for {package}")
        run_script_capture(RUN_FASTBOT, args=["-p", package], description=f"Run Fastbot2.0 model-based testing for {package}")
        run_script_capture(RUN_KEA2, args=["-p", package], description=f"Run Kea2 property-based testing for {package}")
        run_script_capture(RUN_APE, args=["-p", package], description=f"Run Ape search-based testing for {package}")
        run_script_capture(RUN_MONKEY, args=["-p", package], description=f"Run basic start/stop test for {package}")
    else:
        run_script_capture(RUN_MONKEY, args=["-p", package], description=f"Run basic start/stop test for {package}")

    # run_script_capture(ACVTOOL, args=["cover-pickles", package, "--wd", OUT_DIR],
    #                   description="Run ACVTool to deserialize coverage measurement")
    # run_script_capture(ACVTOOL, args=["report", package, "--wd", OUT_DIR],
    #                   description="Run ACVTool to generate html coverage report")



def start_experiment(mode='single', test_only_one=False, skip_install=False):
    install_output_path = os.path.join(OUT_DIR, 'install_results.json')
    if test_only_one:
        app_package_names = get_testing_apps()
        first_pkg = [app_package_names[0]]
        logging.info('Test-only-one enabled; testing only first package: %s', first_pkg)
        if not skip_install:
            run_script_capture(INSTALL_APPS, args=["--package", first_pkg[0], "--output", install_output_path], description=f"Install app {first_pkg} on devices.")
        else:
            logging.info('Skipping installation of %s due to --skip-install', first_pkg)
        execute_apps_with_coverage(first_pkg, mode)
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
        app_package_names.remove("android")
        execute_apps_with_coverage(app_package_names, mode)

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
    args = ['--retries', '10', '--timeout', '30']
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

def _get_first_adb_device() -> str:
    """Helper to consistently find the first available ADB device serial."""
    if not shutil.which('adb'):
        logging.error('adb binary not found in PATH')
        return ""
    try:
        out = subprocess.run(['adb', 'devices'], capture_output=True, text=True, timeout=10)
        lines = [line.strip() for line in (out.stdout or '').splitlines() if line.strip()]
        for line in lines:
            if line.startswith('List of devices'):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == 'device':
                return parts[0]
    except Exception:
        logging.exception('Failed to execute adb devices')
    return ""


def _ensure_adb_root(serial: str) -> bool:
    """Best-effort attempt to toggle adb root. Returns true if successful."""
    try:
        res = subprocess.run(['adb', '-s', serial, 'root'], capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and "cannot run as root" not in (res.stdout or ""):
            logging.info('adb root: SUCCESS on device %s', serial)
            return True
        logging.debug('adb root: Not available/Production build on device %s', serial)
    except Exception:
        logging.exception('Exception during adb root attempt')
    return False

def start_tcpdump() -> bool:
    """Configures device NFLOG rules and starts background tcpdump securely."""
    logging.info('Setting up tcpdump')

    # 1. Device Discovery
    serial = _get_first_adb_device()
    if not serial:
        logging.error('No responsive adb device found to start tcpdump.')
        return False
    logging.info('Selected target adb device: %s', serial)

    # Check if TCPDump is not already started
    try:
        _ensure_adb_root(serial)

        # 'ps -A' covers newer Android versions, falling back to standard 'ps' for older ones.
        # We look for tcpdump commands targeting nflog:1 while ignoring our own grep command.
        ps_cmd = 'ps -A 2>/dev/null || ps'
        grep_cmd = f'{ps_cmd} | grep "[t]cpdump.*nflog:1"'

        _ensure_adb_root(serial)
        check_proc = subprocess.run(
            ['adb', '-s', serial, 'shell', grep_cmd],
            capture_output=True, text=True, timeout=10
        )

        # If grep found a matching line
        if check_proc.returncode == 0 and check_proc.stdout.strip():
            ps_line = check_proc.stdout.strip().splitlines()[0]
            # Split the ps line to find the PID (usually the 2nd column)
            # Example ps output: root      12345 1     4524   1200  sys_epoll_ b7ee0000 s tcpdump
            parts = ps_line.split()
            if len(parts) >= 2:
                # Android 'ps' puts PID in the second column. Let's verify it's a number.
                live_pid = parts[1] if parts[1].isdigit() else parts[2]  # Fallback just in case of weird columns

                if live_pid.isdigit():
                    logging.info('Live check: tcpdump is actively running on device %s (PID: %s).', serial, live_pid)

                    # Cross-reference with the local PID file
                    pid_file_matches = False
                    if os.path.exists(PID_FILE_PATH):
                        try:
                            with open(PID_FILE_PATH, 'r', encoding='utf-8') as f:
                                lines = [line.strip() for line in f.readlines() if line.strip()]
                                if len(lines) >= 2 and lines[0] == serial and lines[1] == live_pid:
                                    pid_file_matches = True
                        except Exception:
                            logging.warning('PID file existed but was unreadable.')

                    if pid_file_matches:
                        logging.info('Local tracking file matches live device state. Resuming safely.')
                    else:
                        logging.warning('Tracking file missing or out-of-sync. Re-aligning local PID file.')
                        os.makedirs(OUT_DIR, exist_ok=True)
                        with open(PID_FILE_PATH, 'w', encoding='utf-8') as f:
                            f.write(f"{serial}\n{live_pid}\n")

                    return True  # Exit early and leave the running capture alone

    except Exception:
        logging.exception('Error during early ADB device process check; defaulting to clean setup.')

    # 2. Privileges & Firewall Setup
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            _ensure_adb_root(serial)

            # Optimized with explicit su root and thresholds for stability
            ipt_cmd = (
                'su root sh -c "'
                'iptables -t mangle -C OUTPUT -j NFLOG --nflog-group 1 2>/dev/null || '
                'iptables -t mangle -I OUTPUT 1 -j NFLOG --nflog-group 1 --nflog-threshold 10'
                '"'
            )

            res = subprocess.run(
                ['adb', '-s', serial, 'shell', ipt_cmd],
                capture_output=True, text=True, timeout=10
            )

            # Check if iptables command actually succeeded on the device
            if res.returncode == 0:
                logging.info('Verified/Installed iptables NFLOG rule on attempt %d/%d', attempt, max_attempts)
                break  # Success! Break out of the retry loop.
            else:
                error_msg = (res.stderr or res.stdout).strip()
                logging.warning(
                    'Attempt %d/%d failed. iptables exited with code %d. Error: %s',
                    attempt, max_attempts, res.returncode, error_msg
                )

        except Exception as e:
            logging.warning('Attempt %d/%d raised an exception: %s', attempt, max_attempts, str(e))

        # If we haven't broken out of the loop and this wasn't the last attempt, wait before retrying
        if attempt < max_attempts:
            time.sleep(1.5)  # Short cooling-off period before retrying
    else:
        # This block executes ONLY if the loop finishes normally without hitting the 'break' statement
        logging.error('Failed to apply iptables NFLOG rule after %d attempts.', max_attempts)
        return False

    # 3. Clean up any residual tracking artifacts
    if os.path.exists(PID_FILE_PATH):
        try:
            os.remove(PID_FILE_PATH)
        except Exception:
            pass

    # 4. Fire up tcpdump in the background safely
    remote_workdir = '/data/local/tmp'
    # We remove the old pcap first to guarantee our validation loop checks fresh data
    # 4. Fire up tcpdump in the background safely
    start_cmd = (
        f"rm -f {REMOTE_PCAP_PATH} && mkdir -p {remote_workdir} && cd {remote_workdir} && "
        f"su root sh -c 'nohup tcpdump -i nflog:1 -w {REMOTE_PCAP_PATH} > nohup.out 2>&1 & echo $!'"
    )
    try:
        _ensure_adb_root(serial)
        logging.info('Executing tcpdump start command on device %s with command: %s', serial, start_cmd)
        res = subprocess.run(['adb', '-s', serial, 'shell', start_cmd], capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            logging.error('ADB rejected background pipeline command: %s', (res.stderr or res.stdout).strip())
            return False

        output = (res.stdout or '').strip()
        pid = output.splitlines()[-1].strip() if output else ""

        if not pid or not pid.isdigit():
            logging.warning('Could not extract a valid PID from command output: "%s"', output)
            return False

        # Persist tracking metrics (serial + pid)
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(PID_FILE_PATH, 'w', encoding='utf-8') as f:
            f.write(f"{serial}\n{pid}\n")
        logging.info('Background tcpdump initialized (Device: %s, PID: %s)', serial, pid)

    except Exception:
        logging.exception('Failed during tcpdump generation sequence')
        return False

    # 5. Fast-polling File Generation Check (Max 5 seconds wait instead of 15 minutes!)
    for check in range(10):
        time.sleep(0.5)
        try:
            ls_res = subprocess.run(['adb', '-s', serial, 'shell', f'ls -l {REMOTE_PCAP_PATH}'], capture_output=True,
                                    text=True, timeout=5)
            if ls_res.returncode == 0 and (ls_res.stdout or '').strip():
                logging.info('Confirmed: Remote pcap generation verified active.')
                return True
        except Exception:
            pass

    logging.error('tcpdump failed to spawn file descriptor at %s within timeout.', REMOTE_PCAP_PATH)
    return False


def pull_ecapture_files():
    """Pull all files from /data/ecapture on the device into OUT_DIR/ecapture/<serial>/.

    Best-effort: attempts to become root, checks for the remote folder, and pulls its contents.
    Returns True if at least one file was pulled, False otherwise.
    """
    logging.info('Pulling /data/ecapture files from device')
    if not shutil.which('adb'):
        logging.error('adb binary not found in PATH; cannot pull /data/ecapture')
        return False

    # select first connected device
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
            logging.warning('No adb device found to pull /data/ecapture')
            return False
        logging.info('Selected adb device %s to pull /data/ecapture', serial)
    except Exception:
        logging.exception('Failed to run adb devices to select device for /data/ecapture pull')
        return False

    # try to become root (best-effort)
    try:
        res = subprocess.run(['adb', '-s', serial, 'root'], capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            logging.info('adb root: OK for /data/ecapture pull')
        else:
            logging.debug('adb root returned non-zero for /data/ecapture pull: %s', (res.stderr or res.stdout).strip())
    except Exception:
        logging.debug('adb root attempt failed for /data/ecapture pull', exc_info=True)

    remote_dir = '/data/ecapture'

    # Check remote directory exists
    try:
        ls_cmd = ['adb', '-s', serial, 'shell', 'ls', '-l', remote_dir]
        ls_res = subprocess.run(ls_cmd, capture_output=True, text=True, timeout=10)
        if ls_res.returncode != 0 or not (ls_res.stdout or ls_res.stderr):
            logging.info('Remote directory %s does not appear to exist or is empty on device %s', remote_dir, serial)
            return False
    except Exception:
        logging.debug('Exception while checking remote /data/ecapture directory', exc_info=True)

    local_base = os.path.join(OUT_DIR, 'ecapture', serial)
    try:
        os.makedirs(local_base, exist_ok=True)
    except Exception:
        logging.exception('Failed to create local directory for ecapture files: %s', local_base)
        return False

    # Pull the entire directory
    try:
        pull_cmd = ['adb', '-s', serial, 'pull', remote_dir, local_base]
        pull_res = subprocess.run(pull_cmd, capture_output=True, text=True, timeout=300)
        if pull_res.returncode == 0:
            logging.info('Pulled /data/ecapture from device %s to %s', serial, local_base)
            return True
        else:
            logging.warning('adb pull of /data/ecapture failed for device %s: %s', serial, (pull_res.stderr or pull_res.stdout).strip())
            return False
    except Exception:
        logging.exception('Exception while pulling /data/ecapture from device')
        return False


def stop_tcpdump() -> bool:
    """Stops tcpdump via PID / Process name signatures on the target ADB device,

    pulls captured pcaps, optional SSL keylogs, and records package UID maps.
    """
    logging.info('Stopping tcpdump execution layout')

    # -------------------------------------------------------------------------
    # 1. Parse execution markers & select target device
    # -------------------------------------------------------------------------
    serial = _get_first_adb_device()
    pid = None

    if os.path.exists(PID_FILE_PATH):
        try:
            with open(PID_FILE_PATH, 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f if line.strip()]
                if len(lines) > 1:
                    pid = lines[1]
        except Exception:
            logging.exception('Failed parsing data from active pid tracking structure')

    if not serial:
        logging.error('Cannot execute termination request: No valid target devices found.')
        return False

    # -------------------------------------------------------------------------
    # 2. Terminate Process (Ordered fallbacks: root -> pkill -> pid-kill -> su)
    # -------------------------------------------------------------------------
    try:
        root_res = subprocess.run(['adb', '-s', serial, 'root'], capture_output=True, text=True, timeout=10)
        if root_res.returncode == 0:
            logging.info('adb root: OK on device %s', serial)
    except Exception:
        logging.exception('adb root attempt encountered unexpected failure')

    termination_commands = [
        ['adb', '-s', serial, 'shell', 'pkill -2 tcpdump'],
    ]
    if pid:
        termination_commands.append(['adb', '-s', serial, 'shell', f'kill -2 {pid}'])
    termination_commands.append(['adb', '-s', serial, 'shell', 'su', '-c', 'pkill -2 tcpdump'])

    killed = False
    for cmd in termination_commands:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                logging.info('Sent kill signal safely utilizing command: %s', " ".join(cmd))
                killed = True
                break
            else:
                logging.debug('Command [%s] non-zero: %s', " ".join(cmd), (res.stderr or res.stdout).strip())
        except Exception:
            continue

    if not killed:
        logging.warning("Signal requests completed, verifying if process terminated automatically...")

    # Allow buffer space for memory pipes to flush on filesystem
    time.sleep(1.5)

    # -------------------------------------------------------------------------
    # 3. Secure File Retrieval (PCAP + SSL Keylogs)
    # -------------------------------------------------------------------------
    local_pcaps_dir = os.path.join(OUT_DIR, "pcaps")
    os.makedirs(local_pcaps_dir, exist_ok=True)
    local_pcap_file = os.path.join(local_pcaps_dir, f'tcpdump_{serial}.pcap')
    pcap_pulled_successfully = False

    try:
        res = subprocess.run(['adb', '-s', serial, 'pull', REMOTE_PCAP_PATH, local_pcap_file],
                             capture_output=True, text=True, timeout=60)
        if res.returncode == 0 and os.path.exists(local_pcap_file) and os.path.getsize(local_pcap_file) > 0:
            logging.info('Successfully pulled populated tcpdump pcap target to: %s', local_pcap_file)
            pcap_pulled_successfully = True
        else:
            logging.error('Failed pulling pcap or zero-byte file caught. adb logs: %s',
                          (res.stderr or res.stdout).strip())
    except Exception:
        logging.exception('Fatal unexpected termination occurred pulling runtime PCAP assets.')

    _pull_ssl_keylogs(serial, local_pcaps_dir)
    # -------------------------------------------------------------------------
    # 4. Cleanup Execution Tracks
    # -------------------------------------------------------------------------
    if pcap_pulled_successfully:
        if os.path.exists(PID_FILE_PATH):
            try:
                os.remove(PID_FILE_PATH)
            except Exception:
                logging.debug('Failed to remove tracking file %s', PID_FILE_PATH)

        subprocess.run(['adb', '-s', serial, 'shell', f'rm -f {REMOTE_PCAP_PATH}'], capture_output=True, timeout=5)
        return True

    return False


def _pull_ssl_keylogs(serial: str, output_dir: str):
    """Scan known alternative device paths and retrieve available SSL key logs."""
    local_ssl = os.path.join(output_dir, f'sslkeylog_{serial}.log')
    remote_candidates = [
        '/storage/emulated/0/Download/sslkeylog.log',
        '/sdcard/Download/sslkeylog.log',
        '/sdcard/sslkeylog.log',
        '/data/local/tmp/sslkeylog.log',
        '/data/misc/ssl/sslkeylog.log',
    ]

    for remote in remote_candidates:
        try:
            ls_res = subprocess.run(['adb', '-s', serial, 'shell', 'ls', '-l', remote],
                                    capture_output=True, text=True, timeout=5)
            if ls_res.returncode != 0:
                continue

            pull_res = subprocess.run(['adb', '-s', serial, 'pull', remote, local_ssl],
                                      capture_output=True, text=True, timeout=20)
            if pull_res.returncode == 0:
                logging.info('Pulled SSL keylog from device %s -> %s', serial, local_ssl)
                return
        except Exception:
            logging.debug('Exception while attempting to check/pull SSL key from %s', remote, exc_info=True)

    logging.info('No SSL keylog discovered at targeted device vectors; skipping.')


def _collect_package_uid_mapping(serial: str):
    """Retrieve package -> UID lists and store results atomically using safe-write blocks."""
    if not serial:
        serial = _get_first_adb_device()
    try:
        res = subprocess.run(['adb', '-s', serial, 'shell', 'pm', 'list', 'packages', '-U'],
                             capture_output=True, text=True, timeout=20)
        pkg_map = {}
        pm_success = (res.returncode == 0)
        pm_error = None if pm_success else (res.stderr or res.stdout or '').strip()

        if pm_success:
            for line in (res.stdout or '').splitlines():
                line = line.strip()
                if not line.startswith('package:'):
                    continue
                pkg, uid = None, None
                for part in line.split():
                    if part.startswith('package:'):
                        pkg = part.split('package:', 1)[1]
                    if part.startswith('uid:'):
                        try:
                            uid = int(part.split('uid:', 1)[1])
                        except ValueError:
                            pass
                if pkg:
                    pkg_map[pkg] = uid

        mapping_content = {'success': pm_success, 'error': pm_error, 'mapping': pkg_map}
        mapping_file = os.path.join(OUT_DIR, f'package_uids_{serial}.json')
        tmp_path = f"{mapping_file}.tmp"

        for attempt in range(1, 4):
            try:
                os.makedirs(os.path.dirname(mapping_file), exist_ok=True)
                with open(tmp_path, 'w', encoding='utf-8') as mf:
                    json.dump(mapping_content, mf, indent=2)
                    mf.flush()
                    try:
                        os.fsync(mf.fileno())
                    except OSError:
                        pass
                os.replace(tmp_path, mapping_file)
                logging.info('Wrote package->UID mapping to %s (attempt %d)', mapping_file, attempt)
                return
            except Exception as e:
                logging.warning('Attempt %d failed writing package metadata mappings: %s', attempt, e)
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                time.sleep(0.5 * attempt)
    except Exception:
        logging.exception('Failed tracking package data allocations.')

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
    if crash_watcher:
        try:
            crash_watcher.start_crash_watcher(device=None, interval=5.0)
            atexit.register(crash_watcher.stop_crash_watcher)
            logging.info('Started crash watcher (background)')
        except Exception:
            logging.exception('Failed to start crash watcher')

    tcp_dump_attempts = 10
    tcp_dump_delay = 10
    while tcp_dump_attempts > 0:
        try:
            tcp_ok = start_tcpdump()
            if not tcp_ok:
                logging.error('Failed to start tcpdump; aborting this attempt so the experiment retry loop can retry')
                tcp_dump_attempts -= 1
            else:
                break
        except Exception:
            tcp_dump_attempts -= 1
            if tcp_dump_attempts > 0:
                logging.warning('Failed to start background services, will retry in %d seconds (%d attempts left)', tcp_dump_delay, tcp_dump_attempts)
                time.sleep(tcp_dump_delay)
            else:
                logging.error('Failed to start background services after multiple attempts; continuing without them')

def stop_background_services():
    """Stop background services started by start_background_services()."""
    try:
        stop_tcpdump()
    except Exception:
        logging.exception('Error while stopping tcpdump during cleanup')

    # Attempt to pull any capture artifacts (including SSL key logs) from /data/ecapture
    try:
        pull_ecapture_files()
    except Exception:
        logging.exception('Failed to pull /data/ecapture files during cleanup')

    _collect_package_uid_mapping(serial=_get_first_adb_device())

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
    # Collect device info before any setup or test runs. This is best-effort and
    # helps capture device state even if later steps fail.
    try:
        collect_args = ["--outdir", OUT_DIR]
        # forward overrides and set-defaults from main args to the collector
        try:
            overrides = getattr(args, 'device_info_override', None) or getattr(args, 'device-info-override', None)
        except Exception:
            overrides = None
        if overrides:
            for ov in overrides:
                collect_args.extend(['-o', ov])
        try:
            set_defaults_flag = getattr(args, 'device_info_set_defaults', None) or getattr(args, 'device-info-set-defaults', None)
        except Exception:
            set_defaults_flag = None
        if set_defaults_flag:
            collect_args.append('--set-defaults')

        run_script_capture(COLLECT_DEVICE_INFO, args=collect_args, description='Collect device info')
    except Exception:
        logging.exception('Collecting device info failed (will continue)')

    if args.skip_setup:
        logging.info('Skipping device setup as requested (--skip-setup)')
    else:
        setup_devices(mode=args.mode, pcapdroid=getattr(args, 'pcapdroid', False), pcap_http_port=args.pcap_http_port, socks5_address=args.socks5_address)

    start_experiment(mode=args.mode, test_only_one=(getattr(args, 'test-only-one', False) or getattr(args, 'test_only_one', False) or args.test_only_one), skip_install=getattr(args, 'skip_install', False))

def main():
    args = parse_args()
    results_dir = os.path.join(BASE_DIR, 'out')
    os.makedirs(results_dir, exist_ok=True)

    # Enforce a global maximum runtime for this process. When the duration is
    # exceeded we attempt graceful shutdown (stop background services) and write
    # the experiment summary with the results collected so far.
    start_time = time.time()
    max_duration_hours = float(getattr(args, 'max_duration_hours', 24.0))
    max_seconds = max(0.0, max_duration_hours * 3600.0)
    # Initialize module-global timeout helpers used by subprocess wrappers
    init_global_timeout(start_time, max_seconds)

    def _check_timeout_and_exit(current_attempt=None, total_attempts=None):
        if max_seconds <= 0:
            return
        elapsed = time.time() - start_time
        if elapsed >= max_seconds:
            # Signal the global timeout to the outer loop so it can perform
            # orderly shutdown and write results. Do not exit here.
            logging.warning('Maximum runtime of %.2f hours exceeded (elapsed %.2f hours). Signalling timeout.', max_duration_hours, elapsed / 3600.0)
            raise GlobalTimeoutReached('Maximum runtime exceeded', attempt=current_attempt, attempts=total_attempts)


    attempts = max(1, int(getattr(args, 'retries', 30)))
    retry_delay = int(getattr(args, 'retry_delay', 600))
    logging.info(f"Set Attempts to {attempts} attempts and Retry Delay {retry_delay} seconds.")
    # Single unified retry loop: perform the entire preflight and experiment in one attempt
    for attempt in range(1, attempts + 1):
        logging.info('Full-run attempt %d/%d starting...', attempt, attempts)
        # Check timeout before starting each attempt
        _check_timeout_and_exit(current_attempt=attempt, total_attempts=attempts)
        try:
            # 1) Ensure adb available
            ensure_adb_available(results_dir)
            _check_timeout_and_exit(current_attempt=attempt, total_attempts=attempts)

            # 2) Start background helpers (tcpdump, crash watcher)
            start_background_services()
            _check_timeout_and_exit(current_attempt=attempt, total_attempts=attempts)

            # 3) Preflight checks: boot, launcher, connectivity
            do_preflight(results_dir)
            _check_timeout_and_exit(current_attempt=attempt, total_attempts=attempts)

            # 4) Setup devices and run experiment
            setup_and_run_experiment(args)
            _check_timeout_and_exit(current_attempt=attempt, total_attempts=attempts)

            # Success: cleanup, write summary and exit
            stop_background_services()
            logging.info('Full-run attempt %d/%d completed successfully', attempt, attempts)
            try:
                write_experiment_summary(results_dir, overall_success=True, attempt=attempt, attempts=attempts)
            except Exception:
                logging.exception('Failed to write experiment summary on success')
            return

        except GlobalTimeoutReached as e:
            logging.error('Global timeout reached: %s', e)
            # Attempt graceful shutdown and write partial results
            try:
                stop_background_services()
            except Exception:
                logging.exception('Error while stopping background services after timeout')
            try:
                write_experiment_summary(results_dir, overall_success=False, attempt=e.attempt or attempt, attempts=e.attempts or attempts)
            except Exception:
                logging.exception('Failed to write experiment summary after timeout')
            # write an explicit timeout marker
            try:
                timeout_file = os.path.join(results_dir, 'experiment_timed_out.json')
                _write_json(timeout_file, {'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat() + 'Z', 'attempt': e.attempt, 'attempts': e.attempts})
            except Exception:
                logging.exception('Failed to write timeout marker')
            # Exit with non-zero to indicate timeout
            sys.exit(2)

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
