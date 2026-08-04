import json
import logging
import os
import shutil
import subprocess
import time
import argparse
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, 'out')
PID_FILE_PATH = os.path.join(OUT_DIR, 'tcpdump_device.pid')
REMOTE_PCAP_PATH = "/data/local/tmp/tcpdump.pcap"


def _get_first_adb_device() -> str:
    """Helper to consistently find the first available ADB device serial."""
    if not shutil.which('adb'):
        logging.error('adb binary not found in PATH')
        return ""
    try:
        out = subprocess.run(['adb', 'devices'], capture_output=True, text=True, timeout=10)
        lines = [line.strip() for line in (out.stdout or '').splitlines() if line.strip()]
        for line in lines:
            if line.startswith('List of devices'):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == 'device':
                return parts[0]
    except Exception:
        logging.exception('Failed to execute adb devices')
    return ""


def _ensure_adb_root(serial: str) -> bool:
    """Best-effort attempt to toggle adb root. Returns true if successful."""
    try:
        res = subprocess.run(['adb', '-s', serial, 'root'], capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and "cannot run as root" not in (res.stdout or ""):
            logging.info('adb root: SUCCESS on device %s', serial)
            return True
        logging.debug('adb root: Not available/Production build on device %s', serial)
    except Exception:
        logging.exception('Exception during adb root attempt')
    return False


def start_tcpdump() -> bool:
    """Configures device NFLOG rules and starts background tcpdump securely."""
    logging.info('Setting up tcpdump')

    # 1. Device Discovery
    serial = _get_first_adb_device()
    if not serial:
        logging.error('No responsive adb device found to start tcpdump.')
        return False
    logging.info('Selected target adb device: %s', serial)

    # Check if TCPDump is not already started
    try:
        _ensure_adb_root(serial)

        # 'ps -A' covers newer Android versions, falling back to standard 'ps' for older ones.
        # We look for tcpdump commands targeting nflog:1 while ignoring our own grep command.
        ps_cmd = 'ps -A 2>/dev/null || ps'
        grep_cmd = f'{ps_cmd} | grep "[t]cpdump.*nflog:1"'

        _ensure_adb_root(serial)
        check_proc = subprocess.run(
            ['adb', '-s', serial, 'shell', grep_cmd],
            capture_output=True, text=True, timeout=10
        )

        # If grep found a matching line
        if check_proc.returncode == 0 and check_proc.stdout.strip():
            ps_line = check_proc.stdout.strip().splitlines()[0]
            # Split the ps line to find the PID (usually the 2nd column)
            # Example ps output: root      12345 1     4524   1200  sys_epoll_ b7ee0000 s tcpdump
            parts = ps_line.split()
            if len(parts) >= 2:
                # Android 'ps' puts PID in the second column. Let's verify it's a number.
                live_pid = parts[1] if parts[1].isdigit() else parts[2]  # Fallback just in case of weird columns

                if live_pid.isdigit():
                    logging.info('Live check: tcpdump is actively running on device %s (PID: %s).', serial, live_pid)

                    # Cross-reference with the local PID file
                    pid_file_matches = False
                    if os.path.exists(PID_FILE_PATH):
                        try:
                            with open(PID_FILE_PATH, 'r', encoding='utf-8') as f:
                                lines = [line.strip() for line in f.readlines() if line.strip()]
                                if len(lines) >= 2 and lines[0] == serial and lines[1] == live_pid:
                                    pid_file_matches = True
                        except Exception:
                            logging.warning('PID file existed but was unreadable.')

                    if pid_file_matches:
                        logging.info('Local tracking file matches live device state. Resuming safely.')
                    else:
                        logging.warning('Tracking file missing or out-of-sync. Re-aligning local PID file.')
                        os.makedirs(OUT_DIR, exist_ok=True)
                        with open(PID_FILE_PATH, 'w', encoding='utf-8') as f:
                            f.write(f"{serial}\n{live_pid}\n")

                    return True  # Exit early and leave the running capture alone

    except Exception:
        logging.exception('Error during early ADB device process check; defaulting to clean setup.')

    # 2. Privileges & Firewall Setup
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            _ensure_adb_root(serial)

            # Optimized with explicit su root and thresholds for stability
            ipt_cmd = (
                'su root sh -c "'
                'iptables -t mangle -C OUTPUT -j NFLOG --nflog-group 1 2>/dev/null || '
                'iptables -t mangle -I OUTPUT 1 -j NFLOG --nflog-group 1 --nflog-threshold 10'
                '"'
            )

            res = subprocess.run(
                ['adb', '-s', serial, 'shell', ipt_cmd],
                capture_output=True, text=True, timeout=10
            )

            # Check if iptables command actually succeeded on the device
            if res.returncode == 0:
                logging.info('Verified/Installed iptables NFLOG rule on attempt %d/%d', attempt, max_attempts)
                break  # Success! Break out of the retry loop.
            else:
                error_msg = (res.stderr or res.stdout).strip()
                logging.warning(
                    'Attempt %d/%d failed. iptables exited with code %d. Error: %s',
                    attempt, max_attempts, res.returncode, error_msg
                )

        except Exception as e:
            logging.warning('Attempt %d/%d raised an exception: %s', attempt, max_attempts, str(e))

        # If we haven't broken out of the loop and this wasn't the last attempt, wait before retrying
        if attempt < max_attempts:
            time.sleep(1.5)  # Short cooling-off period before retrying
    else:
        # This block executes ONLY if the loop finishes normally without hitting the 'break' statement
        logging.error('Failed to apply iptables NFLOG rule after %d attempts.', max_attempts)
        return False

    # 3. Clean up any residual tracking artifacts
    if os.path.exists(PID_FILE_PATH):
        try:
            os.remove(PID_FILE_PATH)
        except Exception:
            pass

    # 4. Fire up tcpdump in the background safely
    remote_workdir = '/data/local/tmp'
    # We remove the old pcap first to guarantee our validation loop checks fresh data
    # 4. Fire up tcpdump in the background safely
    start_cmd = (
        f"rm -f {REMOTE_PCAP_PATH} && mkdir -p {remote_workdir} && cd {remote_workdir} && "
        f"su root sh -c 'nohup tcpdump -i nflog:1 -w {REMOTE_PCAP_PATH} > nohup.out 2>&1 & echo $!'"
    )
    try:
        _ensure_adb_root(serial)
        logging.info('Executing tcpdump start command on device %s with command: %s', serial, start_cmd)
        res = subprocess.run(['adb', '-s', serial, 'shell', start_cmd], capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            logging.error('ADB rejected background pipeline command: %s', (res.stderr or res.stdout).strip())
            return False

        output = (res.stdout or '').strip()
        pid = output.splitlines()[-1].strip() if output else ""

        if not pid or not pid.isdigit():
            logging.warning('Could not extract a valid PID from command output: "%s"', output)
            return False

        # Persist tracking metrics (serial + pid)
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(PID_FILE_PATH, 'w', encoding='utf-8') as f:
            f.write(f"{serial}\n{pid}\n")
        logging.info('Background tcpdump initialized (Device: %s, PID: %s)', serial, pid)

    except Exception:
        logging.exception('Failed during tcpdump generation sequence')
        return False

    # 5. Fast-polling File Generation Check (Max 5 seconds wait instead of 15 minutes!)
    for check in range(10):
        time.sleep(0.5)
        try:
            ls_res = subprocess.run(['adb', '-s', serial, 'shell', f'ls -l {REMOTE_PCAP_PATH}'], capture_output=True,
                                    text=True, timeout=5)
            if ls_res.returncode == 0 and (ls_res.stdout or '').strip():
                logging.info('Confirmed: Remote pcap generation verified active.')
                return True
        except Exception:
            pass

    logging.error('tcpdump failed to spawn file descriptor at %s within timeout.', REMOTE_PCAP_PATH)
    return False


def stop_tcpdump() -> bool:
    """Stops tcpdump via PID / Process name signatures on the target ADB device,

    pulls captured pcaps, optional SSL keylogs, and records package UID maps.
    """
    logging.info('Stopping tcpdump execution layout')

    # -------------------------------------------------------------------------
    # 1. Parse execution markers & select target device
    # -------------------------------------------------------------------------
    serial = _get_first_adb_device()
    pid = None

    if os.path.exists(PID_FILE_PATH):
        try:
            with open(PID_FILE_PATH, 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f if line.strip()]
                if len(lines) > 1:
                    pid = lines[1]
        except Exception:
            logging.exception('Failed parsing data from active pid tracking structure')

    if not serial:
        logging.error('Cannot execute termination request: No valid target devices found.')
        return False

    # -------------------------------------------------------------------------
    # 2. Terminate Process (Ordered fallbacks: root -> pkill -> pid-kill -> su)
    # -------------------------------------------------------------------------
    try:
        root_res = subprocess.run(['adb', '-s', serial, 'root'], capture_output=True, text=True, timeout=10)
        if root_res.returncode == 0:
            logging.info('adb root: OK on device %s', serial)
    except Exception:
        logging.exception('adb root attempt encountered unexpected failure')

    termination_commands = [
        ['adb', '-s', serial, 'shell', 'pkill -2 tcpdump'],
    ]
    if pid:
        termination_commands.append(['adb', '-s', serial, 'shell', f'kill -2 {pid}'])
    termination_commands.append(['adb', '-s', serial, 'shell', 'su', '-c', 'pkill -2 tcpdump'])

    killed = False
    for cmd in termination_commands:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                logging.info('Sent kill signal safely utilizing command: %s', " ".join(cmd))
                killed = True
                break
            else:
                logging.debug('Command [%s] non-zero: %s', " ".join(cmd), (res.stderr or res.stdout).strip())
        except Exception:
            continue

    if not killed:
        logging.warning("Signal requests completed, verifying if process terminated automatically...")

    # Allow buffer space for memory pipes to flush on filesystem
    time.sleep(1.5)

    # -------------------------------------------------------------------------
    # 3. Secure File Retrieval (PCAP + SSL Keylogs)
    # -------------------------------------------------------------------------
    local_pcaps_dir = os.path.join(OUT_DIR, "pcaps")
    os.makedirs(local_pcaps_dir, exist_ok=True)
    local_pcap_file = os.path.join(local_pcaps_dir, f'tcpdump_{serial}.pcap')
    pcap_pulled_successfully = False

    try:
        res = subprocess.run(['adb', '-s', serial, 'pull', REMOTE_PCAP_PATH, local_pcap_file],
                             capture_output=True, text=True, timeout=60)
        if res.returncode == 0 and os.path.exists(local_pcap_file) and os.path.getsize(local_pcap_file) > 0:
            logging.info('Successfully pulled populated tcpdump pcap target to: %s', local_pcap_file)
            pcap_pulled_successfully = True
        else:
            logging.error('Failed pulling pcap or zero-byte file caught. adb logs: %s',
                          (res.stderr or res.stdout).strip())
    except Exception:
        logging.exception('Fatal unexpected termination occurred pulling runtime PCAP assets.')

    _pull_ssl_keylogs(serial, local_pcaps_dir)
    # -------------------------------------------------------------------------
    # 4. Cleanup Execution Tracks
    # -------------------------------------------------------------------------
    if pcap_pulled_successfully:
        if os.path.exists(PID_FILE_PATH):
            try:
                os.remove(PID_FILE_PATH)
            except Exception:
                logging.debug('Failed to remove tracking file %s', PID_FILE_PATH)

        subprocess.run(['adb', '-s', serial, 'shell', f'rm -f {REMOTE_PCAP_PATH}'], capture_output=True, timeout=5)
        return True

    return False


def _pull_ssl_keylogs(serial: str, output_dir: str):
    """Scan known alternative device paths and retrieve available SSL key logs."""
    local_ssl = os.path.join(output_dir, f'sslkeylog_{serial}.log')
    remote_candidates = [
        '/storage/emulated/0/Download/sslkeylog.log',
        '/sdcard/Download/sslkeylog.log',
        '/sdcard/sslkeylog.log',
        '/data/local/tmp/sslkeylog.log',
        '/data/misc/ssl/sslkeylog.log',
    ]

    for remote in remote_candidates:
        try:
            ls_res = subprocess.run(['adb', '-s', serial, 'shell', 'ls', '-l', remote],
                                    capture_output=True, text=True, timeout=5)
            if ls_res.returncode != 0:
                continue

            pull_res = subprocess.run(['adb', '-s', serial, 'pull', remote, local_ssl],
                                      capture_output=True, text=True, timeout=20)
            if pull_res.returncode == 0:
                logging.info('Pulled SSL keylog from device %s -> %s', serial, local_ssl)
                return
        except Exception:
            logging.debug('Exception while attempting to check/pull SSL key from %s', remote, exc_info=True)

    logging.info('No SSL keylog discovered at targeted device vectors; skipping.')


def _collect_package_uid_mapping(serial: str):
    """Retrieve package -> UID lists and store results atomically using safe-write blocks."""
    if not serial:
        serial = _get_first_adb_device()
    try:
        res = subprocess.run(['adb', '-s', serial, 'shell', 'pm', 'list', 'packages', '-U'],
                             capture_output=True, text=True, timeout=20)
        pkg_map = {}
        pm_success = (res.returncode == 0)
        pm_error = None if pm_success else (res.stderr or res.stdout or '').strip()

        if pm_success:
            for line in (res.stdout or '').splitlines():
                line = line.strip()
                if not line.startswith('package:'):
                    continue
                pkg, uid = None, None
                for part in line.split():
                    if part.startswith('package:'):
                        pkg = part.split('package:', 1)[1]
                    if part.startswith('uid:'):
                        try:
                            uid = int(part.split('uid:', 1)[1])
                        except ValueError:
                            pass
                if pkg:
                    pkg_map[pkg] = uid

        mapping_content = {'success': pm_success, 'error': pm_error, 'mapping': pkg_map}
        mapping_file = os.path.join(OUT_DIR, f'package_uids_{serial}.json')
        tmp_path = f"{mapping_file}.tmp"

        for attempt in range(1, 4):
            try:
                os.makedirs(os.path.dirname(mapping_file), exist_ok=True)
                with open(tmp_path, 'w', encoding='utf-8') as mf:
                    json.dump(mapping_content, mf, indent=2)
                    mf.flush()
                    try:
                        os.fsync(mf.fileno())
                    except OSError:
                        pass
                os.replace(tmp_path, mapping_file)
                logging.info('Wrote package->UID mapping to %s (attempt %d)', mapping_file, attempt)
                return
            except Exception as e:
                logging.warning('Attempt %d failed writing package metadata mappings: %s', attempt, e)
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                time.sleep(0.5 * attempt)
    except Exception:
        logging.exception('Failed tracking package data allocations.')


def _configure_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format='%(asctime)s %(levelname)s: %(message)s')


def main():
    parser = argparse.ArgumentParser(description='Start/stop tcpdump via adb')
    parser.add_argument('command', choices=['start', 'stop'], help='Action to perform')
    parser.add_argument('-v','--verbose', action='store_true', help='Enable debug logging')
    args = parser.parse_args()
    _configure_logging(args.verbose)
    if args.command == 'start':
        ok = start_tcpdump()
        logging.info('TCPDump setup %s', 'succeeded' if ok else 'failed')
        raise SystemExit(0 if ok else 1)
    elif args.command == 'stop':
        ok = stop_tcpdump()
        logging.info('TCPDump stop %s', 'succeeded' if ok else 'failed')
        raise SystemExit(0 if ok else 1)
    else:
        logging.error('Unknown command: %s', args.command)
        raise SystemExit(1)

if __name__ == '__main__':
    main()








