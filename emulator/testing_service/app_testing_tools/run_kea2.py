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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEA2_WORKDIR = os.path.join(BASE_DIR, 'tools', 'kea2')


def run_kea2(package, running_minutes=60, serial=None, output_dir=None):
    """Run Kea2 on the given package and return the exit code."""
    if not shutil.which('kea2'):
        logger.error("kea2 not found in PATH. Install with: pip install kea2-python")
        return 1

    os.makedirs(KEA2_WORKDIR, exist_ok=True)
    out = output_dir or os.path.join(KEA2_WORKDIR, 'output')
    os.makedirs(out, exist_ok=True)

    cmd = ['kea2', 'run', '-p', package, '--running-minutes', str(running_minutes)]
    if serial:
        cmd.extend(['-s', serial])

    env = os.environ.copy()
    if serial:
        env['ANDROID_SERIAL'] = serial

    logger.info("Running Kea2 on package: %s (minutes=%d)", package, running_minutes)
    return subprocess.run(cmd, cwd=KEA2_WORKDIR, env=env, text=True).returncode


def main():
    parser = argparse.ArgumentParser(description='Run Kea2 property-based testing on an Android app')
    parser.add_argument('-p', '--package', required=True, help='Package name to test')
    parser.add_argument('--running-minutes', type=int, default=60, help='Test duration in minutes (default: 60)')
    parser.add_argument('--serial', default=os.environ.get('ANDROID_SERIAL'),
                        help='Device serial (default: ANDROID_SERIAL env var)')
    parser.add_argument('--output-dir', default=None, help='Directory to write output (default: tools/kea2/output)')
    args = parser.parse_args()

    sys.exit(run_kea2(args.package, args.running_minutes, args.serial, args.output_dir))


if __name__ == '__main__':
    main()
