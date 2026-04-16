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
import os
import subprocess
import sys
from common import get_adb_cmd

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APE_DIR = os.path.join(BASE_DIR, 'tools', 'ape-bin')
APE_JAR_LOCAL = os.path.join(APE_DIR, 'ape.jar')
APE_SCRIPT_LOCAL = os.path.join(APE_DIR, 'ape')

DEVICE_TMP = '/data/local/tmp/'
DEVICE_APE_JAR = DEVICE_TMP + 'ape.jar'
DEVICE_APE_SCRIPT = DEVICE_TMP + 'ape'


def _adb(serial=None):
    return get_adb_cmd(serial)


def push_ape(serial=None):
    """Push ape.jar and ape shell launcher to the device."""
    adb = _adb(serial)
    logger.info("Pushing ape.jar to device…")
    subprocess.check_call(adb + ['push', APE_JAR_LOCAL, DEVICE_TMP])
    logger.info("Pushing ape shell script to device…")
    subprocess.check_call(adb + ['push', APE_SCRIPT_LOCAL, DEVICE_TMP])
    subprocess.check_call(adb + ['shell', f'chmod 755 {DEVICE_APE_SCRIPT}'])
    logger.info("Ape binaries deployed to device.")


def run_ape(package, running_minutes=60, strategy='sata', serial=None):
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
           '--ape', strategy]
    )
    logger.info("Running Ape on package: %s (strategy=%s, minutes=%d)", package, strategy, running_minutes)
    return subprocess.run(cmd, text=True).returncode


def main():
    parser = argparse.ArgumentParser(description='Run Ape search-based GUI testing on an Android device')
    parser.add_argument('-p', '--package', required=True, help='Package name to test')
    parser.add_argument('--running-minutes', type=int, default=60, help='Test duration in minutes (default: 60)')
    parser.add_argument('--strategy', default='sata', choices=['sata', 'random'],
                        help='Ape exploration strategy (default: sata)')
    parser.add_argument('--serial', default=os.environ.get('ANDROID_SERIAL'),
                        help='Device serial (default: ANDROID_SERIAL env var)')
    parser.add_argument('--no-push', action='store_true',
                        help='Skip pushing binaries (ape.jar and ape shell script) to the device')
    args = parser.parse_args()

    if not os.path.exists(APE_JAR_LOCAL):
        logger.error("ape.jar not found at %s. Run install_tools.py first.", APE_JAR_LOCAL)
        sys.exit(1)

    if not args.no_push:
        push_ape(args.serial)

    sys.exit(run_ape(args.package, args.running_minutes, args.strategy, args.serial))


if __name__ == '__main__':
    main()
