#!/usr/bin/env python3
"""
Kea2 testing tool wrapper.

Runs Kea2 (property-based + Fastbot3 backend) on an Android app.
Kea2 is installed via pip (kea2-python) and must be initialised once with
`kea2 init` before first use (handled automatically by install_tools.py).

Usage:
    python3 run_kea2.py -p <package> [options]

Examples:
    python3 run_kea2.py -p com.example.app --running-minutes 30
    python3 run_kea2.py -p com.example.app --serial emulator-5554
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import os
import tempfile
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, '..', '..', '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from common import get_first_connected_device
from test_results import append_run

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEA2_WORKDIR = os.path.join(BASE_DIR, 'tools', 'kea2')


def run_kea2(package, running_minutes=60, serial=None, output_dir=None, apk_path=None):
    """Run Kea2 on the given package and return the exit code.

    This wrapper behaviour mirrors other tool wrappers in this project:
    - Auto-select first connected device when --serial omitted
    - Optionally pull the installed APK from the device when --apk omitted
    - Set ANDROID_SERIAL in the environment for the run
    - Write a small run summary via test_results.append_run
    """
    if not shutil.which('kea2'):
        logger.error("kea2 not found in PATH. Install with: pip install kea2-python")
        return 1

    os.makedirs(KEA2_WORKDIR, exist_ok=True)
    out = output_dir or os.path.join(KEA2_WORKDIR, 'output')
    os.makedirs(out, exist_ok=True)

    tmpdir = None

    # If no serial provided, pick the first connected adb device (if any)
    if not serial:
        first = get_first_connected_device()
        if first:
            serial = first
            logger.info('Using first connected adb device: %s', serial)

    # If user passed a package name (not an apk path) and requested to use an
    # APK (or didn't provide one), try to pull the installed APK from device so
    # it is available locally (some workflows expect a local APK artifact).
    apk_local = apk_path
    try:
        looks_like_apk = False
        try:
            looks_like_apk = str(package).lower().endswith('.apk') or (apk_path and os.path.exists(apk_path))
        except Exception:
            looks_like_apk = False

        if not apk_local and not looks_like_apk:
            if not serial:
                logger.error('No adb devices connected; provide --serial or connect a device')
                return 2

            pm_cmd = ['adb', '-s', serial, 'shell', 'pm', 'path', package]
            logger.info('Querying device for apk path: %s', ' '.join(pm_cmd))
            res = subprocess.run(pm_cmd, capture_output=True, text=True)
            if res.returncode != 0 or not res.stdout:
                logger.error('Could not determine apk path for package %s: %s', package, (res.stderr or res.stdout).strip())
                return 4
            first_line = res.stdout.strip().splitlines()[0]
            if first_line.startswith('package:'):
                device_apk_path = first_line.split(':', 1)[1].strip()
            else:
                device_apk_path = first_line.strip()

            if not device_apk_path:
                logger.error('pm path returned no apk path for %s', package)
                return 4

            tmpdir = os.path.join(KEA2_WORKDIR, 'tmp', f'kea2_apk_{package}')
            os.makedirs(tmpdir, exist_ok=True)
            local_apk = os.path.join(tmpdir, os.path.basename(device_apk_path))
            pull_cmd = ['adb', '-s', serial, 'pull', device_apk_path, local_apk]
            logger.info('Pulling apk from device: %s', ' '.join(pull_cmd))
            res2 = subprocess.run(pull_cmd, capture_output=True, text=True)
            if res2.returncode != 0:
                logger.error('Failed to pull apk from device for %s: %s', package, (res2.stderr or res2.stdout).strip())
                shutil.rmtree(tmpdir, ignore_errors=True)
                return 5
            apk_local = local_apk

        # Build the kea2 command. Kea2 primarily accepts -p <package>; keep other
        # options such as running-minutes. We don't pass the local APK to kea2
        # here because the kea2 CLI expects the package name; however keeping the
        # APK locally is useful for downstream inspection.
        cmd = ['kea2', 'run', '-p', package, '--running-minutes', str(running_minutes)]
        if serial:
            cmd += ['-s', serial]

        env = os.environ.copy()
        if serial:
            env['ANDROID_SERIAL'] = serial

        logger.info("Running Kea2 on package: %s (minutes=%d)", package, running_minutes)
        ret = subprocess.run(cmd, cwd=KEA2_WORKDIR, env=env, text=True).returncode

        # create a tool summary similar to app_start_summary.json
        summary = {
            'total_packages': 1,
            'started': 1 if ret == 0 else 0,
            'failed': 0 if ret == 0 else 1,
            'skipped': 0,
            'started_by_script': 1 if ret == 0 else 0,
            'started_packages': [package] if ret == 0 else [],
            'failed_packages': [] if ret == 0 else [package],
        }
        failures = []
        if ret != 0:
            failures.append({'package': package, 'reason': 'kea2_failed', 'detail': f'return_code={ret}'})
        try:
            append_run('kea2', summary, failures, out_dir=os.path.join(KEA2_WORKDIR, 'output'))
        except Exception:
            logger.exception('Failed to write kea2 summary')

        return ret
    finally:
        # Clean up any temporary apk we pulled from the device
        if tmpdir:
            try:
                if apk_local and os.path.exists(apk_local):
                    os.remove(apk_local)
                shutil.rmtree(tmpdir, ignore_errors=True)
                logger.info('Removed temporary apk and directory: %s', tmpdir)
            except Exception:
                logger.exception('Failed to remove temporary apk: %s', apk_local)


def main():
    parser = argparse.ArgumentParser(description='Run Kea2 property-based testing on an Android app')
    parser.add_argument('-p', '--package', required=True, help='Package name to test')
    parser.add_argument('--running-minutes', type=int, default=5, help='Test duration in minutes (default: 5)')
    parser.add_argument('--serial', default=os.environ.get('ANDROID_SERIAL'),
                        help='Device serial (default: ANDROID_SERIAL env var)')
    parser.add_argument('--output-dir', default=None, help='Directory to write output (default: tools/kea2/output)')
    args = parser.parse_args()

    sys.exit(run_kea2(args.package, args.running_minutes, args.serial, args.output_dir))


if __name__ == '__main__':
    main()
