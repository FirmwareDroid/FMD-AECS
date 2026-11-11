#!/usr/bin/env python3
"""
Ape testing tool wrapper.

Pushes ape.jar (and the ape shell script) to the Android device via ADB and
then runs the Ape search-based GUI testing tool.

Usage:
    python3 run_ape.py -p <package> [options]

Examples:
    python3 run_ape.py -p com.example.app --running-minutes 30
    python3 run_ape.py -p com.example.app --serial emulator-5554 --strategy random
"""

import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
import shlex
import subprocess
import sys
import time
import threading
import hashlib
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from common import get_adb_cmd
from test_results import append_run

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APE_DIR = os.path.join(BASE_DIR, 'tools', 'ape-bin')
APE_JAR_LOCAL = os.path.join(APE_DIR, 'ape.jar')
APE_SCRIPT_LOCAL = os.path.join(APE_DIR, 'ape')

DEVICE_TMP = '/data/local/tmp/'
DEVICE_APE_JAR = DEVICE_TMP + 'ape.jar'
DEVICE_APE_SCRIPT = DEVICE_TMP + 'ape'

# Default output directory for start_apps summaries
DEFAULT_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
os.makedirs(DEFAULT_OUT_DIR, exist_ok=True)
log_file_path = os.path.join(DEFAULT_OUT_DIR, 'ape_start.log')
root_logger = logging.getLogger()
already_have = False
for h in list(root_logger.handlers):
    try:
        if isinstance(h, logging.FileHandler) and os.path.abspath(getattr(h, 'baseFilename', '')) == os.path.abspath(log_file_path):
            already_have = True
            break
    except Exception:
        continue
if not already_have:
    fh = logging.FileHandler(log_file_path, mode='a', encoding='utf-8')
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    root_logger.addHandler(fh)


def _adb(serial=None):
    return get_adb_cmd(serial)


def push_ape(serial=None):
    """Push ape.jar and ape shell launcher to the device."""
    adb = _adb(serial)
    logging.info("Pushing ape.jar to device…")
    subprocess.check_call(adb + ['push', APE_JAR_LOCAL, DEVICE_TMP])
    logging.info("Pushing ape shell script to device…")
    subprocess.check_call(adb + ['push', APE_SCRIPT_LOCAL, DEVICE_TMP])
    subprocess.check_call(adb + ['shell', f'chmod 755 {DEVICE_APE_SCRIPT}'])
    logging.info("Ape binaries deployed to device.")


def start_package(package, serial=None, wait_seconds=2, use_monkey=False):
    """Start the package on device (bring to foreground).

    Uses `am start` with the resolved main activity first so it does not collide
    with Ape's active UiAutomation connection while Ape is running. Only falls
    back to `monkey` when `use_monkey=True` (before Ape starts).
    """
    adb = _adb(serial)
    logging.info('Starting package %s on device (bringing to foreground)...', package)
    try:
        res = subprocess.run(
            adb + ['shell', 'cmd', 'package', 'resolve-activity', '--brief', package],
            capture_output=True, text=True, timeout=10
        )
        if res.returncode == 0 and res.stdout:
            lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip() and '/' in ln]
            if lines:
                comp = lines[-1].split()[0]
                am_res = subprocess.run(
                    adb + ['shell', 'am', 'start', '-n', comp],
                    capture_output=True, text=True, timeout=10
                )
                if am_res.returncode == 0 and 'Error:' not in (am_res.stdout or '') and 'Exception' not in (am_res.stderr or ''):
                    time.sleep(wait_seconds)
                    return True
        # Fallback: generic MAIN/LAUNCHER intent via am start
        am_generic = subprocess.run(
            adb + ['shell', 'am', 'start', '-a', 'android.intent.action.MAIN', '-c', 'android.intent.category.LAUNCHER', package],
            capture_output=True, text=True, timeout=10
        )
        if am_generic.returncode == 0 and 'Error:' not in (am_generic.stdout or ''):
            time.sleep(wait_seconds)
            return True
    except Exception as e:
        logging.debug('am start resolution failed for %s: %s', package, e)

    if use_monkey:
        try:
            subprocess.check_call(
                adb + ['shell', 'monkey', '-p', package, '-c', 'android.intent.category.LAUNCHER', '1'],
                timeout=15
            )
            time.sleep(wait_seconds)
            logging.info('Start command for %s issued via monkey; waited %ss for app to stabilize', package, wait_seconds)
            return True
        except Exception as e:
            logging.warning('Failed to start package %s via monkey: %s', package, e)
    return False


