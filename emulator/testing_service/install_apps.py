"""Install APKs found on a connected Android device.

This script optionally targets a specific device serial via --serial. When provided,
all adb calls will be executed against that device (-s <serial>). Results from
concurrent installs are aggregated in a thread-safe way.

New: add --all-devices to run the install on every connected device sequentially.
"""
import keyword
import subprocess
import argparse
import sys
import os
import threading
import json
import datetime
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import re

SKIPPED_KEYWORD_LIST = ["overlay", "gpudriver", "__auto_generated", "Launcher", "SystemUI", "grilservice",
                        "ims.apk", "MultiDisplayProvider.apk", "CACertService.apk",
                        "NetworkStack", "HbmSVManager.apk", "ConnectivityThermalPowerManager",
                        "framework-res.apk", "uceShimService.apk", "datastatusnotification.apk",
                        "SecureElement.apk", "RcsService.apk", "QtiTelephonyService.apk",
                        "NfcNci.apk", "SSRestartDetector.apk", "CbrsNetworkMonitor.apk",
                        "secureui.apk", "ONS.apk", "TeleService.apk", "CneApp.apk"]


def normalize_install_error(msg: str) -> str:
    """Normalize/group raw install error messages into concise keys for aggregation.

    Returns a short machine-friendly key such as 'persistent_app_not_updateable',
    'device_offline', 'install_failed_invalid_apk', 'exception_NullPointerException', etc.
    """
    if not msg:
        return 'unknown_error'
    s = str(msg).strip()
    low = s.lower()

    # Persistent app / not updateable
    if 'persistent apps are not updateable' in low or 'persistent app' in low:
        return 'persistent_app_not_updateable'

    # Device offline / not found
    if 'device offline' in low:
        return 'device_offline'
    m = re.search(r"device\s+'([^']+)'\s+not found", s)
    if m:
        return 'device_not_found'
    if "error: device" in low and 'not found' in low:
        return 'device_not_found'

    # Explicit INSTALL_FAILED_* codes
    m = re.search(r'(INSTALL_FAILED_[A-Z0-9_]+)', s)
    if m:
        return m.group(1).lower()

    # Verification failure
    if 'verification' in low:
        return 'verification_failure'

    # Can't find service / cmd errors
    if "can't find service" in low or "cant find service" in low:
        return 'cmd_cant_find_service'

    # Numeric error codes like '-127:'
    m = re.search(r'^\s*([-]?\d+):', s)
    if m:
        return f'install_failure_code_{m.group(1)}'

    # Java exception types present in stack trace
    m = re.search(r'([A-Za-z0-9_]+Exception)', s)
    if m:
        return 'exception_' + m.group(1)

    # Fallback: take the first 80 chars of the first line
    first = s.splitlines()[0]
    key = first if len(first) <= 80 else first[:80] + '...'
    key = re.sub(r'\s+', ' ', key)
    return key.replace(' ', '_')

# configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# How many times to retry when adb reports 'device offline' before giving up
DEVICE_OFFLINE_MAX_RETRIES = 5
# Base wait seconds between device-offline retries (will be multiplied by attempt)
DEVICE_OFFLINE_WAIT_SECONDS = 5


def build_adb_cmd(serial=None, adb_args=None):
    """Return an adb command list. If serial is provided, include -s <serial>."""
    if adb_args is None:
        adb_args = []
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += adb_args
    return cmd


def adb_run(cmd, timeout=30, retries=1, backoff=2, capture_output=True):
    """Run an adb command with optional retries/backoff and timeout.

    Returns CompletedProcess or raises after final failure.
    """
    attempt = 0
    last_exc = None
    while attempt < retries:
        try:
            if capture_output:
                res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
            else:
                res = subprocess.run(cmd, timeout=timeout)
            return res
        except subprocess.TimeoutExpired as e:
            last_exc = e
            attempt += 1
            time.sleep(backoff * attempt)
        except Exception as e:
            last_exc = e
            attempt += 1
            time.sleep(backoff * attempt)
    # final attempt without catching so caller can handle
    if capture_output:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    else:
        return subprocess.run(cmd, timeout=timeout)


