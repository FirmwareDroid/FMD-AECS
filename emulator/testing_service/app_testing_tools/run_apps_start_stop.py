#!/usr/bin/env python3
"""
Python replacement for the legacy `start_apps.sh` script.

Behavior:
 - Uses adb to list installed packages and attempts to launch each app's main/launcher
   activity.
 - When multiple adb devices are connected and no --serial/ANDROID_SERIAL is given,
   the script will pick the first connected device reported by `adb devices`.
 - Replaces the original bash implementation with more robust parsing and logging.

Usage:
    python3 start_apps.py [--serial SERIAL] [--delay 0.3]

This script is intended to live alongside the previous bash script (same directory).
"""

import argparse
import logging
import subprocess
import time
import os
import sys

# Prefer using helper from common if available (keeps device-selection logic consistent)
try:
    from common import get_adb_cmd, get_first_connected_device
except Exception:
    # Fallback if script is executed standalone and common can't be imported
    def get_first_connected_device():
        try:
            r = subprocess.run(['adb', 'devices'], capture_output=True, text=True, timeout=5)
            lines = [l.strip() for l in (r.stdout or '').splitlines()]
            for l in lines[1:]:
                if not l:
                    continue
                parts = l.split()
                if len(parts) >= 2 and parts[1] == 'device':
                    return parts[0]
        except Exception:
            return None
        return None

    def get_adb_cmd(serial=None):
        if serial:
            return ['adb', '-s', serial]
        first = get_first_connected_device()
        if first:
            return ['adb', '-s', first]
        return ['adb']


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def run_adb(cmd, timeout=15):
    try:
        logger.debug('Running adb cmd: %s', ' '.join(cmd))
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res
    except subprocess.TimeoutExpired as e:
        logger.error('adb command timed out: %s', e)
        raise
    except Exception as e:
        logger.exception('adb command failed: %s', e)
        raise


def list_installed_packages(adb_base):
    cmd = adb_base + ['shell', 'pm', 'list', 'packages']
    res = run_adb(cmd)
    out = (res.stdout or '').strip()
    packages = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('package:'):
            pkg = line.split(':', 1)[1].strip()
            if pkg:
                packages.append(pkg)
    return packages


def try_launch_with_monkey(adb_base, package, events=1, seed=None, randomize_throttle=False, throttle=None, extra=None):
    monkey_cmd = ['shell', 'monkey', '-p', package, '-c', 'android.intent.category.LAUNCHER']
    if seed is not None:
        try:
            monkey_cmd += ['-s', str(int(seed))]
        except Exception:
            pass
    if throttle is not None:
        try:
            monkey_cmd += ['--throttle', str(int(throttle))]
        except Exception:
            pass
    if randomize_throttle:
        monkey_cmd.append('--randomize-throttle')
    if extra:
        monkey_cmd += [str(x) for x in extra]
    monkey_cmd.append(str(int(events)))

    cmd = adb_base + monkey_cmd
    res = run_adb(cmd, timeout=20)
    combined = ((res.stdout or '') + '\n' + (res.stderr or '')).lower()
    if 'device offline' in combined:
        raise RuntimeError('device offline')
    return res


def get_pid_of(adb_base, package):
    cmd = adb_base + ['shell', 'pidof', package]
    res = run_adb(cmd, timeout=5)
    out = (res.stdout or '').strip()
    if out:
        # pidof may return multiple pids; take the first
        first = out.splitlines()[-1].strip()
        # sometimes pidof returns an error on older devices; check digits
        if first and any(ch.isdigit() for ch in first):
            # return the last token that looks numeric
            for token in reversed(first.split()):
                if token.isdigit():
                    return token
            return first
    return None


def resolve_main_activity(adb_base, package):
    # Use cmd package resolve-activity --components <package>
    cmd = adb_base + ['shell', f'cmd package resolve-activity --components {package}']
    res = run_adb(cmd, timeout=10)
    out = (res.stdout or '')
    # Look for 'component=' occurrences
    for line in out.splitlines():
        line = line.strip()
        if 'component=' in line:
            try:
                # component=<pkg>/<activity> ...
                comp = line.split('component=', 1)[1].split()[0]
                return comp
            except Exception:
                continue
    return None


