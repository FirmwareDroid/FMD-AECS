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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COMBODROID_DIR = os.path.join(BASE_DIR, 'tools', 'combodroid')
COMBODROID_JAR = os.path.join(COMBODROID_DIR, 'ComboDroid.jar')
KEYSTORE = os.path.join(COMBODROID_DIR, 'testKeyStore.jks')


def generate_config(package, apk_path, android_sdk, output_dir,
                    running_minutes, platform_version='26',
                    buildtool_version='27.0.3'):
    """Write a ComboDroid configuration file and return its path."""
    os.makedirs(output_dir, exist_ok=True)
    config_content = (
        f"subject-dir = {os.path.dirname(os.path.abspath(apk_path))}\n"
        f"apk-name = {os.path.basename(apk_path)}\n"
        f"instrument-output-dir = {output_dir}\n"
        f"androidSDK-dir = {android_sdk}\n"
        f"android-platform-version = {platform_version}\n"
        f"android-buildtool-version = {buildtool_version}\n"
        f"keystore-path = {KEYSTORE}\n"
        "key-alias = combodroid\n"
        "key-password = combodroid\n"
        f"package-name = {package}\n"
        "ComboDroid-type = alpha\n"
        "trace-directory = traces\n"
        f"running-minutes = {running_minutes}\n"
        "modeling-minutes = 30\n"
    )
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
    parser.add_argument('--apk', required=True, help='Path to the APK file to test')
    parser.add_argument('--running-minutes', type=int, default=5,
                        help='Total test duration in minutes (default: 5)')
    parser.add_argument('--output-dir', default=None,
                        help='Directory for instrumented APK and traces (default: tools/combodroid_output)')
    parser.add_argument('--android-sdk', default=os.environ.get('ANDROID_HOME', '/android/sdk'),
                        help='Android SDK root (default: $ANDROID_HOME or /android/sdk)')
    parser.add_argument('--platform-version', default='34',
                        help='Android platform version to use for instrumentation (default: 34)')
    parser.add_argument('--buildtool-version', default='27.0.3',
                        help='Android build-tools version for resigning (default: 27.0.3)')
    args = parser.parse_args()

    if not os.path.exists(COMBODROID_JAR):
        logger.error("ComboDroid.jar not found at %s. Run install_tools.py first.", COMBODROID_JAR)
        sys.exit(1)

    if not os.path.exists(args.apk):
        logger.error("APK not found: %s", args.apk)
        sys.exit(1)

    output_dir = args.output_dir or os.path.join(BASE_DIR, 'output', 'combodroid')
    config_path = generate_config(
        args.package, args.apk, args.android_sdk, output_dir,
        args.running_minutes, args.platform_version, args.buildtool_version,
    )
    sys.exit(run_combodroid(config_path))


if __name__ == '__main__':
    main()
