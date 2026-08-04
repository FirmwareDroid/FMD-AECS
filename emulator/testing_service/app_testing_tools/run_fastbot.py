#!/usr/bin/env python3
"""
Fastbot2.0 testing tool wrapper.

Pushes the Fastbot JARs and arm64-v8a native libraries to the Android device
via ADB, then launches Fastbot model-based GUI testing.

Usage:
    python3 run_fastbot.py -p <package> [options]

Examples:
    python3 run_fastbot.py -p com.example.app --running-minutes 30
python3 run_fastbot.py -p com.example.app --serial emulator-5554 --throttle 300
"""

import argparse
import glob
import logging
import os
import subprocess
import sys
import shutil

# Ensure project root is on sys.path so `from common import get_adb_cmd` works
# when this script is executed directly from its folder.
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

from common import get_adb_cmd, get_first_connected_device
from test_results import append_run

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FASTBOT_DIR = os.path.join(BASE_DIR, 'tools', 'Fastbot_Android')
MONKEYQ_JAR = os.path.join(FASTBOT_DIR, 'monkeyq.jar')
THIRDPART_JAR = os.path.join(FASTBOT_DIR, 'fastbot-thirdpart.jar')
FRAMEWORK_JAR = os.path.join(FASTBOT_DIR, 'framework.jar')
LIBS_ARM64 = os.path.join(FASTBOT_DIR, 'libs', 'arm64-v8a')

DEVICE_SDCARD = '/sdcard/'
DEVICE_TMP = '/data/local/tmp/'
DEVICE_CLASSPATH = '/sdcard/monkeyq.jar:/sdcard/framework.jar:/sdcard/fastbot-thirdpart.jar'
DEVICE_NATIVE_SO = '/data/local/tmp/arm64-v8a/libfastbot_native.so'


def _adb(serial=None):
    return get_adb_cmd(serial)


def push_fastbot(serial=None):
    """Push Fastbot JARs and native libs to the device."""
    adb = _adb(serial)
    logger.info("Pushing Fastbot JARs to device…")
    subprocess.check_call(adb + ['push', MONKEYQ_JAR, DEVICE_SDCARD])
    subprocess.check_call(adb + ['push', THIRDPART_JAR, DEVICE_SDCARD])
    subprocess.check_call(adb + ['push', FRAMEWORK_JAR, DEVICE_SDCARD])

    so_files = sorted(glob.glob(os.path.join(LIBS_ARM64, '*.so')))
    if so_files:
        logger.info("Pushing %d arm64-v8a .so files to device…", len(so_files))
        # Ensure target directory exists on device and push .so files into
        # an arch-specific subfolder so tools that expect
        # /data/local/tmp/arm64-v8a/... can find them.
        remote_dir = os.path.join(DEVICE_TMP, 'arm64-v8a')
        try:
            subprocess.check_call(adb + ['shell', 'mkdir', '-p', remote_dir])
        except Exception:
            logger.warning('Failed to create remote dir %s; falling back to %s', remote_dir, DEVICE_TMP)
            remote_dir = DEVICE_TMP
        for so in so_files:
            subprocess.check_call(adb + ['push', so, remote_dir])
    else:
        logger.warning("No .so files found in %s", LIBS_ARM64)

    logger.info("Fastbot artifacts deployed to device.")


def run_fastbot(package, running_minutes=60, throttle=500, serial=None):
    """Execute Fastbot on the device and return its exit code."""
    # If no serial provided, try to auto-select the first connected device
    if not serial:
        first = get_first_connected_device()
        if first:
            serial = first
            logger.info('Using first connected adb device: %s', serial)
        else:
            logger.error('No adb devices connected; provide --serial or connect a device')
            return 2

    adb = _adb(serial)
    cmd = (
        adb
        + ['shell',
           f'CLASSPATH={DEVICE_CLASSPATH}',
           'exec', 'app_process', '/system/bin',
           'com.android.commands.monkey.Monkey',
           '-p', package,
           '--agent', 'reuseq',
           '--running-minutes', str(running_minutes),
           '--throttle', str(throttle),
           "-s", "12345",
           '-v',
           '-v']
    )
    cmd = [str(a) for a in cmd]
    logger.info(
        "Running Fastbot2.0 on package: %s (minutes=%d, throttle=%dms): CMD: %s",
        package, running_minutes, throttle, cmd
    )
    return subprocess.run(cmd, text=True).returncode


