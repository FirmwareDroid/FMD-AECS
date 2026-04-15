#!/usr/bin/env python3
"""
Droidrun agent CLI – integration shim for the testing pipeline.

This script is invoked by run_experiment.py when `--mode droidrun` is
selected.  It wraps the `droidrun` command-line tool so that the pipeline
can set it up and run it with a single interface.

Usage (called by run_experiment.py):
    python3 droidrun_agent_cli.py run   [--task "…"] [--serial <device>]
    python3 droidrun_agent_cli.py setup [--serial <device>]

Environment variables:
    ANDROID_SERIAL   – device serial number (overridable via --serial)
    DROIDRUN_*       – any Droidrun configuration variables
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys

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


def cmd_run(args):
    """Run the Droidrun agent."""
    _require_droidrun()
    cmd = ['droidrun', 'run', args.task]
    env = os.environ.copy()
    if args.serial:
        cmd.extend(['-s', args.serial])
        env['ANDROID_SERIAL'] = args.serial
    logger.info("Running Droidrun agent with task: %s…", args.task[:100])
    rc = subprocess.run(cmd, env=env, text=True).returncode
    if rc != 0:
        logger.error("Droidrun exited with code %d", rc)
    return rc


def cmd_setup(args):
    """Install the Droidrun portal on the connected device."""
    _require_droidrun()
    cmd = ['droidrun', 'setup']
    if args.serial:
        cmd.extend(['-s', args.serial])
    logger.info("Installing Droidrun portal on device…")
    return subprocess.run(cmd, text=True).returncode


def main():
    parser = argparse.ArgumentParser(
        description='Droidrun agent CLI shim for the FMD-AECS testing pipeline')
    sub = parser.add_subparsers(dest='action', required=True)

    run_p = sub.add_parser('run', help='Run the Droidrun LLM agent')
    run_p.add_argument('--task', default=DEFAULT_TASK,
                       help='Natural language task for the agent')
    run_p.add_argument('--serial', default=os.environ.get('ANDROID_SERIAL'),
                       help='Device serial (default: ANDROID_SERIAL env var)')

    setup_p = sub.add_parser('setup', help='Install the Droidrun portal on the device')
    setup_p.add_argument('--serial', default=os.environ.get('ANDROID_SERIAL'),
                         help='Device serial (default: ANDROID_SERIAL env var)')

    args = parser.parse_args()

    if args.action == 'setup':
        sys.exit(cmd_setup(args))
    else:
        sys.exit(cmd_run(args))


if __name__ == '__main__':
    main()