def start_activity(adb_base, component):
    # component is expected as <package>/<ActivityName>
    cmd = adb_base + ['shell', 'am', 'start', '-n', component]
    res = run_adb(cmd, timeout=10)
    combined = ((res.stdout or '') + '\n' + (res.stderr or '')).lower()
    if 'device offline' in combined:
        raise RuntimeError('device offline')
    return res


def main():
    parser = argparse.ArgumentParser(description='Start installed apps on the connected device (replacement for start_apps.sh)')
    parser.add_argument('--serial', help='ADB device serial (if omitted the first connected device is used)')
    parser.add_argument('--delay', type=float, default=0.3, help='Delay between app launches (seconds)')
    parser.add_argument('--max', type=int, default=0, help='Maximum number of apps to attempt (0 = all)')
    # Compatibility with previous start_apps.py invocation used by run_experiment
    parser.add_argument('-p', '--package', dest='package', help='(optional) single package to start')
    parser.add_argument('-m', '--monkey-events', dest='monkey_events', type=int, default=1, help='Number of monkey events to send when attempting monkey launch (default 1)')
    parser.add_argument('--monkey-seed', dest='monkey_seed', type=int, help='Random seed for monkey (-s)')
    parser.add_argument('--monkey-throttle', dest='monkey_throttle', type=int, help='Monkey --throttle in milliseconds')
    parser.add_argument('--monkey-randomize-throttle', dest='monkey_randomize_throttle', action='store_true', help='Use --randomize-throttle with monkey')
    parser.add_argument('--monkey-extra', dest='monkey_extra', nargs='*', help='Extra raw monkey arguments (tokens) to append, e.g. --monkey-extra --pct-touch 50')
    args = parser.parse_args()

    serial = args.serial or os.environ.get('ANDROID_SERIAL') or os.environ.get('ADB_SERIAL')
    adb_base = get_adb_cmd(serial)
    # If get_adb_cmd selected a serial, reflect that for logging
    if len(adb_base) >= 3 and adb_base[1] == '-s':
        used_serial = adb_base[2]
    else:
        used_serial = None

    logger.info('Using adb base: %s', ' '.join(adb_base))
    logger.info('Listing installed packages...')
    try:
        packages = list_installed_packages(adb_base)
    except Exception as e:
        logger.exception('Failed to list installed packages: %s', e)
        sys.exit(2)

    if not packages:
        logger.info('No installed packages found')
        return

    count = 0
    for package in packages:
        if args.max and count >= args.max:
            break
        logger.info('Attempting to start package: %s', package)
        try:
            try_launch_with_monkey(adb_base, package, events=args.monkey_events, seed=getattr(args, 'monkey_seed', None), randomize_throttle=getattr(args, 'monkey_randomize_throttle', False), throttle=getattr(args, 'monkey_throttle', None), extra=getattr(args, 'monkey_extra', None))
        except RuntimeError as e:
            if 'device offline' in str(e).lower():
                logger.error('Device offline detected; aborting start sequence')
                sys.exit(2)
            logger.warning('Monkey launch failed for %s: %s', package, e)
        except Exception:
            logger.exception('Unexpected error running monkey for %s', package)

        time.sleep(args.delay)

        pid = None
        try:
            pid = get_pid_of(adb_base, package)
        except Exception:
            logger.exception('Failed to query pid for %s', package)

        if pid:
            logger.info('✅ Success: %s is running (PID: %s)', package, pid)
        else:
            # Try to resolve and launch main activity
            try:
                main_activity = resolve_main_activity(adb_base, package)
                if main_activity:
                    logger.info('Resolved main activity for %s: %s', package, main_activity)
                    try:
                        start_activity(adb_base, main_activity)
                    except RuntimeError:
                        logger.error('Device offline detected while starting activity for %s; aborting', package)
                        sys.exit(2)
                    except Exception:
                        logger.exception('Failed to start main activity for %s', package)
                else:
                    logger.warning('No launchable main activity found for %s', package)
            except Exception:
                logger.exception('Failed to resolve main activity for %s', package)

        time.sleep(args.delay)
        count += 1


if __name__ == '__main__':
    main()

