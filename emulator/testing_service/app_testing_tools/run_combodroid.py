#!/usr/bin/env python3
"""
ComboDroid testing tool wrapper.

Generates a ComboDroid configuration file from command-line arguments and
then runs ComboDroid.jar (combinatorial GUI event generation) with Java.

Usage:
    python3 run_combodroid.py -p <package> --apk <path/to/app.apk> [options]

Note:
    The APK must be on the host file-system (not the device).  ComboDroid
    instruments the APK, resigns it with its own test keystore, installs the
    instrumented version on the connected device, and then tests it.
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
_HERE = os.path.dirname(os.path.abspath(__file__))

def _find_project_root(marker='common.py'):
    p = _HERE
    while True:
        candidate = os.path.join(p, marker)
        if os.path.exists(candidate):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None

_PROJECT_ROOT = _find_project_root() or os.path.normpath(os.path.join(_HERE, '..', '..', '..'))
if _PROJECT_ROOT and _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common import get_first_connected_device
from test_results import append_run

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COMBODROID_DIR = os.path.join(BASE_DIR, 'tools', 'combodroid')
COMBODROID_JAR = os.path.join(COMBODROID_DIR, 'ComboDroid.jar')
KEYSTORE = os.path.join(COMBODROID_DIR, 'testKeyStore.jks')


def _choose_android_sdk_root(provided):
    # Prefer explicit provided path, then common env vars, then platform defaults
    candidates = []
    if provided:
        candidates.append(provided)
    for var in ('ANDROID_HOME', 'ANDROID_SDK_ROOT', 'ANDROID_SDK'):
        val = os.environ.get(var)
        if val:
            candidates.append(val)
    # macOS common location
    candidates.append(os.path.expanduser('~/Library/Android/sdk'))
    # generic fallback
    candidates.append('/android/sdk')
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    # return the provided value or first candidate even if missing
    return provided or candidates[0]


def _latest_platform(android_sdk):
    plat_dir = os.path.join(android_sdk, 'platforms')
    if not os.path.isdir(plat_dir):
        return None
    best = None
    for name in os.listdir(plat_dir):
        if not name.startswith('android-'):
            continue
        try:
            ver = int(name.split('-', 1)[1])
        except Exception:
            continue
        if best is None or ver > best:
            best = ver
    return str(best) if best is not None else None


def _latest_buildtools(android_sdk):
    bt_dir = os.path.join(android_sdk, 'build-tools')
    if not os.path.isdir(bt_dir):
        return None
    def parse_version(v):
        parts = v.split('.')
        nums = []
        for p in parts:
            try:
                nums.append(int(p))
            except Exception:
                # non-numeric segments: treat as 0
                nums.append(0)
        return tuple(nums)
    best = None
    best_name = None
    for name in os.listdir(bt_dir):
        path = os.path.join(bt_dir, name)
        if not os.path.isdir(path):
            continue
        ver = parse_version(name)
        if best is None or ver > best:
            best = ver
            best_name = name
    return best_name


def generate_config(package, apk_path, android_sdk, output_dir,
                    running_minutes, platform_version='26',
                    buildtool_version='27.0.3', modeling_minutes=30, serial=None):
    """Write a ComboDroid configuration file and return its path."""
    os.makedirs(output_dir, exist_ok=True)
    lines = [
        f"subject-dir = {os.path.dirname(os.path.abspath(apk_path))}",
        f"apk-name = {os.path.basename(apk_path)}",
        f"instrument-output-dir = {output_dir}",
        f"instrumented-output-dir = {output_dir}",
        f"androidSDK-dir = {android_sdk}",
        f"android-platform-version = {platform_version}",
        f"android-buildtool-version = {buildtool_version}",
        f"keystore-path = {KEYSTORE}",
        "key-alias = combodroid",
        "key-password = combodroid",
        f"package-name = {package}",
        "startup-script = []",
        "ComboDroid-type = alpha",
        "trace-directory = traces",
    ]
    if serial:
        lines.insert(10, f"serial = {serial}")
    # Validate Android SDK layout and build-tools/platform existence and warn
    try:
        sdk_platform_dir = os.path.join(android_sdk, 'platforms', f'android-{platform_version}')
        sdk_buildtool_dir = os.path.join(android_sdk, 'build-tools', buildtool_version)
        if not os.path.isdir(sdk_platform_dir):
            logger.warning('Android platform directory not found: %s (expected for platform %s). Ensure this platform is installed in %s', sdk_platform_dir, platform_version, android_sdk)
        if not os.path.isdir(sdk_buildtool_dir):
            logger.warning('Android build-tools directory not found: %s (expected build-tools %s). Ensure this build-tools version is installed in %s', sdk_buildtool_dir, buildtool_version, android_sdk)
    except Exception:
        logger.exception('Error while checking Android SDK layout')

    # Enforce minimum modeling_minutes
    if modeling_minutes is None or modeling_minutes < 30:
        logger.info('modeling-minutes must be at least 30; setting modeling-minutes=30')
        modeling_minutes = 30

    lines.append(f"running-minutes = {running_minutes}")
    lines.append(f"modeling-minutes = {modeling_minutes}")
    config_content = "\n".join(lines) + "\n"
    config_path = os.path.join(output_dir, 'combodroid.conf')
    with open(config_path, 'w', encoding='utf-8') as fh:
        fh.write(config_content)
    logger.info("ComboDroid config written to %s", config_path)
    return config_path


def run_combodroid(config_path):
    """Run ComboDroid.jar and return its exit code."""
    java = shutil.which('java')
    if not java:
        logger.error("java not found in PATH.  Java 21+ is required.")
        return 1
    cmd = [java, '-Xmx4g', '-jar', COMBODROID_JAR, config_path, '--no-startup', '-v']
    logger.info("Launching ComboDroid…")
    return subprocess.run(cmd, cwd=COMBODROID_DIR, text=True).returncode


def main():
    parser = argparse.ArgumentParser(description='Run ComboDroid combinatorial GUI testing')
    parser.add_argument('-p', '--package', required=True, help='Package name to test')
    parser.add_argument('--apk', required=False, help='Path to the APK file to test (if omitted, will pull from device)')
    parser.add_argument('--serial', default=None, help='Device serial (optional). If omitted, first connected device is used')
    parser.add_argument('--running-minutes', type=int, default=5,
                        help='Total test duration in minutes (default: 5)')
    parser.add_argument('--output-dir', default=None,
                        help='Directory for instrumented APK and traces (default: tools/combodroid_output)')
    parser.add_argument('--android-sdk', default=os.environ.get('ANDROID_HOME', '/android/sdk'),
                        help='Android SDK root (default: $ANDROID_HOME or /android/sdk)')
    parser.add_argument('--platform-version', default=None,
                        help='Android platform version to use for instrumentation (default: latest installed)')
    parser.add_argument('--buildtool-version', default=None,
                        help='Android build-tools version for resigning (default: latest installed)')
    parser.add_argument('--modeling-minutes', type=int, default=30,
                        help='Time to generate use cases for each iteration (minimum: 30)')
    args = parser.parse_args()

    if not os.path.exists(COMBODROID_JAR):
        logger.error("ComboDroid.jar not found at %s. Run install_tools.py first.", COMBODROID_JAR)
        sys.exit(1)

    apk_path = args.apk
    tmpdir = None
    # Determine serial early so we can include it in the ComboDroid config.
    serial = args.serial
    if not serial:
        try:
            serial = get_first_connected_device()
            if serial:
                logger.info('Using first connected adb device: %s', serial)
        except Exception:
            serial = None

    # Choose Android SDK root and select platform/build-tools if not provided
    android_sdk = _choose_android_sdk_root(args.android_sdk)
    if not android_sdk or not os.path.isdir(android_sdk):
        logger.warning('Android SDK root not found at %s; some operations may fail', android_sdk)
    # Determine platform and build-tools versions to use
    if not args.platform_version:
        latest = _latest_platform(android_sdk)
        if latest:
            args.platform_version = latest
            logger.info('No platform-version provided; using latest installed platform: %s', latest)
        else:
            logger.warning('No Android platforms detected under %s', android_sdk)
    if not args.buildtool_version:
        latest_bt = _latest_buildtools(android_sdk)
        if latest_bt:
            args.buildtool_version = latest_bt
            logger.info('No buildtool-version provided; using latest installed build-tools: %s', latest_bt)
        else:
            logger.warning('No Android build-tools detected under %s', android_sdk)
    # If user did not provide an apk path, attempt to pull the installed apk from device
    if not apk_path or not os.path.exists(apk_path):
        if not serial:
            logger.error('No adb devices connected; provide --serial or connect a device')
            sys.exit(2)

        pm_cmd = ['adb', '-s', serial, 'shell', 'pm', 'path', args.package]
        logger.info('Querying device for apk path: %s', ' '.join(pm_cmd))
        res = subprocess.run(pm_cmd, capture_output=True, text=True)
        if res.returncode != 0 or not res.stdout:
            logger.error('Could not determine apk path for package %s: %s', args.package, (res.stderr or res.stdout).strip())
            sys.exit(4)
        first_line = res.stdout.strip().splitlines()[0]
        if first_line.startswith('package:'):
            device_apk_path = first_line.split(':', 1)[1].strip()
        else:
            device_apk_path = first_line.strip()

        if not device_apk_path:
            logger.error('pm path returned no apk path for %s', args.package)
            sys.exit(4)

        tmpdir = os.path.join(BASE_DIR, 'tmp', f'combodroid_apk_{args.package}')
        os.makedirs(tmpdir, exist_ok=True)
        local_apk = os.path.join(tmpdir, os.path.basename(device_apk_path))
        pull_cmd = ['adb', '-s', serial, 'pull', device_apk_path, local_apk]
        logger.info('Pulling apk from device: %s', ' '.join(pull_cmd))
        res2 = subprocess.run(pull_cmd, capture_output=True, text=True)
        if res2.returncode != 0:
            logger.error('Failed to pull apk from device for %s: %s', args.package, (res2.stderr or res2.stdout).strip())
            shutil.rmtree(tmpdir, ignore_errors=True)
            sys.exit(5)
        apk_path = local_apk

    try:
        output_dir = args.output_dir or os.path.join(BASE_DIR, 'out', 'combodroid')
        config_path = generate_config(
            args.package, apk_path, android_sdk, output_dir,
            args.running_minutes, args.platform_version, args.buildtool_version,
            modeling_minutes=args.modeling_minutes, serial=serial,
        )
        ret = run_combodroid(config_path)

        # create a tool summary similar to app_start_summary.json
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
            failures.append({'package': args.package, 'reason': 'combodroid_failed', 'detail': f'return_code={ret}'})
        try:
            append_run('combodroid', summary, failures, out_dir=os.path.join(BASE_DIR, 'out'))
        except Exception:
            logger.exception('Failed to write combodroid summary')

        sys.exit(ret)
    finally:
        # Clean up any temporary apk we pulled from the device
        if tmpdir:
            try:
                if os.path.exists(apk_path):
                    os.remove(apk_path)
                shutil.rmtree(tmpdir, ignore_errors=True)
                logger.info('Removed temporary apk and directory: %s', tmpdir)
            except Exception:
                logger.exception('Failed to remove temporary apk: %s', apk_path)


if __name__ == '__main__':
    main()
