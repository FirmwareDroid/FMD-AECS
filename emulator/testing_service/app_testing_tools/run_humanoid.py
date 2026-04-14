#!/usr/bin/env python3
"""
Humanoid testing tool wrapper.

Starts the Humanoid XMLRPC service (deep-learning guided event selector) and
then runs DroidBot with the `--humanoid` flag so it queries Humanoid for
action prioritisation.

Prerequisites:
  - DroidBot and Humanoid cloned by install_tools.py
  - TensorFlow 1.12 (requires Python 3.5-3.7; may not be available on arm64 /
    Python 3.8+)

Usage:
    python3 run_humanoid.py -p <package> --apk <path/to/app.apk> [options]
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HUMANOID_DIR = os.path.join(BASE_DIR, 'tools', 'Humanoid')
HUMANOID_AGENT = os.path.join(HUMANOID_DIR, 'agent.py')
HUMANOID_CONFIG = os.path.join(HUMANOID_DIR, 'config.json')
HUMANOID_PORT = 50405


def start_humanoid_service():
    """Start the Humanoid XMLRPC service and return the subprocess handle."""
    if not os.path.exists(HUMANOID_AGENT):
        logger.error("Humanoid agent.py not found at %s. Run install_tools.py first.", HUMANOID_AGENT)
        return None
    cmd = [sys.executable, HUMANOID_AGENT, '-c', HUMANOID_CONFIG]
    logger.info("Starting Humanoid XMLRPC service on port %d…", HUMANOID_PORT)
    proc = subprocess.Popen(cmd, cwd=HUMANOID_DIR, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(5)
    if proc.poll() is not None:
        _, err = proc.communicate()
        logger.error("Humanoid service failed to start: %s", err)
        return None
    logger.info("Humanoid service started (pid=%d).", proc.pid)
    return proc


def stop_humanoid_service(proc):
    if proc is None or proc.poll() is not None:
        return
    logger.info("Stopping Humanoid service (pid=%d)…", proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_droidbot_with_humanoid(package, apk_path, running_minutes=60, serial=None):
    """Run DroidBot guided by Humanoid and return the exit code."""
    if not shutil.which('droidbot'):
        logger.error("droidbot not found in PATH. Run install_tools.py first.")
        return 1

    output_dir = os.path.join(BASE_DIR, 'output', 'humanoid', package)
    os.makedirs(output_dir, exist_ok=True)

    cmd = [
        'droidbot',
        '-a', apk_path,
        '-o', output_dir,
        '-policy', 'dfs_greedy',
        '-humanoid', f'localhost:{HUMANOID_PORT}',
        '-timeout', str(running_minutes * 60),
    ]
    if serial:
        cmd.extend(['-d', serial])

    logger.info("Running DroidBot+Humanoid on package: %s (minutes=%d)", package, running_minutes)
    return subprocess.run(cmd, text=True).returncode


def main():
    parser = argparse.ArgumentParser(
        description='Run Humanoid learning-guided Android UI testing (DroidBot + Humanoid)')
    parser.add_argument('-p', '--package', required=True, help='Package name to test')
    parser.add_argument('--apk', required=True, help='Path to the APK file to test')
    parser.add_argument('--running-minutes', type=int, default=60,
                        help='Test duration in minutes (default: 60)')
    parser.add_argument('--serial', default=os.environ.get('ANDROID_SERIAL'),
                        help='Device serial (default: ANDROID_SERIAL env var)')
    parser.add_argument('--no-humanoid-service', action='store_true',
                        help='Skip starting the Humanoid XMLRPC service (assume it is already running)')
    args = parser.parse_args()

    if not os.path.isdir(HUMANOID_DIR):
        logger.error("Humanoid directory not found at %s. Run install_tools.py first.", HUMANOID_DIR)
        sys.exit(1)

    humanoid_proc = None
    if not args.no_humanoid_service:
        humanoid_proc = start_humanoid_service()
        if humanoid_proc is None:
            logger.warning("Humanoid service unavailable – running DroidBot without Humanoid guidance.")

    rc = 1
    try:
        rc = run_droidbot_with_humanoid(args.package, args.apk, args.running_minutes, args.serial)
    finally:
        stop_humanoid_service(humanoid_proc)

    sys.exit(rc)


if __name__ == '__main__':
    main()
