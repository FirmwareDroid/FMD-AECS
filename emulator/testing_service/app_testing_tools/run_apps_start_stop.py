#!/usr/bin/env python3
"""
Start all installed apps on a connected Android device and report statistics.
This script will:
 - list installed packages using adb
 - for each package, try to start it (monkey first, then resolved main activity)
 - detect if the app started via `pidof`
 - collect successes and failures and a frequency analysis of failure reasons
 - write summary.json and failures.json into current working directory

Additionally, you can control how many monkey events are sent when attempting a monkey launch
using the --monkey-events / -m CLI option (default 1), and other monkey options like
--monkey-seed, --monkey-throttle, and raw extra monkey args via --monkey-extra.

Usage: python3 start_apps.py [-s SERIAL] [-d DELAY] [--monkey-events N] [--monkey-seed N] [--monkey-extra ...]
"""

import argparse
import json
import subprocess
import time
import logging
from collections import Counter, defaultdict
from shutil import which
from typing import Dict, Any, List, Optional
import os

# Default output directory for start_apps summaries
DEFAULT_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

ADB = which('adb') or 'adb'


def run_adb(cmd_args, capture_output=True, text=True):
    try:
        result = subprocess.run([ADB] + cmd_args, capture_output=capture_output, text=text)
        return result.returncode, result.stdout or '', result.stderr or ''
    except FileNotFoundError:
        raise RuntimeError(f"adb not found. Make sure Android platform-tools are installed and adb is in PATH.")


def list_packages(serial=None):
    cmd = []
    if serial:
        cmd += ['-s', serial]
    cmd += ['shell', 'pm', 'list', 'packages']
    rc, out, err = run_adb(cmd)
    if rc != 0:
        logging.error('Failed to list packages: %s', err.strip())
        return []
    packages = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('package:'):
            packages.append(line.split(':', 1)[1].strip())
    return packages


def pid_of(package, serial=None):
    cmd = []
    if serial:
        cmd += ['-s', serial]
    cmd += ['shell', 'pidof', package]
    rc, out, err = run_adb(cmd)
    if rc != 0:
        return None
    out = out.strip()
    if not out:
        return None
    return out


def monkey_launch(package, serial=None, events=1, monkey_opts: Optional[Dict[str, Any]] = None):
    """
    Use monkey to attempt to start a package's main launcher activity.

    :param package: package name to target
    :param serial: optional device serial for adb -s
    :param events: number of monkey events to send (int >= 1)
    :param monkey_opts: optional dict of monkey options (seed, throttle, flags, extra args)
    :return: (success: bool, combined_output: str)
    """
    if events is None:
        events = 1
    try:
        events_int = int(events)
    except Exception:
        events_int = 1
    if events_int < 1:
        events_int = 1

    if monkey_opts is None:
        monkey_opts = {}

    cmd = []
    if serial:
        cmd += ['-s', serial]

    # Build monkey command tokens
    monkey_cmd = ['shell', 'monkey', '-p', package, '-c', 'android.intent.category.LAUNCHER']

    # boolean flags
    if monkey_opts.get('ignore_crashes'):
        monkey_cmd.append('--ignore-crashes')
    if monkey_opts.get('ignore_timeouts'):
        monkey_cmd.append('--ignore-timeouts')
    if monkey_opts.get('ignore_security_exceptions'):
        monkey_cmd.append('--ignore-security-exceptions')
    if monkey_opts.get('monitor_native_crashes'):
        monkey_cmd.append('--monitor-native-crashes')
    if monkey_opts.get('ignore_native_crashes'):
        monkey_cmd.append('--ignore-native-crashes')
    if monkey_opts.get('kill_process_after_error'):
        monkey_cmd.append('--kill-process-after-error')
    if monkey_opts.get('hprof'):
        monkey_cmd.append('--hprof')

    # seed
    seed = monkey_opts.get('seed')
    if seed is not None:
        try:
            monkey_cmd += ['-s', str(int(seed))]
        except Exception:
            pass

    # throttle
    throttle = monkey_opts.get('throttle')
    if throttle is not None:
        try:
            monkey_cmd += ['--throttle', str(int(throttle))]
        except Exception:
            pass
    if monkey_opts.get('randomize_throttle'):
        monkey_cmd.append('--randomize-throttle')

    # append any raw extra args (list of tokens)
    extra = monkey_opts.get('extra')
    if extra:
        # ensure list of strings
        monkey_cmd += [str(x) for x in extra]

    # finally append the event count
    monkey_cmd.append(str(events_int))

    cmd += monkey_cmd

    logging.debug('Running adb command: %s', ' '.join(cmd))
    rc, out, err = run_adb(cmd)
    return rc == 0, out + err


