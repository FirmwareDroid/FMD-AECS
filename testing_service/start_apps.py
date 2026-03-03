#!/usr/bin/env python3
"""
Start all installed apps on a connected Android device and report statistics.
This script will:
 - list installed packages using adb
 - for each package, try to start it (monkey first, then resolved main activity)
 - detect if the app started via `pidof`
 - collect successes and failures and a frequency analysis of failure reasons
 - write summary.json and failures.json into current working directory

Usage: python3 start_apps.py [-s SERIAL] [-d DELAY]
"""

import argparse
import json
import subprocess
import time
import logging
from collections import Counter, defaultdict
from shutil import which
from typing import Dict, Any, List

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


def monkey_launch(package, serial=None):
    # Use monkey to attempt to start a package's main launcher activity
    cmd = []
    if serial:
        cmd += ['-s', serial]
    cmd += ['shell', 'monkey', '-p', package, '-c', 'android.intent.category.LAUNCHER', '1']
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
    # Look for 'component=' in output
    for line in out.splitlines():
        if 'component=' in line:
            # component=com.example/.MainActivity or component=com.example/com.example.MainActivity
            part = line.split('component=', 1)[1].strip()
            if part:
                return part
    # fallback: try brief resolve-activity
    cmd = []
    if serial:
        cmd += ['-s', serial]
    cmd += ['shell', 'cmd', 'package', 'resolve-activity', '--brief', package]
    rc, out, err = run_adb(cmd)
    if rc == 0 and out:
        return out.strip()
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


def get_apk_path(package: str, serial: str | None = None) -> str | None:
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


def start_packages(serial=None, delay=0.3, stop_after_start=False, stop_delay=1.0):
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
        ok, output = monkey_launch(pkg, serial)
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
    }

    logging.info('Finished processing packages. Started=%d Failed=%d. Started by script=%d', len(success), len(failures), len(started_by_script))

    # frequency analysis for failures
    freq = dict(failure_reasons)

    # write outputs
    out_summary = {
        'summary': summary,
        'failure_frequency': freq,
    }

    try:
        with open('summary.json', 'w') as f:
            json.dump(out_summary, f, indent=2)
        with open('failures.json', 'w') as f:
            json.dump(failures, f, indent=2)
        logging.info('Wrote summary.json and failures.json')
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

    return out_summary, failures


def main():
    parser = argparse.ArgumentParser(description='Start all installed Android packages and collect stats')
    parser.add_argument('-s', '--serial', help='ADB device serial to target (optional)')
    parser.add_argument('-d', '--delay', type=float, default=0.1, help='Delay between package operations in seconds')
    parser.add_argument('--stop-after-start', action='store_true', help='If set, force-stop apps that the script started after --stop-delay seconds')
    parser.add_argument('--stop-delay', type=float, default=1.0, help='Seconds to wait before stopping apps when --stop-after-start is set')
    parser.add_argument('--pretty', dest='pretty', action='store_true', default=True, help='Pretty-print output summary to stdout')
    parser.add_argument('--no-pretty', dest='pretty', action='store_false', help='Do not pretty-print output summary')
    args = parser.parse_args()

    if not which('adb'):
        logging.error('adb not found in PATH. Aborting.')
        return

    try:
        summary, failures = start_packages(args.serial, args.delay, stop_after_start=args.stop_after_start, stop_delay=args.stop_delay)
        if args.pretty:
            pretty_print_summary(summary, failures)
    except RuntimeError as e:
        logging.error('Runtime error: %s', e)


if __name__ == '__main__':
    main()