def main():
    parser = argparse.ArgumentParser(description='Run Fastbot2.0 model-based GUI testing on an Android device')
    parser.add_argument('-p', '--package', required=True, help='Package name to test')
    parser.add_argument('--running-minutes', type=int, default=5, help='Test duration in minutes (default: 5)')
    parser.add_argument('--throttle', type=int, default=500, help='Delay between actions in ms (default: 500)')
    parser.add_argument('--serial', default=None,
                        help='Device serial (default: first connected device or ANDROID_SERIAL env var)')
    parser.add_argument('--no-push', action='store_true',
                        help='Skip pushing binaries (assume they are already on the device)')
    args = parser.parse_args()

    for jar in [MONKEYQ_JAR, THIRDPART_JAR, FRAMEWORK_JAR]:
        if not os.path.exists(jar):
            logger.error("Missing Fastbot JAR: %s. Run install_tools.py first.", jar)
            sys.exit(1)

    # Auto-select serial if not provided
    serial = args.serial or os.environ.get('ANDROID_SERIAL')
    if not serial:
        first = get_first_connected_device()
        if first:
            serial = first
            logger.info('Using first connected adb device: %s', serial)

    if not args.no_push:
        try:
            push_fastbot(serial)
        except Exception:
            logger.exception('Failed to push Fastbot artifacts to device')

    # Verify that the required native fastbot library exists on the device
    def device_file_exists(serial, remote_path):
        adb = _adb(serial)
        try:
            res = subprocess.run(adb + ['shell', 'ls', '-l', remote_path], capture_output=True, text=True)
            return res.returncode == 0
        except Exception:
            return False

    def ensure_native_so(serial, remote_path, local_libs_dir):
        # Check if the file already exists on device
        if device_file_exists(serial, remote_path):
            logger.info('Native fastbot library present on device: %s', remote_path)
            return True

        # Try to push the specific library from local libs if available
        local_candidate = os.path.join(local_libs_dir, os.path.basename(remote_path))
        if os.path.exists(local_candidate):
            logger.info('Pushing native fastbot library to device: %s -> %s', local_candidate, remote_path)
            adb = _adb(serial)
            remote_dir = os.path.dirname(remote_path)
            try:
                subprocess.check_call(adb + ['shell', 'mkdir', '-p', remote_dir])
                subprocess.check_call(adb + ['push', local_candidate, remote_path])
            except Exception:
                logger.exception('Failed to push native fastbot library to device')
                return False
            return device_file_exists(serial, remote_path)

        logger.error('Native fastbot library missing on device and not found locally: %s', remote_path)
        return False

    # Enforce presence of the native library before running fastbot
    if not ensure_native_so(serial, DEVICE_NATIVE_SO, LIBS_ARM64):
        logger.error('Required native fastbot library not available on device: %s', DEVICE_NATIVE_SO)
        logger.error('Either run with pushing enabled or ensure the file exists on the device')
        sys.exit(3)

    ret = run_fastbot(args.package, args.running_minutes, args.throttle, serial)

    # Prepare summary similar to other tool wrappers
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
        failures.append({'package': args.package, 'reason': 'fastbot_failed', 'detail': f'return_code={ret}'})
    try:
        out_dir = os.path.join(BASE_DIR, 'out')
        append_run('fastbot', summary, failures, out_dir=out_dir)
    except Exception:
        logger.exception('Failed to write fastbot summary')

    sys.exit(ret)


if __name__ == '__main__':
    main()