def resolve_main_activity(package, serial=None):
    # Use cmd package resolve-activity --components to try to find a launchable component
    cmd = []
    if serial:
        cmd += ['-s', serial]
    cmd += ['shell', 'cmd', 'package', 'resolve-activity', '--components', package]
    rc, out, err = run_adb(cmd)
    if rc != 0:
        return None
    # Look for 'component=' in output and sanitize it. Example lines can include
    # trailing attributes like 'priority=0' which must not be passed to `am start -n`.
    for line in out.splitlines():
        if 'component=' in line:
            # component=com.example/.MainActivity or component=com.example/com.example.MainActivity
            part = line.split('component=', 1)[1].strip()
            if not part:
                continue
            # take only the first whitespace-separated token to drop things like 'priority=0'
            token = part.split()[0].strip()
            # token may be like com.example/.MainActivity or com.example/com.example.MainActivity
            # ensure it contains a slash; if not, try to be conservative and return as-is
            return token
    # fallback: try brief resolve-activity
    cmd = []
    if serial:
        cmd += ['-s', serial]
    cmd += ['shell', 'cmd', 'package', 'resolve-activity', '--brief', package]
    rc, out, err = run_adb(cmd)
    if rc == 0 and out:
        # brief output may also include extra tokens; sanitize similarly
        first = out.strip().splitlines()[0].strip()
        if first:
            return first.split()[0].strip()
    return None


def am_start(component, serial=None):
    cmd = []
    if serial:
        cmd += ['-s', serial]
    cmd += ['shell', 'am', 'start', '-n', component]
    rc, out, err = run_adb(cmd)
    return rc == 0, out + err


def am_force_stop(package, serial=None):
    cmd = []
    if serial:
        cmd += ['-s', serial]
    cmd += ['shell', 'am', 'force-stop', package]
    rc, out, err = run_adb(cmd)
    return rc == 0, out + err


def pretty_print_summary(summary: Dict[str, Any], failures: List[Dict[str, Any]], max_examples: int = 3) -> None:
    """Print a human-friendly summary to stdout.

    :param summary: the out_summary dict returned by start_packages
    :param failures: list of failures entries
    """
    print('\n=== Start Apps Summary ===')
    s = summary.get('summary', {})
    print(f"Total packages: {s.get('total_packages', 0)}")
    print(f"Started: {s.get('started', 0)}")
    print(f"Failed: {s.get('failed', 0)}")
    print(f"Started by script: {s.get('started_by_script', 0)}")
    # Print the actual package names for successes and failures if available
    started_pkgs = s.get('started_packages') or summary.get('started_packages') or []
    failed_pkgs = s.get('failed_packages') or summary.get('failed_packages') or []
    if started_pkgs:
        print('\nSuccessful packages:')
        for p in started_pkgs:
            print(f' - {p}')
    if failed_pkgs:
        print('\nFailed packages:')
        for p in failed_pkgs:
            print(f' - {p}')

    print('\n--- Failure frequency ---')
    ff = summary.get('failure_frequency', {})
    if not ff:
        print('No failures reported')
    else:
        # sort by frequency desc
        for reason, cnt in sorted(ff.items(), key=lambda kv: kv[1], reverse=True):
            print(f"{reason}: {cnt}")

    if failures:
        print(f"\n--- Failure examples (up to {max_examples} each) ---")
        # group by reason
        grouped = defaultdict(list)
        for f in failures:
            grouped[f.get('reason', 'unknown')].append(f)
        for reason, items in grouped.items():
            print(f"\nReason: {reason} (examples: {len(items)})")
            for ex in items[:max_examples]:
                pkg = ex.get('package')
                detail = ex.get('detail')
                print(f" - {pkg}: {str(detail)[:400]}")
    print('=========================\n')


