#!/usr/bin/env python3
"""
Run the experiment pipeline inside all running emulator containers.

This script mirrors the behaviour of `run_experiment.py` but runs it inside
all Docker containers whose name matches a provided filter (default: "android_emulator").

It supports selecting a single mode (same choices as run_experiment.py) or
individual flags to start specific app-testing tools (ape, fastbot, kea2, droidrun, monkey).
If any of the individual tool flags are provided they override --mode and the
script will start one container-invocation per enabled tool.

Each invocation is executed detached (docker exec -d ...) so the container
runs the experiment independently.

Example:
  python3 run_experiments_in_containers.py --mode monkey --test-only-one --pcap-http-port 54320 \
    --socks5-address 172.31.250.4

Or run multiple tools on all containers:
  python3 run_experiments_in_containers.py --ape --fastbot --pcapdroid

"""

import argparse
import logging
import shutil
import subprocess
import sys
from typing import List

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

DOCKER_DEFAULT_FILTER = "android_emulator"
REMOTE_RUN_SCRIPT = "/android/testing_service/run_experiment.py"


def find_docker_cli() -> str:
    docker = shutil.which('docker')
    if not docker:
        logging.error("docker CLI not found on PATH. Please install Docker / ensure 'docker' is available.")
        sys.exit(2)
    return docker


def list_containers(docker_cli: str, name_filter: str) -> List[str]:
    try:
        res = subprocess.run([docker_cli, 'ps', '--format', '{{.Names}}'], capture_output=True, text=True, check=True)
        names = [n.strip() for n in (res.stdout or '').splitlines() if n.strip()]
        matched = [n for n in names if name_filter in n]
        return matched
    except subprocess.CalledProcessError as e:
        logging.error('Failed to list docker containers: %s', e)
        logging.debug('stdout: %s', getattr(e, 'stdout', ''))
        logging.debug('stderr: %s', getattr(e, 'stderr', ''))
        sys.exit(3)


def build_exec_command(docker_cli: str, container: str, mode: str, args) -> List[str]:
    cmd = [docker_cli, 'exec', '-d', container, 'python3', REMOTE_RUN_SCRIPT, '--mode', mode]
    if args.test_only_one:
        cmd.append('--test-only-one')
    if args.skip_setup:
        cmd.append('--skip-setup')
    if args.pcapdroid:
        cmd.append('--pcapdroid')
    if args.pcap_http_port is not None:
        cmd.extend(['--pcap-http-port', str(args.pcap_http_port)])
    if args.socks5_address:
        cmd.extend(['--socks5-address', args.socks5_address])
    # pass through the selection of testing tools as flags if helpful (not strictly required by run_experiment)
    return cmd


def parse_args():
    parser = argparse.ArgumentParser(description='Run run_experiment.py inside all matching android emulator containers')
    parser.add_argument('--filter', type=str, default=DOCKER_DEFAULT_FILTER, help='Substring to match container names (default: android_emulator)')
    parser.add_argument('--mode', choices=['basic', 'droidrun', 'single', 'monkey', 'ape', 'fastbot', 'kea2'], default='basic', help='Mode to run inside containers (ignored if individual tool flags provided)')

    # Individual tool switches - if any of these are provided they override --mode and the script will
    # run one invocation per enabled tool.
    parser.add_argument('--ape', action='store_true', help='Run mode=ape in containers')
    parser.add_argument('--fastbot', action='store_true', help='Run mode=fastbot in containers')
    parser.add_argument('--kea2', action='store_true', help='Run mode=kea2 in containers')
    parser.add_argument('--droidrun', action='store_true', help='Run mode=droidrun in containers')
    parser.add_argument('--monkey', action='store_true', help='Run mode=monkey in containers')

    # Common flags forwarded to run_experiment.py
    parser.add_argument('--test-only-one', action='store_true', help='If set, only the first app in the list will be tested')
    parser.add_argument('--skip-setup', action='store_true', help='Skip device setup steps (installing Appium/PCAPdroid/Droidrun)')
    parser.add_argument('--pcapdroid', action='store_true', help='Enable PCAPdroid setup on connected devices')
    parser.add_argument('--pcap-http-port', type=int, default=54320, help='Port to use for pcap http server (used when --pcapdroid set)')
    parser.add_argument('--socks5-address', type=str, default='127.0.0.1', help='The SOCKS5 proxy address (used when --pcapdroid set)')

    parser.add_argument('--dry-run', action='store_true', help='Print commands that would be executed but do not run them')

    return parser.parse_args()


def main():
    args = parse_args()
    docker_cli = find_docker_cli()
    containers = list_containers(docker_cli, args.filter)

    if not containers:
        logging.error("No running containers found matching filter '%s'", args.filter)
        sys.exit(1)

    # Determine modes to run
    modes = []
    if args.ape or args.fastbot or args.kea2 or args.droidrun or args.monkey:
        if args.ape:
            modes.append('ape')
        if args.fastbot:
            modes.append('fastbot')
        if args.kea2:
            modes.append('kea2')
        if args.droidrun:
            modes.append('droidrun')
        if args.monkey:
            modes.append('monkey')
    else:
        modes = [args.mode]

    logging.info('Found %d container(s) matching "%s": %s', len(containers), args.filter, containers)
    logging.info('Will start the following modes in each container: %s', modes)

    failures = []
    for c in containers:
        for m in modes:
            cmd = build_exec_command(docker_cli, c, m, args)
            logging.info('Starting mode=%s in container=%s', m, c)
            logging.debug('Command: %s', ' '.join(cmd))
            if args.dry_run:
                logging.info('DRY-RUN: %s', ' '.join(cmd))
                continue
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if res.returncode != 0:
                    logging.error('Failed to start experiment in container %s (mode=%s). Return code: %s', c, m, res.returncode)
                    logging.error('stdout: %s', res.stdout)
                    logging.error('stderr: %s', res.stderr)
                    failures.append((c, m, res.returncode, res.stderr or res.stdout))
                else:
                    logging.info('Started container %s (mode=%s) successfully', c, m)
            except Exception as e:
                logging.exception('Exception while launching experiment in container %s (mode=%s): %s', c, m, e)
                failures.append((c, m, 'exception', str(e)))

    if failures:
        logging.error('Completed with %d failure(s)', len(failures))
        for f in failures:
            logging.error('Failure: container=%s mode=%s error=%s', f[0], f[1], f[3])
        sys.exit(4)

    logging.info('All requested experiments started successfully (detached in containers)')


if __name__ == '__main__':
    main()