def check_device_free_kb(serial=None):
    """Return approximate free KB on /data partition or None if unknown."""
    try:
        cmd = build_adb_cmd(serial, ["shell", "df", "/data"])
        res = adb_run(cmd, timeout=10, retries=1)
        out = (res.stdout or '').strip()
        # Typical df output: Filesystem     1K-blocks    Used Available Use% Mounted on
        for line in out.splitlines():
            parts = [p for p in line.split() if p]
            if len(parts) >= 4 and parts[-1].startswith('/'):
                # Available is usually at index -3
                try:
                    avail = int(parts[-3])
                    return avail
                except Exception:
                    continue
        return None
    except Exception:
        return None


def get_connected_devices():
    """Return a list of connected adb device serials (those in 'device' state)."""
    try:
        result = subprocess.run(["adb", "devices"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        lines = [l.strip() for l in result.stdout.splitlines()]
        devices = []
        for line in lines[1:]:  # skip header
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        return devices
    except Exception:
        return []


def get_apk_files(serial=None):
    """
    Fetches the list of all APK files on the connected Android device (or
    a specific device when serial is provided).
    :return: List of APK file paths.
    """
    # Prefer querying installed packages (safer than scanning entire filesystem).
    apk_paths = set()
    # Keep track of package names that were listed by `pm list packages -f`
    # but whose file paths we chose not to treat as installable sources.
    skipped_package_names = []
    try:
        cmd = build_adb_cmd(serial, ["shell", "pm", "list", "packages", "-f"])  # returns package:<path>=<pkg>
        res = adb_run(cmd, timeout=20, retries=2)
        out = (res.stdout or '')
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            # format: package:/data/app/.../base.apk=com.example
            if line.startswith('package:') and '=' in line:
                parts = line.split('=', 1)
                path = parts[0].split(':', 1)[1]
                pkg_name = parts[1].strip()
                if path.startswith('/data/'):
                    logging.debug('Skipping installed-package path under /data/: %s', path)
                    if pkg_name:
                        skipped_package_names.append(pkg_name)
                elif any(pack_keyword in path for pack_keyword in SKIPPED_KEYWORD_LIST):
                    logging.debug('Skipping path due to keyword filter: %s', path)
                    if pkg_name:
                        skipped_package_names.append(pkg_name)
                else:
                    apk_paths.add(path)
    except Exception as e:
        logger.warning('pm list packages -f failed: %s', e)

    # Also check /sdcard and /storage for APK files (user downloads) but limit search depth
    try:
        cmd = build_adb_cmd(serial, ["shell", "find", "/sdcard", "/storage", "-maxdepth", "3", "-type", "f", "-name", "*.apk"])
        res = adb_run(cmd, timeout=30, retries=1)
        out = (res.stdout or '')
        for p in out.splitlines():
            p = p.strip()
            if p:
                apk_paths.add(p)
    except Exception:
        # non-fatal, just continue with whatever we found
        pass

    return sorted(apk_paths), skipped_package_names


def filter_apk_install_list(apk_list):
    """
    Filters out APKs whose paths contain '/apex/' or '/overlay/'.

    :param apk_list: List of APK file paths.
    :return: Filtered list of APK file paths.
    """
    filtered = [apk for apk in apk_list if "/apex/" not in apk and "/overlay/" not in apk]
    # Determine which APKs were filtered out and log them so they appear in the
    # normal logfile output. Use INFO level so they are captured by the
    # script's default logging configuration.
    skipped = [apk for apk in apk_list if apk not in filtered]
    if skipped:
        # Log count and first few items to avoid overly long single-line logs
        try:
            preview = ', '.join(skipped[:10])
            if len(skipped) > 10:
                preview = preview + ', ...'
        except Exception:
            preview = str(skipped)
        logger.info('Skipped %d APK(s) due to /apex/ or /overlay/ paths: %s', len(skipped), preview)
    return filtered


def install_apk(apk_path, results, lock, serial=None, stop_event=None):
    """
    Installs a single APK file on the connected Android device and tracks results.

    :param apk_path: Path to the APK file on the device.
    :param results: Dictionary to track success and failure counts and lists.
    :param lock: threading.Lock to protect results updates.
    :param serial: optional device serial to target via adb -s.
    """
    try:
        # If a global stop event was signaled (e.g., device went offline), skip work
        if stop_event is not None and stop_event.is_set():
            logger.info('Skipping %s because stop event is set (device offline)', apk_path)
            return
        logger.info('Installing %s', apk_path)
        # Use pm install -r to allow reinstallation; run with retries and timeout
        cmd = build_adb_cmd(serial, ["shell", "pm", "install", "-r", apk_path])
        attempts = 2
        for attempt in range(1, attempts + 1):
            try:
                res = adb_run(cmd, timeout=120, retries=1)
            except Exception as e:
                logger.warning('Install attempt %d for %s raised: %s', attempt, apk_path, e)
                if attempt == attempts:
                    raise
                time.sleep(2 * attempt)
                continue

            out = (res.stdout or '').strip()
            err = (res.stderr or '').strip()
            combined = (out + '\n' + err).lower()
            # If adb reports 'device offline' we should abort further installs immediately
            if 'device offline' in combined:
                logger.warning('Device reported offline while installing %s (attempt %d/%d). Will retry up to %d times.', apk_path, attempt, attempts, DEVICE_OFFLINE_MAX_RETRIES)
                # Try to wait for the device to come back online a few times before giving up
                offline_ok = False
                for off_try in range(1, DEVICE_OFFLINE_MAX_RETRIES + 1):
                    wait = DEVICE_OFFLINE_WAIT_SECONDS * off_try
                    logger.info('Waiting %s seconds before offline-retry %d/%d for %s', wait, off_try, DEVICE_OFFLINE_MAX_RETRIES, apk_path)
                    time.sleep(wait)
                    # check connected devices
                    try:
                        devs = get_connected_devices()
                    except Exception:
                        devs = []
                    if serial:
                        if serial in devs:
                            logger.info('Device %s is back online (offline-retry %d). Retrying install.', serial, off_try)
                            offline_ok = True
                            break
                        else:
                            logger.info('Device %s still not present (offline-retry %d/%d).', serial, off_try, DEVICE_OFFLINE_MAX_RETRIES)
                    else:
                        if devs:
                            logger.info('At least one adb device found again (offline-retry %d). Retrying install.', off_try)
                            offline_ok = True
                            break
                        else:
                            logger.info('No adb devices found yet (offline-retry %d/%d).', off_try, DEVICE_OFFLINE_MAX_RETRIES)

                if not offline_ok:
                    logger.error('Device remained offline after %d retries while installing %s; aborting further installs for this device.', DEVICE_OFFLINE_MAX_RETRIES, apk_path)
                    # mark an abort flag in results and signal stop_event so other workers can stop
                    with lock:
                        results.setdefault('aborted_offline', True)
                    if stop_event is not None:
                        stop_event.set()
                    # record this specific failure
                    with lock:
                        results['failures']['count'] += 1
                        key = 'device_offline'
                        results['failures']['details'][key] += 1
                        results['failures']['items'].append({"apk": apk_path, "error": "device offline", "group": key})
                    return
                # if offline_ok is True, continue and retry install attempts
            if res.returncode == 0 and ('Success' in out or 'Success' in err or out == ''):
                logger.info('Successfully installed %s', apk_path)
                with lock:
                    results["success"] += 1
                    results.setdefault("installed", []).append(apk_path)
                break
            else:
                error_message = err or out or 'Unknown error'
                logger.warning('Failed to install %s (attempt %d): %s', apk_path, attempt, error_message)
                if attempt == attempts:
                    # Normalize/group the error for clearer aggregation
                    key = normalize_install_error(error_message)
                    # Treat persistent-app-not-updateable as a non-fatal condition and count as success
                    if key == 'persistent_app_not_updateable':
                        logger.info('Treating persistent app install error as success for %s', apk_path)
                        with lock:
                            results["success"] += 1
                            results.setdefault("installed", []).append(apk_path)
                            results["treated_as_success"] += 1
                    else:
                        with lock:
                            results["failures"]["count"] += 1
                            results["failures"]["details"][key] += 1
                            results["failures"]["items"].append({"apk": apk_path, "error": error_message, "group": key})
                else:
                    # small backoff and retry
                    time.sleep(1 * attempt)
        # gentle delay between installs to reduce load
        time.sleep(0.25)
    except Exception as e:
        error_message = str(e)
        # If the exception text indicates device offline, set stop_event as well
        if 'device offline' in error_message.lower():
            logger.error('Device reported offline during install of %s; aborting further installs for this device.', apk_path)
            if stop_event is not None:
                stop_event.set()
            with lock:
                results.setdefault('aborted_offline', True)
        logger.exception('Error installing %s: %s', apk_path, error_message)
        key = normalize_install_error(error_message)
        # Treat persistent-app-not-updateable as success
        if key == 'persistent_app_not_updateable':
            logger.info('Treating persistent app install exception as success for %s', apk_path)
            with lock:
                results["success"] += 1
                results.setdefault("installed", []).append(apk_path)
                results["treated_as_success"] += 1
        else:
            with lock:
                results["failures"]["count"] += 1
                results["failures"]["details"][key] += 1
                results["failures"]["items"].append({"apk": apk_path, "error": error_message, "group": key})


def run_install_for_device(serial, apk_list, max_workers):
    """
    Run parallel installations for a single device serial and return per-device results.
    """
    results = {
        "success": 0,
        "installed": [],
        "skipped": 0,
        "treated_as_success": 0,
        "failures": {
            "count": 0,
            "details": defaultdict(int),
            "items": []
        }
    }
    lock = threading.Lock()
    stop_event = threading.Event()
    if not apk_list:
        return results

    # Cap the number of workers to avoid overwhelming the device/emulator.
    cap = max(1, min(max_workers, 1, len(apk_list)))
    with ThreadPoolExecutor(max_workers=cap) as executor:
        futures = [executor.submit(install_apk, apk, results, lock, serial, stop_event) for apk in apk_list]
        for f in as_completed(futures):
            # If a device-offline event occurred, try to cancel remaining futures and stop waiting
            if stop_event.is_set():
                logger.info('Stop event detected; attempting to cancel remaining tasks for device %s', serial if serial else 'default')
                for fut in futures:
                    if not fut.done():
                        try:
                            fut.cancel()
                        except Exception:
                            pass
                break
            try:
                f.result()
            except Exception as e:
                logger.exception('Worker raised an exception: %s', e)
    # convert defaultdict to normal dict for portability
    results["failures"]["details"] = dict(results["failures"]["details"])
    return results


def find_apk_paths_for_package(serial, apk_list, package_name):
    """Resolve candidate APK paths for a package on a given device.

    First try `pm path <package>` on the device (preferred). If that yields
    paths, return them. Otherwise, fall back to scanning the discovered
    apk_list for filenames or paths that contain the package name or its last
    segment.
    """
    candidates = []
    # 1) Try pm path
    try:
        cmd = build_adb_cmd(serial, ["shell", "pm", "path", package_name])
        proc = adb_run(cmd, timeout=10, retries=2)
        if proc.returncode == 0 and proc.stdout:
            for line in proc.stdout.splitlines():
                line = line.strip()
                if line.startswith('package:'):
                    p = line.split(':', 1)[1].strip()
                    if p:
                        candidates.append(p)
            if candidates:
                return candidates
    except Exception:
        pass

    # 2) Fallback: search discovered APK list for matches
    short = package_name.split('.')[-1].lower()
    for p in apk_list:
        if package_name.lower() in p.lower() or short in os.path.basename(p).lower():
            candidates.append(p)
    return candidates


def parse_args():
    parser = argparse.ArgumentParser(description="Install APKs found on a device or all connected devices")
    parser.add_argument(
        "-s", "--serial",
        required=False,
        help="Target device serial (adb -s <serial>). If omitted, will query the default adb device.")
    parser.add_argument(
        "-a", "--all-devices",
        action="store_true",
        help="Install APKs on all connected devices sequentially (one device after another)")
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=4,
        help="Number of parallel installer workers (default: 2 x CPU cores, capped 32)")
    parser.add_argument(
        "-o", "--output",
        required=False,
        help="Optional JSON output file to write installation results to (e.g., results.json)")
    parser.add_argument(
        "-p", "--package",
        required=False,
        help="Optional package name to install (e.g., com.example.app). If set, the script will attempt to locate APK(s) for this package on the device and install only those.")
    return parser.parse_args()


def main():
    args = parse_args()
    # Determine target device: prefer explicit serial, otherwise pick first connected device
    if args.serial:
        target_serial = args.serial
    else:
        devs = get_connected_devices()
        if not devs:
            logger.info('No connected devices found.')
            return
        if len(devs) > 1:
            logger.info('Multiple adb devices detected; using first device: %s', devs[0])
        target_serial = devs[0]

    serial = target_serial
    header = f"Device: {serial if serial else 'default'}"
    logger.info('\n%s', '=' * len(header))
    logger.info(header)
    logger.info('%s\n', '=' * len(header))

    apk_files, skipped_pkg_names = get_apk_files(serial=serial)
    if not apk_files:
        if skipped_pkg_names:
            try:
                preview_names = ', '.join(skipped_pkg_names[:10])
                if len(skipped_pkg_names) > 10:
                    preview_names = preview_names + ', ...'
            except Exception:
                preview_names = str(skipped_pkg_names)
            logger.info('No installable APK file paths found on device %s; pm listed these packages but their paths were skipped as sources: %s', serial if serial else 'default', preview_names)
            per_device_results = {"skipped": True, "reason": "no_installable_apks", "skipped_package_names": skipped_pkg_names}
        else:
            logger.info('No APK files found on device %s; skipping.', serial if serial else 'default')
            per_device_results = {"skipped": True, "reason": "no_apks"}

        # write output if requested
        if args.output:
            out_obj = {
                "timestamp": datetime.datetime.utcnow().isoformat() + 'Z',
                "device": serial,
                "success_count": 0,
                "failures_count": 0,
                "treated_as_success": 0,
                "total_app_count": 0,
                "results": per_device_results
            }
            try:
                with open(args.output, 'w', encoding='utf-8') as f:
                    json.dump(out_obj, f, ensure_ascii=False, indent=2)
                logger.info('Wrote JSON results to %s', args.output)
            except Exception as e:
                logger.exception('Failed to write JSON output: %s', e)
        return

    apk_filtered_list = filter_apk_install_list(apk_files)
    skipped_apks = [p for p in apk_files if p not in apk_filtered_list]
    if skipped_pkg_names:
        try:
            preview_pkg = ', '.join(skipped_pkg_names[:10])
            if len(skipped_pkg_names) > 10:
                preview_pkg = preview_pkg + ', ...'
        except Exception:
            preview_pkg = str(skipped_pkg_names)
        logger.info('Device %s: %d package(s) were listed by pm but skipped as install sources: %s', serial if serial else 'default', len(skipped_pkg_names), preview_pkg)
    if skipped_apks:
        try:
            preview = ', '.join(skipped_apks[:10])
            if len(skipped_apks) > 10:
                preview = preview + ', ...'
        except Exception:
            preview = str(skipped_apks)
        logger.info('Device %s: %d APK(s) were skipped by filters: %s', serial if serial else 'default', len(skipped_apks), preview)

    free_kb = check_device_free_kb(serial)
    if free_kb is not None and free_kb < 50 * 1024:
        logger.warning('Device %s has low free space (%d KB). Skipping installs to avoid instability.', serial if serial else 'default', free_kb)
        per_device_results = {
            "skipped": True,
            "reason": "low_storage",
            "available_kb": free_kb,
            "skipped_items": apk_filtered_list if apk_filtered_list else skipped_apks
        }
        # write output if requested
        if args.output:
            out_obj = {
                "timestamp": datetime.datetime.utcnow().isoformat() + 'Z',
                "device": serial,
                "success_count": 0,
                "failures_count": 0,
                "treated_as_success": 0,
                "total_app_count": 0,
                "results": per_device_results
            }
            try:
                with open(args.output, 'w', encoding='utf-8') as f:
                    json.dump(out_obj, f, ensure_ascii=False, indent=2)
                logger.info('Wrote JSON results to %s', args.output)
            except Exception as e:
                logger.exception('Failed to write JSON output: %s', e)
        return

    if args.package:
        candidates = find_apk_paths_for_package(serial, apk_filtered_list, args.package)
        if not candidates:
            logger.info('Could not find APK for package %s on device %s; skipping.', args.package, serial if serial else 'default')
            per_device_results = {"skipped": True, "reason": "package_not_found", "package": args.package}
            if args.output:
                out_obj = {
                    "timestamp": datetime.datetime.utcnow().isoformat() + 'Z',
                    "device": serial,
                    "success_count": 0,
                    "failures_count": 0,
                    "treated_as_success": 0,
                    "total_app_count": 0,
                    "results": per_device_results
                }
                try:
                    with open(args.output, 'w', encoding='utf-8') as f:
                        json.dump(out_obj, f, ensure_ascii=False, indent=2)
                    logger.info('Wrote JSON results to %s', args.output)
                except Exception as e:
                    logger.exception('Failed to write JSON output: %s', e)
            return
        apk_filtered_list = candidates

    logger.info('Found %d APK(s) to install on %s.', len(apk_filtered_list), serial if serial else 'default')

    per_device_results = run_install_for_device(serial, apk_filtered_list, args.workers)
    if skipped_apks:
        per_device_results.setdefault('skipped_items', skipped_apks)
    if skipped_pkg_names:
        per_device_results.setdefault('skipped_package_names', skipped_pkg_names)
    try:
        per_device_results['skipped'] = int(len(skipped_apks) + len(skipped_pkg_names))
    except Exception:
        per_device_results.setdefault('skipped', 0)

    # print single-device summary
    logger.info('\n📊 Installation Summary for %s:', serial if serial else 'default')
    logger.info('  ✅ Successfully installed: %d', per_device_results['success'])
    logger.info('  ℹ️ Treated-as-success (persistent): %d', per_device_results.get('treated_as_success', 0))
    logger.info('  ❌ Failed installations: %d', per_device_results['failures']['count'])
    if per_device_results['failures']['count'] > 0:
        logger.warning('\n⚠️ Failure Details:')
        for error, count in per_device_results['failures']['details'].items():
            logger.warning('  %s: %d occurrences', error, count)

    overall = {
        'success': per_device_results['success'],
        'failures': per_device_results['failures']['count'],
        'treated_as_success': per_device_results.get('treated_as_success', 0)
    }

    # write final output if requested
    if args.output:
        out_obj = {
            "timestamp": datetime.datetime.utcnow().isoformat() + 'Z',
            "device": serial,
            "success_count": overall['success'],
            "failures_count": overall['failures'],
            "treated_as_success": overall.get('treated_as_success', 0),
            "total_app_count": overall['success'] + overall['failures'],
            "results": per_device_results
        }
        try:
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump(out_obj, f, ensure_ascii=False, indent=2)
            logger.info('Wrote JSON results to %s', args.output)
        except Exception as e:
            logger.exception('Failed to write JSON output: %s', e)

    # Overall summary
    logger.info('\n=============================')
    logger.info('Overall Summary:')
    logger.info('  ✅ Total successful installs: %d', overall['success'])
    logger.info('  ❌ Total failures: %d', overall['failures'])



if __name__ == "__main__":
    main()