def get_apk_path(package: str, serial: Optional[str] = None) -> Optional[str]:
    """Return the device APK path for a package or None if not found.

    Uses `adb shell pm path <package>` which typically returns lines like:
      package:/data/app/com.example-1/base.apk
    """
    cmd = []
    if serial:
        cmd += ['-s', serial]
    cmd += ['shell', 'pm', 'path', package]
    rc, out, err = run_adb(cmd)
    if rc != 0 or not out:
        return None
    # take first line and strip leading 'package:'
    first = out.splitlines()[0].strip()
    if first.startswith('package:'):
        return first.split('package:', 1)[1]
    return first


def start_packages(serial=None, delay=0.3, stop_after_start=False, stop_delay=1.0, package: Optional[str] = None, monkey_events: int = 1, monkey_opts: Optional[Dict[str, Any]] = None):
    """Start packages. If `package` is provided, only that package will be attempted.

    Returns (out_summary, failures)
    """
    if package:
        packages = [package]
        total = 1
        logging.info('Targeting single package: %s', package)
    else:
        packages = list_packages(serial)
        total = len(packages)
        logging.info('Found %d packages to try to start.', total)

    success = []
    failures = []
    failure_reasons = Counter()
    failure_examples = defaultdict(list)
    started_by_script = []

    for pkg in packages:
        logging.info('Processing package: %s', pkg)

        # detect overlays (auto_generated/resource overlay) and skip
        apk_path = get_apk_path(pkg, serial)
        is_overlay = False
        lower_pkg = pkg.lower()
        if 'overlay' in lower_pkg or 'auto_generated' in lower_pkg or 'auto-generated' in lower_pkg:
            is_overlay = True
        if apk_path:
            lower_path = apk_path.lower()
            if '/overlay/' in lower_path or lower_path.startswith('/overlay') or '/product/overlay' in lower_path or '/vendor/overlay' in lower_path:
                is_overlay = True
        if is_overlay:
            reason = 'Overlay'
            logging.info('Skipping overlay package: %s (apk_path=%s)', pkg, apk_path)
            failure_reasons[reason] += 1
            failure_examples[reason].append({'package': pkg, 'apk_path': apk_path})
            failures.append({'package': pkg, 'reason': reason, 'detail': apk_path or ''})
            continue

        # if already running, consider success
        pid_before = pid_of(pkg, serial)
        if pid_before:
            logging.info('Package %s already running (pid=%s).', pkg, pid_before)
            success.append(pkg)
            time.sleep(delay)
            continue

        # try monkey launch
        ok, output = monkey_launch(pkg, serial, events=monkey_events, monkey_opts=monkey_opts)
        time.sleep(delay)
        pid_after = pid_of(pkg, serial)
        if pid_after:
            logging.info('Monkey launched %s (pid=%s).', pkg, pid_after)
            success.append(pkg)
            started_by_script.append(pkg)
            # optionally stop it again
            if stop_after_start:
                logging.info('Will stop %s after %.2fs', pkg, stop_delay)
                time.sleep(stop_delay)
                stopped_ok, stopped_out = am_force_stop(pkg, serial)
                if stopped_ok:
                    logging.info('Stopped %s (force-stop).', pkg)
                else:
                    logging.warning('Failed to stop %s: %s', pkg, stopped_out.strip())
            continue

        # try resolving main activity and am start
        main = resolve_main_activity(pkg, serial)
        if main and "No activity found" not in main:
            logging.info('Resolved main activity for %s -> %s', pkg, main)
            ok2, out2 = am_start(main, serial)
            time.sleep(delay)
            pid_after = pid_of(pkg, serial)
            if pid_after:
                logging.info('Started %s via am start (pid=%s).', pkg, pid_after)
                success.append(pkg)
                started_by_script.append(pkg)
                if stop_after_start:
                    logging.info('Will stop %s after %.2fs', pkg, stop_delay)
                    time.sleep(stop_delay)
                    stopped_ok, stopped_out = am_force_stop(pkg, serial)
                    if stopped_ok:
                        logging.info('Stopped %s (force-stop).', pkg)
                    else:
                        logging.warning('Failed to stop %s: %s', pkg, stopped_out.strip())
                continue
            else:
                reason = 'start_no_pid'
                failure_reasons[reason] += 1
                failure_examples[reason].append({'package': pkg, 'component': main, 'am_output': out2})
                failures.append({'package': pkg, 'reason': reason, 'detail': out2})
                logging.warning('am start did not create a pid for %s', pkg)
                continue
        else:
            # no main activity resolved
            reason = 'no_main_activity'
            failure_reasons[reason] += 1
            failure_examples[reason].append({'package': pkg, 'output': output})
            failures.append({'package': pkg, 'reason': reason, 'detail': output})
            logging.warning('No main activity found for %s', pkg)
            continue

    summary = {
        'total_packages': total,
        'started': len(success),
        'failed': len(failures),
        'started_by_script': len(started_by_script),
        'started_packages': success,
        'failed_packages': [f.get('package') for f in failures]
    }

    logging.info('Finished processing packages. Started=%d Failed=%d. Started by script=%d', len(success), len(failures), len(started_by_script))

    # frequency analysis for failures
    freq = dict(failure_reasons)

    summary['failure_frequency'] = freq
    summary["failures"] = failures

    try:
        os.makedirs(DEFAULT_OUT_DIR, exist_ok=True)
        out_path = os.path.join(DEFAULT_OUT_DIR, 'app_start_summary.json')
        with open(out_path, 'w') as f:
            json.dump(summary, f, indent=2)
        logging.info('Wrote summary to %s', out_path)
    except Exception as e:
        logging.error('Failed to write output files: %s', e)

    # also print detailed frequency
    logging.info('Failure frequency: %s', freq)
    if failures:
        logging.info('Examples of failures per reason:')
        for reason, examples in failure_examples.items():
            logging.info('  %s: %d example(s). Showing up to 3', reason, len(examples))
            for ex in examples[:3]:
                logging.info('    %s', ex)

    return summary, failures