def run_ape(package, running_minutes=300, strategy='sata', serial=None):
    """Execute Ape on the device and return its exit code."""
    adb = _adb(serial)
    cmd = (
        adb
        + ['shell',
           f'CLASSPATH={DEVICE_APE_JAR}',
           '/system/bin/app_process',
           DEVICE_TMP,
           'com.android.commands.monkey.Monkey',
           '-p', package,
           '--running-minutes', str(running_minutes),
           '--ape', strategy,
           '--ignore-crashes',
           '--ignore-timeouts',
           '--ignore-security-exceptions',
           "-s", "12345"]
    )
    logging.info("Running Ape on package: %s (strategy=%s, minutes=%d). CMD: %s", package, strategy, running_minutes, cmd)
    # Start Ape (monkey) as a subprocess so we can monitor foreground package
    cmd = [str(a) for a in cmd]
    proc = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    stop_watcher = threading.Event()

    def get_foreground_package():
        """Return the package name currently in foreground, or None on error."""
        adb_cmd = adb + ['shell']
        # Try a few common dumpsys queries to get the resumed/focused activity
        queries = [
            "dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp'",
            "dumpsys activity activities | grep mResumedActivity",
            "dumpsys activity top | grep ACTIVITY",
        ]
        for q in queries:
            try:
                res = subprocess.run(adb_cmd + [q], capture_output=True, text=True, timeout=5, shell=False)
                out = (res.stdout or '').strip()
                if not out:
                    continue
                # Try to extract package from typical patterns
                # e.g. mCurrentFocus=Window{... com.android.settings/.Settings}
                import re
                m = re.search(r'([\w\.]+)\/(?:[\w\.$]+)', out)
                if m:
                    return m.group(1)
                # alternative: look for package= in dumpsys
                m2 = re.search(r'package=([\w\.]+)', out)
                if m2:
                    return m2.group(1)
            except Exception:
                continue
        return None

    def foreground_watcher():
        """Ensure the target package stays in foreground while proc is running."""
        check_interval = 1.0
        start_wall = time.time()
        max_wall_seconds = max(120, int(running_minutes) * 60 + 90)
        # Screenshot-based movement detection: take a screenshot every N checks and
        # compare hashes. If the screen does not change for more than
        # no_change_seconds, treat as frozen/closed and attempt restart.
        screenshot_every = 2  # take screenshot every 2 watcher iterations
        no_change_seconds = 8
        no_change_threshold = int(no_change_seconds / check_interval)
        screenshot_counter = 0
        last_screen_hash = None
        unchanged_iterations = 0
        while not stop_watcher.is_set():
            if time.time() - start_wall > max_wall_seconds:
                logging.warning('Ape process exceeded max wall time (%ds); terminating subprocess.', max_wall_seconds)
                try:
                    proc.terminate()
                except Exception:
                    pass
                break
            try:
                fg = get_foreground_package()
                # If the foreground package is different, bring target back
                if fg and fg != package:
                    logging.info('Foreground changed to %s; bringing %s back to foreground', fg, package)
                    # Try multiple quick relaunch attempts to ensure app returns to foreground
                    for attempt in range(3):
                        try:
                            start_package(package, serial=serial, wait_seconds=1)
                        except Exception:
                            logging.exception('Failed to restart package %s on attempt %d', package, attempt + 1)
                        # re-check foreground and break early if restored
                        try:
                            new_fg = get_foreground_package()
                            if new_fg == package:
                                logging.info('Package %s restored to foreground on attempt %d', package, attempt + 1)
                                break
                        except Exception:
                            pass

                # Additionally, detect if the app process was stopped entirely (ape may have closed it)
                try:
                    adb_cmd = _adb(serial) + ['shell', 'pidof', package]
                    pid_res = subprocess.run(adb_cmd, capture_output=True, text=True, timeout=5)
                    if pid_res.returncode != 0 or not (pid_res.stdout or pid_res.stderr).strip():
                        # pidof not found or package not running — try a few quick starts
                        logging.info('Package %s does not appear to be running (pidof returned %s). Attempting to start.', package, pid_res.returncode)
                        for attempt in range(3):
                            try:
                                start_package(package, serial=serial, wait_seconds=1)
                            except Exception:
                                logging.exception('Failed to start package %s on attempt %d', package, attempt + 1)
                            try:
                                new_fg = get_foreground_package()
                                if new_fg == package:
                                    logging.info('Package %s started and in foreground on attempt %d', package, attempt + 1)
                                    break
                            except Exception:
                                pass
                except Exception:
                    # pidof may not exist; try a generic ps|grep fallback to detect a running process
                    try:
                        adb_cmd = _adb(serial) + ['shell', 'ps -A | grep -F "' + package + '"']
                        res = subprocess.run(adb_cmd, capture_output=True, text=True, timeout=5)
                        out = (res.stdout or '').strip()
                        if not out:
                            logging.info('Fallback ps check shows package %s not running; attempting to start', package)
                            try:
                                start_package(package, serial=serial, wait_seconds=1)
                            except Exception:
                                logging.exception('Failed to start package %s after ps fallback detected it not running', package)
                    except Exception:
                        # give up on detection for this iteration
                        logging.debug('Could not determine running state of %s (pidof/ps checks failed)', package)
                # Screenshot movement detection
                try:
                    screenshot_counter += 1
                    if screenshot_counter >= screenshot_every:
                        screenshot_counter = 0
                        # capture screenshot bytes via adb
                        try:
                            adb_cmd = _adb(serial) + ['exec-out', 'screencap', '-p']
                            cap = subprocess.run(adb_cmd, capture_output=True, timeout=10)
                            if cap.returncode == 0 and cap.stdout:
                                h = hashlib.md5(cap.stdout).hexdigest()
                                if last_screen_hash is None:
                                    last_screen_hash = h
                                    unchanged_iterations = 0
                                else:
                                    if h == last_screen_hash:
                                        unchanged_iterations += screenshot_every
                                    else:
                                        last_screen_hash = h
                                        unchanged_iterations = 0
                                # If screen unchanged for threshold, attempt restart
                                if unchanged_iterations >= no_change_threshold:
                                    logging.info('No screen movement detected for %ds; restarting package %s', no_change_seconds, package)
                                    try:
                                        start_package(package, serial=serial, wait_seconds=1)
                                    except Exception:
                                        logging.exception('Failed to restart package %s after no-movement detection', package)
                                    unchanged_iterations = 0
                        except Exception:
                            logging.debug('Screenshot capture failed; skipping movement detection this iteration')
                except Exception:
                    logging.debug('Movement detection error', exc_info=True)
                # if fg is None, we couldn't determine — skip but continue
            except Exception:
                logging.debug('Foreground watcher exception', exc_info=True)
            # If process exited, stop
            if proc.poll() is not None:
                break
            stop_watcher.wait(check_interval)

    watcher_thread = threading.Thread(target=foreground_watcher, name='ape-foreground-watcher', daemon=True)
    watcher_thread.start()

    # Stream output and wait for process to finish
    try:
        for line in proc.stdout:
            if line:
                logging.info('[APE] %s', line.rstrip())
        ret = proc.wait()
    finally:
        stop_watcher.set()
        watcher_thread.join(timeout=5)

    return ret


