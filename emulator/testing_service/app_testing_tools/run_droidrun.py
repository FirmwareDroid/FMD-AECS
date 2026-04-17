#!/usr/bin/env python3
"""
Droidrun testing tool wrapper.

Runs the Droidrun LLM-based agent for automated Android app testing.
Droidrun is installed via pip and requires an LLM provider to be configured
(`droidrun configure`) and the Droidrun portal to be installed on the device
(`droidrun setup`).

Usage:
    python3 run_droidrun.py run   [--task "…"] [--serial <device>]
    python3 run_droidrun.py setup [--serial <device>]
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, '..', '..', '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from common import get_first_connected_device

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DEFAULT_TASK = (
    "Explore the app and test its main features. "
    "Navigate through different screens and interact with UI elements."
)


def _require_droidrun():
    if not shutil.which('droidrun'):
        logger.error("droidrun not found in PATH. Install with: pip install droidrun")
        sys.exit(1)


def run_droidrun(task, serial=None):
    """Run the Droidrun agent with the given task description."""
    _require_droidrun()
    # If no serial provided, use the first connected adb device (if any)
    if not serial:
        first = get_first_connected_device()
        if first:
            serial = first

    cmd = ['droidrun', 'run', task]
    env = os.environ.copy()
    if serial:
        cmd.extend(['-s', serial])
        env['ANDROID_SERIAL'] = serial
    logger.info("Running Droidrun agent: %s…", task[:80])
    return subprocess.run(cmd, env=env, text=True).returncode


def setup_droidrun(serial=None):
    """Install the Droidrun portal application on the connected device."""
    _require_droidrun()
    # If no serial provided, use the first connected adb device (if any)
    if not serial:
        first = get_first_connected_device()
        if first:
            serial = first

    cmd = ['droidrun', 'setup']
    if serial:
        cmd.extend(['-s', serial])
    logger.info("Installing Droidrun portal on device…")
    return subprocess.run(cmd, text=True).returncode


def main():
    parser = argparse.ArgumentParser(description='Droidrun LLM-based Android testing agent wrapper')
    sub = parser.add_subparsers(dest='action', required=True)

    run_p = sub.add_parser('run', help='Run the Droidrun agent')
    run_p.add_argument('--task', default=DEFAULT_TASK, help='Natural language task for the agent')
    run_p.add_argument('--serial', default=os.environ.get('ANDROID_SERIAL'), help='Device serial')

    setup_p = sub.add_parser('setup', help='Install the Droidrun portal on the device')
    setup_p.add_argument('--serial', default=os.environ.get('ANDROID_SERIAL'), help='Device serial')

    args = parser.parse_args()

    if args.action == 'setup':
        sys.exit(setup_droidrun(args.serial))
    else:
        sys.exit(run_droidrun(args.task, args.serial))


if __name__ == '__main__':
    main()
