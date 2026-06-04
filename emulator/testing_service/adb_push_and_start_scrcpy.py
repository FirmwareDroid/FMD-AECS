#!/usr/bin/env python3
"""
Simple watcher that ensures a scrcpy server JAR is pushed to any connected
ADB device and attempts to start it as a background process.

Behavior:
 - Polls `adb devices` repeatedly.
 - For each device in "device" state, verifies whether the remote file exists; if not,
   pushes the local JAR to the remote path. We intentionally only check existence
   (not file size) to avoid overwriting preinstalled or externally-managed binaries.
 - Attempts to start the server on the device using `app_process64` or
   `app_process` with CLASSPATH set to the pushed JAR. Uses a best-effort `nohup`
   style command to background the server so it survives the shell session.

Defaults (configurable via CLI):
 - Local server JAR: ./emulator/prebuilts/adb/scrcpy-server.jar
 - Remote path: /data/local/tmp/scrcpy-server.jar
 - Poll interval: 10s

Note: This script assumes `adb` is in PATH and the running user has access to
an adb server that can reach devices.
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from typing import List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("adb-scrcpy-watcher")

DEFAULT_REMOTE_PATH = "/data/local/tmp/scrcpy-server.jar"
DEFAULT_POLL_INTERVAL = 120.0

ADB_BINARY = shutil.which("adb") or "adb"


class AdbError(RuntimeError):
    pass


def ensure_adbd_root(serial: str, timeout: float = 8.0) -> bool:
    """
    Attempt to run `adb -s <serial> root` to restart adbd as root.

    Returns True if the command appears to have succeeded (returncode == 0) or
    if the command returned an output that indicates success. Returns False when
    the attempt failed. This is non-fatal — many production devices will refuse.
    """
    try:
        cp = run_adb(["-s", serial, "root"], timeout=timeout)
        # Successful restart typically gives rc==0 and may print "restarting adbd as root"
        if cp.returncode == 0:
            out = (cp.stdout or "") + (cp.stderr or "")
            logger.info("adb root succeeded (device=%s): %s", serial, out.strip())
            # adbd typically restarts; give it a short moment to come back
            time.sleep(5.0)
            return True
        else:
            logger.debug("adb root returned rc=%s for device %s: %s", cp.returncode, serial, (cp.stderr or '').strip())
            return False
    except AdbError as e:
        logger.debug("adb root failed for device %s: %s", serial, e)
        return False

def run_adb(args: List[str], timeout: Optional[float] = 15.0) -> subprocess.CompletedProcess:
    cmd = [ADB_BINARY] + args
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return cp
    except FileNotFoundError:
        raise AdbError("adb binary not found in PATH")
    except subprocess.TimeoutExpired as e:
        raise AdbError(f"adb command timeout: {' '.join(cmd)}")


def list_devices() -> List[str]:
    """Return list of device serials in 'device' state."""
    cp = run_adb(["devices", "-l"])  # list with details
    if cp.returncode != 0:
        logger.debug("adb devices failed: %s %s", cp.returncode, cp.stderr.strip())
        return []
    lines = [l.strip() for l in cp.stdout.splitlines()]
    serials = []
    for line in lines:
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial = parts[0]
        state = parts[1]
        if state == "device":
            serials.append(serial)
    return serials


def remote_file_info(serial: str, remote_path: str):
    """Return (exists: bool/None, size: None, path: str/None).
    exists=None indicates probe was inconclusive (e.g. subprocess not supported).

    Note: size is intentionally not provided — callers should only rely on
    the existence boolean.
    """
    try:
        cp = run_adb(["-s", serial, "shell", "ls", "-l", remote_path])
        if cp.returncode != 0:
            # grep stderr or stdout for No such file
            out = (cp.stdout or "") + (cp.stderr or "")
            if "No such file" in out or "No such file or directory" in out:
                return False, None, None
            # Some devices print to stdout a message; treat as not found
            return False, None, None
        out = cp.stdout.strip()
        if not out:
            return None, None, None
        # We only care about existence. If ls -l produced output, assume file exists.
        return True, None, remote_path
    except AdbError as e:
        logger.debug("remote_file_info adb error for %s: %s", serial, e)
        return None, None, None


def push_jar(serial: str, local_path: str, remote_path: str) -> bool:
    """Push local_path to remote_path on device serial. Returns True on success."""
    if not os.path.exists(local_path):
        logger.error("Local server JAR not found: %s", local_path)
        return False
    exists, size, _ = remote_file_info(serial, remote_path)
    # If the server process is already running on the device, avoid overwriting
    # the binary. This prevents replacing a running server even if sizes differ.
    try:
        if is_server_running(serial):
            logger.info("scrcpy server already running on %s; skipping push to %s", serial, remote_path)
            return True
    except Exception:
        # If server detection fails for this device, fall back to normal behavior
        logger.debug("is_server_running check failed for %s; will continue with push verification", serial)

    if exists:
        logger.info("Remote jar already present on %s: %s (existence-only check)", serial, remote_path)
        # Ensure the remote jar is executable so it can be launched via app_process
        try:
            cp_chmod = run_adb(["-s", serial, "shell", "chmod", "+x", remote_path], timeout=8)
            if cp_chmod.returncode == 0:
                logger.info("Set executable permissions on %s for device %s", remote_path, serial)
            else:
                logger.warning("Failed to set executable permissions on %s for device %s: rc=%s stderr=%s",
                               remote_path, serial, cp_chmod.returncode, (cp_chmod.stderr or '').strip())
        except AdbError as e:
            logger.warning("chmod via adb failed for %s on device %s: %s", remote_path, serial, e)
        return True

    logger.info("Pushing %s -> %s (device=%s)", local_path, remote_path, serial)
    cp = run_adb(["-s", serial, "push", local_path, remote_path], timeout=120)
    if cp.returncode != 0:
        logger.error("adb push failed for %s: rc=%s stderr=%s", serial, cp.returncode, cp.stderr.strip())
        return False
    # verify existence after push
    exists2, size2, _ = remote_file_info(serial, remote_path)
    if exists2:
        logger.info("Push verified (existence) for device %s", serial)
        # Ensure the remote jar is executable so it can be launched via app_process
        try:
            cp_chmod = run_adb(["-s", serial, "shell", "chmod", "+x", remote_path], timeout=8)
            if cp_chmod.returncode == 0:
                logger.info("Set executable permissions on %s for device %s", remote_path, serial)
            else:
                logger.warning("Failed to set executable permissions on %s for device %s: rc=%s stderr=%s",
                               remote_path, serial, cp_chmod.returncode, (cp_chmod.stderr or '').strip())
        except AdbError as e:
            logger.warning("chmod via adb failed for %s on device %s: %s", remote_path, serial, e)
        return True
    logger.warning("Push completed but verification failed for device %s (remote file not found)", serial)
    return False


def is_server_running(serial: str) -> bool:
    """Return True if scrcpy server process looks like it is running on device."""
    # Try a few variations since `ps` output varies across devices
    # First try full process listing with ps -A
    candidates = ["ps -A", "ps", "ps aux"]
    for cmd in candidates:
        try:
            cp = run_adb(["-s", serial, "shell", cmd, "|", "grep", "com.genymobile.scrcpy.Server"], timeout=5)
            # Because of shell pipe, adb returns 0 even if grep found nothing; however
            # some shells will return non-zero. Check stdout for evidence
            out = (cp.stdout or "") + (cp.stderr or "")
            if "com.genymobile.scrcpy.Server" in out or "scrcpy-server" in out:
                return True
        except AdbError:
            continue
    # Last-resort: grepping process list directly (without pipe) and check return code
    try:
        cp = run_adb(["-s", serial, "shell", "ps | grep scrcpy"], timeout=5)
        out = (cp.stdout or "") + (cp.stderr or "")
        if "scrcpy" in out or "com.genymobile.scrcpy.Server" in out:
            return True
    except AdbError:
        pass
    # Additional broad check: look for 'scrcpy' in a full ps aux listing. Some
    # devices or init systems place the process under a different name, but
    # presence of 'scrcpy' in ps output is a good heuristic that the server is
    # running.
    try:
        cp2 = run_adb(["-s", serial, "shell", "ps", "aux"], timeout=5)
        out2 = (cp2.stdout or "") + (cp2.stderr or "")
        if "scrcpy" in out2:
            return True
    except AdbError:
        pass
    return False


def start_server(serial: str, remote_path: str, use_app_process64: bool = True) -> bool:
    """Attempt to start the scrcpy server on the device in background.

    Attempts app_process64 (preferred) then app_process. Uses nohup-style
    backgrounding with redirection. Returns True if the start command was
    invoked successfully (best-effort) and the server seems to be running.
    """
    jar_basename = os.path.basename(remote_path)

    # Build candidate start commands. We attempt to use CLASSPATH and app_process
    # to run the server. The arguments for the Server class vary across versions;
    # we pass a minimal argument list (server version placeholder and 0).
    server_version = "1.0"  # placeholder value; server will still run in many versions
    candidates = []
    if use_app_process64:
        candidates.append(f"CLASSPATH={remote_path} app_process64 / com.genymobile.scrcpy.Server {server_version} 0")
    candidates.append(f"CLASSPATH={remote_path} app_process / com.genymobile.scrcpy.Server {server_version} 0")

    # Wrap with nohup-style backgrounding. Use sh -c to allow redirection.
    for cmd in candidates:
        sh_cmd = f"sh -c 'nohup {cmd} >/dev/null 2>&1 &'"
        logger.info("Attempting to start scrcpy server on %s with: %s", serial, cmd)
        try:
            cp = run_adb(["-s", serial, "shell", sh_cmd], timeout=8)
            # We consider the start invoked successfully if adb returned 0. Then
            # verify if process is visible shortly after.
            if cp.returncode == 0:
                # small sleep to allow process to start
                time.sleep(0.8)
                if is_server_running(serial):
                    logger.info("scrcpy server appears to be running on %s", serial)
                    return True
                else:
                    logger.debug("start_server: start command returned OK but server not found in ps for %s", serial)
            else:
                logger.warning("adb shell start command returned rc=%s stderr=%s", cp.returncode, cp.stderr.strip())
        except AdbError as e:
            logger.debug("start_server adb error for %s: %s", serial, e)
    logger.error("Failed to start scrcpy server on %s using known start commands", serial)
    return False


def ensure_server_on_device(serial: str, local_jar: str, remote_path: str):
    """Ensure the jar is pushed to the device. This function will NOT attempt
    to start the server; it only ensures the file exists on the remote device.
    """
    try:
        ok_push = push_jar(serial, local_jar, remote_path)
        if not ok_push:
            logger.warning("Push failed for device %s; will retry later", serial)
            return False
        # We purposely do not attempt to start or check the running server here.
        return True
    except Exception as e:
        logger.exception("ensure_server_on_device error for %s: %s", serial, e)
        return False


def main_loop(local_jar: str, remote_path: str, interval: float, once: bool = False):
    logger.info("Starting adb-scrcpy watcher: local=%s remote=%s interval=%.1fs", local_jar, remote_path, interval)
    if not shutil.which("adb"):
        logger.error("adb not found in PATH. Please install adb or add it to PATH.")
        sys.exit(2)

    while True:
        try:
            serials = list_devices()
            if not serials:
                logger.debug("No devices connected")
            for s in serials:
                try:
                    # Only ensure the JAR is present on the device; do not start the server
                    os.chmod(remote_path, 0o755)
                    ensure_adbd_root(s, timeout=30)
                    ensure_server_on_device(s, local_jar, remote_path)
                except Exception:
                    logger.exception("Device processing failed for %s", s)
            if once:
                break
        except Exception as e:
            logger.exception("Watcher loop unexpected error: %s", e)
        time.sleep(interval)


def parse_args():
    p = argparse.ArgumentParser(description="Push and start scrcpy-server.jar on connected adb devices (looping watcher)")
    p.add_argument("--local-jar", default="./emulator/prebuilts/adb/scrcpy-server.jar",
                   help="Path to local scrcpy-server.jar to push (default: %(default)s)")
    p.add_argument("--remote-path", default=DEFAULT_REMOTE_PATH, help="Remote destination path for the jar on device (default: %(default)s)")
    p.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL, help="Polling interval in seconds")
    p.add_argument("--once", action="store_true", help="Run one check cycle then exit (useful for scripting)")
    return p.parse_args()


if __name__ == '__main__':
    args = parse_args()
    # expand user and relative paths
    local_jar = os.path.expanduser(args.local_jar)
    if not os.path.isabs(local_jar):
        # make relative to repository root (script location)
        script_dir = os.path.dirname(os.path.realpath(__file__))
        repo_root = os.path.dirname(script_dir)
        local_jar = os.path.normpath(os.path.join(repo_root, local_jar))
    try:
        main_loop(local_jar, args.remote_path, args.interval, once=args.once)
    except KeyboardInterrupt:
        logger.info("Interrupted by user; exiting")
        sys.exit(0)


