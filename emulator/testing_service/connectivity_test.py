#!/usr/bin/env python3
"""Simple connectivity check for an Android emulator/device.

Attempts to run: adb shell curl <URL>
If curl is not present on the device, falls back to wget or ping to check basic network reachability.

Usage:
  connectivity_test.py [-s SERIAL] [--url URL] [--retries N] [--timeout SEC] [--verbose]

Exit codes:
  0 - success (connectivity verified)
  1 - adb not found or device not available
  2 - connectivity check failed after retries
  3 - unexpected error
"""

import argparse
import shutil
import subprocess
import sys
import time
import logging


def adb_base_cmd(serial=None):
	adb = shutil.which('adb')
	if not adb:
		return None
	cmd = [adb]
	if serial:
		cmd.extend(['-s', serial])
	return cmd


def device_present(adb_base):
	try:
		proc = subprocess.run(adb_base + ['devices'], capture_output=True, text=True, timeout=10)
	except Exception:
		return False
	lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
	# first non-empty line is header; look for lines with device state
	for l in lines[1:]:
		if '\tdevice' in l or l.endswith('\tdevice'):
			return True
	return False


def run_adb_shell(adb_base, shell_cmd, timeout):
	full = adb_base + ['shell'] + shell_cmd
	try:
		proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
		return proc
	except subprocess.TimeoutExpired:
		return None
	except Exception:
		return None


def check_connectivity(adb_base, url, timeout, verbose=False):
	# First try: curl
	if verbose:
		logging.info('Checking for curl on device...')
	proc = run_adb_shell(adb_base, ['command', '-v', 'curl'], timeout=5)
	has_curl = False
	if proc and proc.returncode == 0 and proc.stdout.strip():
		has_curl = True

	if has_curl:
		if verbose:
			logging.info('Using curl to fetch URL headers')
		# Use a HEAD-like request to avoid downloading large bodies when possible
		proc = run_adb_shell(adb_base, ['curl', '-sS', '-I', '--max-time', str(timeout), url], timeout=timeout + 2)
		if proc is None:
			if verbose:
				logging.warning('curl timed out')
			return False, 'curl timeout'
		if proc.returncode == 0 and proc.stdout:
			out = proc.stdout.strip()
			if verbose:
				logging.info('curl stdout:\n%s', out)
			# Look for HTTP status line
			if 'HTTP/' in out or out.lower().find('date:') != -1:
				return True, 'curl success'
			# Some versions return body; treat any non-empty output as success
			return True, 'curl returned output'
		else:
			if verbose:
				logging.warning('curl failed, returncode=%s stderr=%s', getattr(proc, 'returncode', None), getattr(proc, 'stderr', None))

	# Second try: wget
	if verbose:
		logging.info('curl not available or failed; checking for wget on device...')
	proc = run_adb_shell(adb_base, ['command', '-v', 'wget'], timeout=5)
	has_wget = False
	if proc and proc.returncode == 0 and proc.stdout.strip():
		has_wget = True

	if has_wget:
		if verbose:
			logging.info('Using wget to fetch URL')
		proc = run_adb_shell(adb_base, ['wget', '--timeout=' + str(timeout), '--tries=1', '-qO-', url], timeout=timeout + 2)
		if proc is None:
			if verbose:
				logging.warning('wget timed out')
			return False, 'wget timeout'
		if proc.returncode == 0 and proc.stdout:
			if verbose:
				logging.info('wget stdout (truncated):\n%s', proc.stdout[:200])
			return True, 'wget success'
		else:
			if verbose:
				logging.warning('wget failed, returncode=%s', getattr(proc, 'returncode', None))

	# Final fallback: ping well-known IP (8.8.8.8)
	if verbose:
		logging.info('Falling back to ping test (8.8.8.8)')
	proc = run_adb_shell(adb_base, ['ping', '-c', '1', '-W', str(int(timeout)), '8.8.8.8'], timeout=timeout + 2)
	if proc and proc.returncode == 0:
		if verbose:
			logging.info('ping succeeded')
		return True, 'ping success'
	else:
		if verbose:
			logging.warning('ping failed or not available')
	return False, 'no connectivity'


def main():
	parser = argparse.ArgumentParser(description='Connectivity test for Android emulator/device')
	parser.add_argument('-s', '--serial', help='ADB device serial to target')
	parser.add_argument('--url', default='https://www.google.com', help='URL to fetch using curl on device')
	parser.add_argument('--retries', type=int, default=60, help='Number of retries')
	parser.add_argument('--timeout', type=int, default=10, help='Per-attempt timeout in seconds')
	parser.add_argument('--delay', type=int, default=10, help='Delay between retries in seconds')
	parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
	args = parser.parse_args()

# configure logging according to verbosity
	logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

	# If no serial provided and multiple devices are connected, pick the first
	if not args.serial:
		adb = shutil.which('adb')
		if not adb:
			logging.error('adb not found in PATH')
			sys.exit(1)
		try:
			proc = subprocess.run([adb, 'devices'], capture_output=True, text=True, timeout=10)
			lines = [l.strip() for l in (proc.stdout or '').splitlines() if l.strip()]
			serial = None
			for l in lines:
				if l.startswith('List of devices'):
					continue
				parts = l.split()
				if len(parts) >= 2 and parts[1] == 'device':
					serial = parts[0]
					break
			if serial:
				logging.info('No --serial provided; selecting first connected device: %s', serial)
				args.serial = serial
		except Exception:
			logging.exception('Failed to query adb devices')
			sys.exit(1)

	adb_base = adb_base_cmd(args.serial)
	if not adb_base:
		logging.error('adb not found in PATH')
		sys.exit(1)

	if not device_present(adb_base):
		logging.error('No connected adb device found for the given serial (or no devices at all).')
		sys.exit(1)

	for attempt in range(1, args.retries + 1):
		if args.verbose:
			logging.info('Attempt %d/%d...', attempt, args.retries)
		ok, reason = check_connectivity(adb_base, args.url, timeout=args.timeout, verbose=args.verbose)
		if ok:
			logging.info('Connectivity OK: %s', reason)
			sys.exit(0)
		else:
			logging.warning('Connectivity check failed: %s', reason)
			if attempt < args.retries:
				if args.verbose:
					logging.info('Waiting %d seconds before retry...', args.delay)
				time.sleep(args.delay)

	logging.error('Connectivity test failed after retries')
	sys.exit(2)


if __name__ == '__main__':
	try:
		main()
	except KeyboardInterrupt:
		logging.info('Interrupted by user')
		sys.exit(3)