def main():
    parser = argparse.ArgumentParser(description='Start all installed Android packages and collect stats')
    parser.add_argument('-s', '--serial', help='ADB device serial to target (optional)')
    parser.add_argument('-d', '--delay', type=float, default=1, help='Delay between package operations in seconds')
    parser.add_argument('--stop-after-start', action='store_true', help='If set, force-stop apps that the script started after --stop-delay seconds')
    parser.add_argument('--stop-delay', type=float, default=1.0, help='Seconds to wait before stopping apps when --stop-after-start is set')
    parser.add_argument('--pretty', dest='pretty', action='store_true', default=True, help='Pretty-print output summary to stdout')
    parser.add_argument('--no-pretty', dest='pretty', action='store_false', help='Do not pretty-print output summary')
    # Accept package either as positional argument or via --package flag to support both usages
    parser.add_argument('package', nargs='?', help='(optional) single package to start')
    parser.add_argument('-p', '--package', dest='package_flag', help='(optional) single package to start (alternative flag)')
    parser.add_argument('-m', '--monkey-events', dest='monkey_events', type=int, default=1, help='Number of monkey events to send when attempting monkey launch (default 1)')

    # Monkey configuration options
    parser.add_argument('--monkey-seed', dest='monkey_seed', type=int, help='Random seed for monkey (-s)')
    parser.add_argument('--monkey-throttle', dest='monkey_throttle', type=int, help='Monkey --throttle in milliseconds')
    parser.add_argument('--monkey-randomize-throttle', dest='monkey_randomize_throttle', action='store_true', help='Use --randomize-throttle with monkey')
    parser.add_argument('--monkey-ignore-crashes', dest='monkey_ignore_crashes', action='store_true', help='Use --ignore-crashes with monkey')
    parser.add_argument('--monkey-ignore-timeouts', dest='monkey_ignore_timeouts', action='store_true', help='Use --ignore-timeouts with monkey')
    parser.add_argument('--monkey-ignore-security-exceptions', dest='monkey_ignore_security_exceptions', action='store_true', help='Use --ignore-security-exceptions with monkey')
    parser.add_argument('--monkey-monitor-native-crashes', dest='monkey_monitor_native_crashes', action='store_true', help='Use --monitor-native-crashes with monkey')
    parser.add_argument('--monkey-ignore-native-crashes', dest='monkey_ignore_native_crashes', action='store_true', help='Use --ignore-native-crashes with monkey')
    parser.add_argument('--monkey-kill-process-after-error', dest='monkey_kill_process_after_error', action='store_true', help='Use --kill-process-after-error with monkey')
    parser.add_argument('--monkey-hprof', dest='monkey_hprof', action='store_true', help='Use --hprof with monkey')
    parser.add_argument('--monkey-extra', dest='monkey_extra', nargs='*', help='Extra raw monkey arguments (tokens) to append, e.g. --monkey-extra --pct-touch 50')

    args = parser.parse_args()

    # choose package from either positional or flag
    package_to_start = args.package_flag or args.package

    if not which('adb'):
        logging.error('adb not found in PATH. Aborting.')
        return

    # validate monkey events
    if args.monkey_events is None or args.monkey_events < 1:
        logging.error('Invalid --monkey-events value: %s. It must be an integer >= 1.', args.monkey_events)
        return

    # validate throttle if provided
    if args.monkey_throttle is not None and args.monkey_throttle < 0:
        logging.error('Invalid --monkey-throttle value: %s. It must be >= 0.', args.monkey_throttle)
        return

    # validate seed if provided
    if args.monkey_seed is not None:
        try:
            int(args.monkey_seed)
        except Exception:
            logging.error('Invalid --monkey-seed value: %s. It must be an integer.', args.monkey_seed)
            return

    monkey_opts = {
        'seed': args.monkey_seed,
        'throttle': args.monkey_throttle,
        'randomize_throttle': bool(args.monkey_randomize_throttle),
        'ignore_crashes': bool(args.monkey_ignore_crashes),
        'ignore_timeouts': bool(args.monkey_ignore_timeouts),
        'ignore_security_exceptions': bool(args.monkey_ignore_security_exceptions),
        'monitor_native_crashes': bool(args.monkey_monitor_native_crashes),
        'ignore_native_crashes': bool(args.monkey_ignore_native_crashes),
        'kill_process_after_error': bool(args.monkey_kill_process_after_error),
        'hprof': bool(args.monkey_hprof),
        'extra': args.monkey_extra or []
    }

    try:
        summary, failures = start_packages(args.serial, args.delay, stop_after_start=args.stop_after_start, stop_delay=args.stop_delay, package=package_to_start, monkey_events=args.monkey_events, monkey_opts=monkey_opts)
        if args.pretty:
            pretty_print_summary(summary, failures)
    except RuntimeError as e:
        logging.error('Runtime error: %s', e)


if __name__ == '__main__':
    main()