def main():
    parser = argparse.ArgumentParser(description='Run Ape search-based GUI testing on an Android device')
    parser.add_argument('-p', '--package', required=True, help='Package name to test')
    parser.add_argument('--running-minutes', type=int, default=1, help='Test duration in minutes (default: 1)')
    parser.add_argument('--strategy', default='sata', choices=['sata', 'random'],
                        help='Ape exploration strategy (default: sata)')
    parser.add_argument('--serial', default=os.environ.get('ANDROID_SERIAL'),
                        help='Device serial (default: ANDROID_SERIAL env var)')
    parser.add_argument('--no-push', action='store_true',
                        help='Skip pushing binaries (ape.jar and ape shell script) to the device')
    args = parser.parse_args()

    if not os.path.exists(APE_JAR_LOCAL):
        logging.error("ape.jar not found at %s. Run install_tools.py first.", APE_JAR_LOCAL)
        sys.exit(1)

    if not args.no_push:
        try:
            push_ape(args.serial)
        except Exception as e:
            logging.exception('Failed to push Ape binaries to device: %s', e)
            summary = {
                'total_packages': 1, 'started': 0, 'failed': 1, 'skipped': 0,
                'started_by_script': 0, 'started_packages': [], 'failed_packages': [args.package],
            }
            failures = [{'package': args.package, 'reason': 'ape_push_failed', 'detail': str(e)}]
            try:
                append_run('ape', summary, failures, out_dir=os.path.join(BASE_DIR, 'out'))
            except Exception:
                pass
            sys.exit(2)

    # Ensure the package is started and in foreground before running Ape
    try:
        started = start_package(args.package, serial=args.serial, wait_seconds=2, use_monkey=True)
        if not started:
            logging.warning('Proceeding to run Ape even though package %s may not be in foreground', args.package)
    except Exception:
        logging.exception('Error while attempting to start package %s; continuing to run Ape', args.package)

    ret = run_ape(args.package, args.running_minutes, args.strategy, args.serial)

    # Prepare summary similar to app_start_summary.json
    summary = {
        'total_packages': 1,
        'started': 1 if ret == 0 else 0,
        'failed': 0 if ret == 0 else 1,
        'skipped': 0,
        'started_by_script': 1 if ret == 0 else 0,
        'started_packages': [args.package] if ret == 0 else [],
        'failed_packages': [] if ret == 0 else [args.package],
    }
    failures = []
    if ret != 0:
        failures.append({'package': args.package, 'reason': 'ape_failed', 'detail': f'return_code={ret}'})

    # write tool-specific summary file
    try:
        out_dir = os.path.join(BASE_DIR, 'out')
        append_run('ape', summary, failures, out_dir=out_dir)
    except Exception:
        logging.exception('Failed to write ape summary')

    sys.exit(ret)


if __name__ == '__main__':
    main()
