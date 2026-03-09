#!/usr/bin/env python3
"""install_appium.py

CLI to install Appium's uiautomator2 driver on the host (via `appium driver install uiautomator2`)
and optionally install uiautomator2 server/test APKs on one device or all connected devices.

Usage examples:
  # install driver on host only
  ./install_appium.py --driver-install

  # install driver on host and push apks to all devices
  ./install_appium.py --driver-install --all --apk-server /path/to/appium-uiautomator2-server.apk --apk-test /path/to/appium-uiautomator2-test.apk

  # install apks to a single device
  ./install_appium.py --device emulator-5554 --apk-server ... --apk-test ...

Notes:
- Installing the uiautomator2 driver on the host requires Appium CLI (the `appium` command) to be available in PATH.
- If you supply APK paths they will be pushed and installed on the device(s). If no APKs are provided, the script only installs the host driver (if requested) and checks package presence on devices.
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from typing import List, Optional

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"


def run_cmd(cmd: List[str], check: bool = True, capture: bool = True, env=None):
    """Run command and return (returncode, stdout, stderr). Raises CalledProcessError if check and rc !=0."""
    logging.debug("Running command: %s", shlex.join(cmd))
    completed = subprocess.run(cmd, stdout=subprocess.PIPE if capture else None,
                               stderr=subprocess.PIPE if capture else None,
                               text=True, env=env)
    out = completed.stdout if capture else None
    err = completed.stderr if capture else None
    logging.debug("Exit %s stdout=%s stderr=%s", completed.returncode, (out or "").strip(), (err or "").strip())
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, cmd, output=out, stderr=err)
    return completed.returncode, out, err


def list_connected_devices(adb_cmd: str = "adb") -> List[str]:
    """Return list of device serials reported by `adb devices` (excluding header and offline entries)."""
    rc, out, err = run_cmd([adb_cmd, "devices"], check=True)
    devices: List[str] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def check_device_state(serial: str, adb_cmd: str = "adb") -> bool:
    try:
        rc, out, err = run_cmd([adb_cmd, "-s", serial, "get-state"], check=True)
        return (out or "").strip() == "device"
    except subprocess.CalledProcessError:
        return False


def adb_push_and_install(serial: str, local_apk: str, adb_cmd: str = "adb", reinstall: bool = True) -> bool:
    """Push a local APK to device and install it. Returns True on success."""
    if not os.path.exists(local_apk):
        logging.error("APK not found: %s", local_apk)
        return False
    basename = os.path.basename(local_apk)
    remote_tmp = f"/data/local/tmp/{basename}"
    try:
        logging.info("Pushing %s -> %s on %s", local_apk, remote_tmp, serial)
        run_cmd([adb_cmd, "-s", serial, "push", local_apk, remote_tmp], check=True)
        install_args = [adb_cmd, "-s", serial, "shell", "pm", "install"]
        if reinstall:
            install_args.append("-r")
        install_args.append(remote_tmp)
        logging.info("Installing on device %s: %s", serial, remote_tmp)
        run_cmd(install_args, check=True)
        # remove remote tmp
        run_cmd([adb_cmd, "-s", serial, "shell", "rm", "-f", remote_tmp], check=False)
        return True
    except subprocess.CalledProcessError as e:
        logging.error("Failed to push/install %s on %s: rc=%s stdout=%s stderr=%s", local_apk, serial, getattr(e, 'returncode', None), getattr(e, 'output', None), getattr(e, 'stderr', None))
        return False


def is_package_installed(serial: str, package_name: str, adb_cmd: str = "adb") -> bool:
    try:
        rc, out, err = run_cmd([adb_cmd, "-s", serial, "shell", "pm", "list", "packages", package_name], check=True)
        return package_name in (out or "")
    except subprocess.CalledProcessError:
        return False


def check_driver_installed(driver_name: str = "uiautomator2", appium_bin: Optional[str] = None) -> bool:
    """Check if a given Appium driver is installed on the host by asking `appium driver list` and checking output."""
    if appium_bin is None:
        appium_bin = shutil.which("appium")
    if not appium_bin:
        logging.warning("Cannot verify driver installation: 'appium' CLI not found in PATH.")
        return False
    try:
        rc, out, err = run_cmd([appium_bin, "driver", "list"], check=False)
        text = (out or "") + "\n" + (err or "")
        if driver_name in text:
            logging.info("Appium driver '%s' found in driver list.", driver_name)
            return True
        # try a more relaxed check: some appium versions print JSON or tables; search words
        if any(driver_name in line for line in text.splitlines()):
            logging.info("Appium driver '%s' appears in driver list output.", driver_name)
            return True
        logging.warning("Appium driver '%s' not found in appium driver list. Output:\n%s", driver_name, text.strip())
        return False
    except Exception as e:
        logging.error("Failed to list appium drivers: %s", e)
        return False


def install_host_driver(driver_name: str = "uiautomator2") -> bool:
    """Install appium driver on host via `appium driver install <driver_name>` and verify installation."""
    appium_bin = shutil.which("appium")
    if not appium_bin:
        logging.error("`appium` CLI not found in PATH. Install Appium (npm i -g appium) to use host driver install.")
        return False
    try:
        logging.info("Installing Appium driver '%s' on host using: %s driver install %s", driver_name, appium_bin, driver_name)
        rc, out, err = run_cmd([appium_bin, "driver", "install", driver_name], check=True)
        logging.info("Driver install output:\n%s", out or "")
    except subprocess.CalledProcessError as e:
        logging.error("Appium driver install failed: %s", getattr(e, 'stderr', e))
        return False

    # verify
    installed = check_driver_installed(driver_name, appium_bin=appium_bin)
    if installed:
        logging.info("Verified driver '%s' installed successfully.", driver_name)
    else:
        logging.error("Driver '%s' does not appear in appium driver list after installation.", driver_name)
    return installed


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Install Appium uiautomator2 driver and optionally push uiautomator2 apks to devices")
    group = p.add_mutually_exclusive_group(required=False)
    group.add_argument("--all", action="store_true", help="Operate on all connected devices")
    group.add_argument("--device", "-d", type=str, help="Target device serial")
    p.add_argument("--driver-install", action="store_true", help="Install driver on host using `appium driver install uiautomator2`")
    p.add_argument("--apk-server", type=str, help="Path to uiautomator2 server APK to install on device(s)")
    p.add_argument("--apk-test", type=str, help="Path to uiautomator2 test APK to install on device(s)")
    p.add_argument("--adb", type=str, default="adb", help="ADB command (default: adb)")
    p.add_argument("--reinstall", action="store_true", help="Force reinstall of APKs (adb install -r)")
    p.add_argument("--yes", action="store_true", help="Auto-confirm prompts")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose debug logging")
    return p.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=LOG_FORMAT)

    if not args.all and not args.device and not args.driver_install:
        logging.error("Nothing to do: specify --driver-install or --device/--all")
        sys.exit(2)

    driver_ok = None
    if args.driver_install:
        driver_ok = install_host_driver("uiautomator2")
        if not driver_ok:
            logging.error("Host driver installation failed or verification failed")
            if not args.yes:
                logging.info("Continuing only if user forces with --yes")
                sys.exit(1)

    device_list: List[str] = []
    adb_cmd = args.adb

    # If user explicitly requested device(s) use them. Otherwise, if --driver-install was used without
    # --device/--all, probe connected devices to report per-device uiautomator2 presence (no installs).
    if args.device:
        device_list = [args.device]
    elif args.all:
        try:
            device_list = list_connected_devices(adb_cmd=adb_cmd)
        except subprocess.CalledProcessError as e:
            logging.error("Failed to list devices: %s", e)
            sys.exit(1)
    elif args.driver_install:
        # probe devices to include status in the summary
        try:
            logging.info("Probing connected devices to report uiautomator2 package status...")
            device_list = list_connected_devices(adb_cmd=adb_cmd)
        except Exception:
            device_list = []

    summary = {"target_devices": len(device_list), "installed": [], "skipped": [], "failed": [], "driver_installed": driver_ok, "device_status": {}}

    if device_list:
        logging.info("Devices found: %s", device_list)
    else:
        logging.info("No connected devices detected.")

    for dev in device_list:
        logging.info("Processing device: %s", dev)
        if not check_device_state(dev, adb_cmd=adb_cmd):
            logging.warning("Device %s is not in 'device' state, skipping", dev)
            summary['skipped'].append(dev)
            continue
        success = True
        # check if uiautomator2 packages already installed (before)
        server_pkg = "io.appium.uiautomator2.server"
        test_pkg = "io.appium.uiautomator2.server.test"
        server_before = is_package_installed(dev, server_pkg, adb_cmd=adb_cmd)
        test_before = is_package_installed(dev, test_pkg, adb_cmd=adb_cmd)
        summary['device_status'][dev] = {"server_before": server_before, "test_before": test_before, "install_attempts": []}

        logging.info("Device %s status before: server=%s, test=%s", dev, server_before, test_before)

        if args.apk_server:
            ok = adb_push_and_install(dev, args.apk_server, adb_cmd=adb_cmd, reinstall=args.reinstall)
            summary['device_status'][dev]['install_attempts'].append({"apk": args.apk_server, "ok": ok})
            if ok:
                summary['installed'].append(f"{dev}:{args.apk_server}")
            else:
                summary['failed'].append(f"{dev}:{args.apk_server}")
                success = False
        else:
            if server_before:
                logging.info("Server package %s already present on %s", server_pkg, dev)
            else:
                logging.info("Server package %s not present on %s (no apk provided to install)", server_pkg, dev)

        if args.apk_test:
            ok = adb_push_and_install(dev, args.apk_test, adb_cmd=adb_cmd, reinstall=args.reinstall)
            summary['device_status'][dev]['install_attempts'].append({"apk": args.apk_test, "ok": ok})
            if ok:
                summary['installed'].append(f"{dev}:{args.apk_test}")
            else:
                summary['failed'].append(f"{dev}:{args.apk_test}")
                success = False
        else:
            if test_before:
                logging.info("Test package %s already present on %s", test_pkg, dev)
            else:
                logging.info("Test package %s not present on %s (no apk provided to install)", test_pkg, dev)

        # re-check installed state (after)
        server_after = is_package_installed(dev, server_pkg, adb_cmd=adb_cmd)
        test_after = is_package_installed(dev, test_pkg, adb_cmd=adb_cmd)
        summary['device_status'][dev].update({"server_after": server_after, "test_after": test_after})

        logging.info("Device %s status after: server=%s, test=%s", dev, server_after, test_after)

        if success:
            logging.info("Device %s: OK", dev)
        else:
            logging.warning("Device %s: had failures", dev)

    # pretty-print summary
    try:
        logging.info("Summary: %s", json.dumps(summary, indent=2))
    except Exception:
        logging.info("Summary (raw): %s", summary)


if __name__ == "__main__":
    main()

