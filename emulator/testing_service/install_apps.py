"""Install APKs found on a connected Android device.

This script optionally targets a specific device serial via --serial. When provided,
all adb calls will be executed against that device (-s <serial>). Results from
concurrent installs are aggregated in a thread-safe way.

New: add --all-devices to run the install on every connected device sequentially.
"""

import subprocess
import argparse
import os
import threading
import json
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict


def build_adb_cmd(serial=None, adb_args=None):
    """Return an adb command list. If serial is provided, include -s <serial>."""
    if adb_args is None:
        adb_args = []
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += adb_args
    return cmd


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
    try:
        cmd = build_adb_cmd(serial, ["shell", "find", "/", "-type", "f", "-name", "*.apk"])
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # filter out empty lines
        return [p for p in result.stdout.strip().split("\n") if p]
    except Exception as e:
        print(f"Error fetching APK files: {e}")
        return []


def filter_apk_install_list(apk_list):
    """
    Filters out APKs whose paths contain '/apex/' or '/overlay/'.

    :param apk_list: List of APK file paths.
    :return: Filtered list of APK file paths.
    """
    return [apk for apk in apk_list if "/apex/" not in apk and "/overlay/" not in apk]


def install_apk(apk_path, results, lock, serial=None):
    """
    Installs a single APK file on the connected Android device and tracks results.

    :param apk_path: Path to the APK file on the device.
    :param results: Dictionary to track success and failure counts and lists.
    :param lock: threading.Lock to protect results updates.
    :param serial: optional device serial to target via adb -s.
    """
    try:
        print(f"Installing {apk_path}")
        cmd = build_adb_cmd(serial, ["shell", "pm", "install", apk_path])
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0:
            print(f"Successfully installed {apk_path}")
            with lock:
                results["success"] += 1
                results.setdefault("installed", []).append(apk_path)
        else:
            error_message = (result.stderr.strip() or result.stdout.strip() or "Unknown error")
            print(f"Failed to install {apk_path}: {error_message}")
            with lock:
                results["failures"]["count"] += 1
                results["failures"]["details"][error_message] += 1
                results["failures"]["items"].append({"apk": apk_path, "error": error_message})
    except Exception as e:
        error_message = str(e)
        print(f"Error installing {apk_path}: {error_message}")
        with lock:
            results["failures"]["count"] += 1
            results["failures"]["details"][error_message] += 1
            results["failures"]["items"].append({"apk": apk_path, "error": error_message})


def run_install_for_device(serial, apk_list, max_workers):
    """
    Run parallel installs for a single device serial and return per-device results.
    """
    results = {
        "success": 0,
        "installed": [],
        "failures": {
            "count": 0,
            "details": defaultdict(int),
            "items": []
        }
    }
    lock = threading.Lock()
    if not apk_list:
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(install_apk, apk, results, lock, serial) for apk in apk_list]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"Worker raised an exception: {e}")
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
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
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
        default=min(32, (os.cpu_count() or 4) * 2),
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

    # Determine target devices
    if args.all_devices:
        devices = get_connected_devices()
        if not devices:
            print("No connected devices found.")
            return
    else:
        # single target: either explicit serial or default adb device (None)
        devices = [args.serial]

    overall = {"success": 0, "failures": 0}
    per_device_summary = {}

    for serial in devices:
        header = f"Device: {serial if serial else 'default'}"
        print("\n" + "=" * len(header))
        print(header)
        print("=" * len(header) + "\n")

        apk_files = get_apk_files(serial=serial)
        if not apk_files:
            print(f"No APK files found on device {serial if serial else 'default'}; skipping.")
            per_device_summary[serial if serial else 'default'] = {"skipped": True, "reason": "no_apks"}
            continue

        apk_filtered_list = filter_apk_install_list(apk_files)
        # If user requested a package, narrow candidates
        if args.package:
            candidates = find_apk_paths_for_package(serial, apk_filtered_list, args.package)
            if not candidates:
                print(f"Could not find APK for package {args.package} on device {serial if serial else 'default'}; skipping.")
                per_device_summary[serial if serial else 'default'] = {"skipped": True, "reason": "package_not_found", "package": args.package}
                continue
            apk_filtered_list = candidates

        print(f"Found {len(apk_filtered_list)} APK(s) to install on {serial if serial else 'default'}.")

        per_device_results = run_install_for_device(serial, apk_filtered_list, args.workers)

        # print per-device summary
        print("\n📊 Installation Summary for {}:".format(serial if serial else 'default'))
        print(f"  ✅ Successfully installed: {per_device_results['success']}")
        print(f"  ❌ Failed installations: {per_device_results['failures']['count']}")
        if per_device_results['failures']['count'] > 0:
            print("\n⚠️ Failure Details:")
            for error, count in per_device_results['failures']['details'].items():
                print(f"  {error}: {count} occurrences")

        overall['success'] += per_device_results['success']
        overall['failures'] += per_device_results['failures']['count']

        # store per-device results (serial string or 'default')
        per_device_summary[serial if serial else 'default'] = per_device_results

    # Overall summary
    print("\n=============================")
    print("Overall Summary:")
    print(f"  ✅ Total successful installs: {overall['success']}")
    print(f"  ❌ Total failures: {overall['failures']}")

    # Write JSON output if requested
    if args.output:
        out_obj = {
            "timestamp": datetime.datetime.utcnow().isoformat() + 'Z',
            "success_count": overall['success'],
            "failures_count": overall['failures'],
            "total_app_count": overall['success'] + overall['failures'],
            "per_device": per_device_summary
        }
        try:
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump(out_obj, f, ensure_ascii=False, indent=2)
            print(f"Wrote JSON results to {args.output}")
        except Exception as e:
            print(f"Failed to write JSON output: {e}")


if __name__ == "__main__":
    main()

