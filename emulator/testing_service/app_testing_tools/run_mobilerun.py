#!/usr/bin/env python3
"""
Mobilerun testing tool wrapper.

Runs the Mobilerun LLM-based agent for automated Android app testing.
This wrapper delegates execution to the custom `mobilerun_agent_cli.py`
script which handles permissions, speed optimizations, and LLM configuration.

Usage:
    python3 run_mobilerun.py run   [--prompt "…"] [--package <pkg>] [--serial <device>]
    python3 run_mobilerun.py setup [--serial <device>]
"""

import argparse
import logging
import os
import sys
import subprocess

from test_results import append_run

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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Assumes the custom script is in the same directory as this wrapper
CLI_SCRIPT = os.path.join(_HERE, 'mobilerun_agent_cli.py')


def run_mobilerun(prompt=None, package=None, serial=None):
    """Run the Mobilerun agent with the given task or package."""
    cmd = [sys.executable, CLI_SCRIPT]
    if prompt:
        cmd.extend(['--prompt', prompt])
    if package:
        cmd.extend(['--package', package])
    if serial:
        cmd.extend(['--device', serial])

    logger.info("Delegating to %s...", os.path.basename(CLI_SCRIPT))
    return subprocess.run(cmd, text=True).returncode


def setup_mobilerun(serial=None):
    """Install the Mobilerun portal and configure ADB permissions."""
    cmd = [sys.executable, CLI_SCRIPT, '--setup-device']
    if serial:
        cmd.extend(['--device', serial])

    logger.info("Installing and configuring Mobilerun on device...")
    return subprocess.run(cmd, text=True).returncode


def main():
    parser = argparse.ArgumentParser(description='Mobilerun LLM-based Android testing agent wrapper')
    sub = parser.add_subparsers(dest='action', required=True)

    run_p = sub.add_parser('run', help='Run the Mobilerun agent')
    run_p.add_argument('--prompt', default=None, help='Natural language task for the agent')
    run_p.add_argument('--package', default=None, help='Target application package for auto-exploration')
    run_p.add_argument('--serial', default=None,
                       help='Device serial (optional). If omitted, the CLI handles discovery.')

    setup_p = sub.add_parser('setup', help='Install and configure Mobilerun on the device')
    setup_p.add_argument('--serial', default=None,
                         help='Device serial (optional). If omitted, the CLI handles discovery.')

    args = parser.parse_args()

    if not os.path.isfile(CLI_SCRIPT):
        logger.error("Cannot find the underlying CLI script: %s", CLI_SCRIPT)
        sys.exit(1)

    if args.action == 'setup':
        sys.exit(setup_mobilerun(args.serial))
    else:
        # Execute run sequence
        ret = run_mobilerun(args.prompt, args.package, args.serial)

        # Determine the task string for the test_results output
        if args.prompt:
            task_preview = args.prompt
        elif args.package:
            task_preview = f"Explore package: {args.package}"
        else:
            task_preview = "No specific task or package provided"

        summary = {
            'total_packages': 1,
            'started': 1 if ret == 0 else 0,
            'tool': 'mobilerun',
            'task_preview': task_preview[:120],
        }
        failures = []
        if ret != 0:
            failures.append({'task': task_preview, 'reason': 'mobilerun_failed', 'detail': f'return_code={ret}'})

        try:
            append_run('mobilerun', summary, failures, out_dir=os.path.join(_HERE, 'output'))
        except Exception:
            logger.exception('Failed to write mobilerun summary')

        sys.exit(ret)


if __name__ == '__main__':
    main()