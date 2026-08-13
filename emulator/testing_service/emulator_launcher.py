#!/usr/bin/env python3
"""
emulator_launcher.py
Start and monitor `emulator_start.sh` wrapper. If the Android emulator (qemu)
crashes or a fatal error appears, restart `emulator_start.sh` after a cooldown.

The launcher prints exit codes and diagnostics for callers (useful for supervising
containers). Configurable via environment variables or CLI args.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
import logging
import shutil


def run_cmd(cmd):
    return subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def find_qemu_child(wrapper_pid, avd_name, timeout=10.0, ignore_pids=None):
    """Try to find a qemu-system child process for the given wrapper pid.
    Return PID int if found, otherwise None.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = subprocess.check_output(["pgrep", "-P", str(wrapper_pid)], text=True)
            for line in out.strip().splitlines():
                if not line:
                    continue
                pid = line.strip()
                if ignore_pids and int(pid) in ignore_pids:
                    continue
                try:
                    comm = subprocess.check_output(["ps", "-p", pid, "-o", "comm="], text=True).strip()
                except subprocess.CalledProcessError:
                    continue
                if "qemu" in comm or "qemu-system" in comm:
                    if is_process_zombie(int(pid)):
                        if ignore_pids is not None:
                            ignore_pids.add(int(pid))
                        continue
                    return int(pid)
        except subprocess.CalledProcessError:
            pass
        try:
            out = subprocess.check_output(["pgrep", "-f", "qemu-system"], text=True)
            for line in out.strip().splitlines():
                if not line:
                    continue
                pid = line.strip()
                if ignore_pids and int(pid) in ignore_pids:
                    continue
                try:
                    args = subprocess.check_output(["ps", "-p", pid, "-o", "args="], text=True).strip()
                except subprocess.CalledProcessError:
                    continue
                if avd_name in args or "qemu-system-" in args:
                    if is_process_zombie(int(pid)):
                        if ignore_pids is not None:
                            ignore_pids.add(int(pid))
                        continue
                    return int(pid)
        except subprocess.CalledProcessError:
            pass
        time.sleep(0.5)
    return None


def pid_is_running(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def is_process_zombie(pid):
    """Return True if the process exists but is a zombie (defunct)."""
    try:
        out = subprocess.check_output(["ps", "-p", str(pid), "-o", "stat="], text=True)
        stat = out.strip()
        return 'Z' in stat
    except subprocess.CalledProcessError:
        return False
    except Exception:
        return False


def tail_contains(path, needle):
    try:
        out = subprocess.check_output(["tail", "-n", "200", path], text=True, stderr=subprocess.DEVNULL)
        return needle.lower() in out.lower()
    except Exception:
        return False


def _check_device_responsive(timeout=5):
    """Return True if an adb-connected device/emulator responds to basic checks."""
    logger = logging.getLogger("emulator_launcher")
    try:
        adb = shutil.which('adb') or 'adb'
        logger.debug(f"_check_device_responsive: using adb binary '{adb}' with timeout={timeout}s")

        p = subprocess.run([adb, 'devices'], capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or '')
        logger.debug(f"adb devices output:\n{out}")
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        serial = None
        for l in lines:
            if l.startswith('List of devices'):
                continue
            parts = l.split()
            if len(parts) >= 2 and parts[1] == 'device':
                serial = parts[0]
                break
        if not serial:
            logger.debug('_check_device_responsive: no device in adb devices list')
            return False
        logger.debug(f"_check_device_responsive: selected device serial={serial}")

        try:
            start = time.time()
            r = subprocess.run([adb, '-s', serial, 'shell', 'dumpsys', 'activity', 'activities'], capture_output=True,
                               text=True, timeout=timeout)
            duration = time.time() - start
            snippet = (r.stdout or r.stderr or '')[:2000]
            logger.debug(
                f"dumpsys activity returned (rc={r.returncode}) in {duration:.2f}s; snippet_len={len(snippet)}")
            if r.returncode == 0 and snippet.strip():
                if 'ResumedActivity' in snippet or 'mResumedActivity' in snippet or 'Running activities' in snippet or 'ACTIVITY' in snippet:
                    logger.debug('_check_device_responsive: dumpsys activity indicates responsive')
                    return True
                logger.debug('_check_device_responsive: dumpsys activity non-empty => responsive')
                return True
        except Exception as e:
            logger.debug(f"dumpsys activity check failed: {e}")

        try:
            start = time.time()
            r_pid = subprocess.run([adb, '-s', serial, 'shell', 'pidof', 'system_server'], capture_output=True,
                                   text=True, timeout=timeout)
            duration = time.time() - start
            pidout = (r_pid.stdout or r_pid.stderr or '').strip()
            logger.debug(f"pidof system_server returned (rc={r_pid.returncode}) in {duration:.2f}s; out='{pidout}'")
            if r_pid.returncode == 0 and pidout:
                logger.debug('_check_device_responsive: system_server pid present => responsive')
                return True
        except Exception as e:
            logger.debug(f"pidof system_server check failed: {e}")

        try:
            start = time.time()
            r2 = subprocess.run([adb, '-s', serial, 'shell', 'getprop', 'sys.boot_completed'], capture_output=True,
                                text=True, timeout=timeout)
            duration = time.time() - start
            v = (r2.stdout or r2.stderr or '').strip()
            logger.debug(f"getprop sys.boot_completed returned (rc={r2.returncode}) in {duration:.2f}s; val='{v}'")
            if r2.returncode == 0 and v == '1':
                logger.debug('_check_device_responsive: boot_completed == 1 => responsive')
                return True
            if r2.returncode == 0 and v:
                logger.debug('_check_device_responsive: getprop returned non-empty => responsive')
                return True
        except Exception as e:
            logger.debug(f"getprop check failed: {e}")

    except Exception as e:
        logger.warning(f"_check_device_responsive overall failure: {e}")
        return False
    logger.warning('_check_device_responsive: no positive signals from adb checks; treating device as unresponsive')
    return False


def is_adb_available(timeout=5):
    """Return True if the adb executable responds (quick 'adb version' check)."""
    try:
        res = subprocess.run(["adb", "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        return res.returncode == 0
    except Exception:
        return False


def wait_for_adb_available(timeout=30, interval=2):
    logger = logging.getLogger("emulator_launcher")
    """Wait up to timeout seconds for adb to be usable. Returns True if available."""
    start = time.time()
    while True:
        if is_adb_available():
            logger.debug("adb available")
            return True
        if time.time() - start >= timeout:
            logger.warning("adb did not become available within %s seconds", timeout)
            return False
        time.sleep(interval)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--script", required=True, help="Path to emulator_start.sh wrapper")
    p.add_argument("--log", required=True, help="Path to log file to monitor")
    p.add_argument("--avd", default=None, help="AVD name to help find qemu process (optional)")
    p.add_argument("--cooldown", type=int, default=int(os.environ.get("RESTART_COOLDOWN", "30")))
    p.add_argument("--max-restarts", type=int, default=int(os.environ.get("MAX_RESTARTS", "20")))
    p.add_argument("--unresponsive-minutes", type=int, default=int(os.environ.get('UNRESPONSIVE_MINUTES', '5')),
                   help="Minutes of adb unresponsiveness before treating emulator as hung")
    args = p.parse_args()

    launcher_log = args.log
    script = args.script
    avd_name = args.avd or ""
    cooldown = args.cooldown
    max_restarts = args.max_restarts
    unresponsive_minutes = args.unresponsive_minutes or 0
    unresponsive_threshold = int(unresponsive_minutes) * 60

    restart_count = 0
    zombie_pids = set()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    launch_logfile = os.path.join(script_dir, "out/emulator_launcher.log")
    os.makedirs(os.path.dirname(launch_logfile), exist_ok=True)

    logger = logging.getLogger("emulator_launcher")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(launch_logfile, mode="a", encoding="utf-8")
        fh.setLevel(logging.INFO)
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s [launcher] %(levelname)s %(message)s")
        fh.setFormatter(fmt)
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)

    ### Launch adb_process_watcher.py ###
    watcher_path = os.path.join(script_dir, "adb_process_watcher.py")
    if os.path.exists(watcher_path):
        logger.info(f"Starting background ADB monitor daemon: {watcher_path}")
        try:
            process = subprocess.Popen(
                [sys.executable, watcher_path],
                preexec_fn=os.setsid,  # Detach process group so it lives independently
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            # Give the process a brief moment to initialize or fail
            time.sleep(0.5)
            return_code = process.poll()
            if return_code is not None:
                logger.error(f"ADB monitor daemon exited immediately with code {return_code}")
            logger.info(f"Background ADB monitor daemon started successfully (PID: {process.pid})")
        except Exception as e:
            logger.exception(f"Failed to start background adb_process_watcher.py: {e}")
    else:
        logger.warning(f"Expected adb_process_watcher.py at {watcher_path}, but file was not found.")

    def on_term(sig, frame):
        logger.info(f"Received signal {sig}; exiting launcher.")
        sys.exit(0)

    signal.signal(signal.SIGINT, on_term)
    signal.signal(signal.SIGTERM, on_term)

    def _reap_children(signum, frame):
        try:
            while True:
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
                logger.debug(f"Reaped child pid={pid} status={status}")
        except ChildProcessError:
            pass
        except Exception as e:
            logger.exception(f"Exception while reaping children: {e}")

    try:
        signal.signal(signal.SIGCHLD, _reap_children)
    except AttributeError:
        pass

    while True:
        if restart_count >= max_restarts:
            logger.error(f"Maximum restart limit reached ({max_restarts}); giving up.")
            sys.exit(3)

        logger.info(f"Starting wrapper script: {script} (attempt {restart_count + 1}/{max_restarts})")
        proc = subprocess.Popen(["/bin/bash", script])
        wrapper_pid = proc.pid
        logger.info(f"Wrapper started with pid {wrapper_pid}")

        qemu_pid = find_qemu_child(wrapper_pid, avd_name, timeout=10.0, ignore_pids=zombie_pids)
        if qemu_pid:
            logger.info(f"Found qemu subprocess pid {qemu_pid}; monitoring until it exits")
            last_responsive_time = time.time()
            last_responsive_check = 0
            responsive_check_interval = 60.0
            while True:
                if not pid_is_running(qemu_pid):
                    logger.info(f"qemu subprocess {qemu_pid} no longer running")
                    break
                if is_process_zombie(qemu_pid):
                    logger.warning(f"qemu subprocess {qemu_pid} is in zombie (defunct) state; treating as exited")
                    zombie_pids.add(qemu_pid)
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break
                if tail_contains(launcher_log, "kvm run failed"):
                    logger.warning("Detected 'kvm run failed' in emulator log; terminating and will restart")
                    try:
                        os.kill(qemu_pid, signal.SIGTERM)
                    except Exception:
                        pass
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break
                if tail_contains(launcher_log, "segmentation fault") or tail_contains(launcher_log, "segfault"):
                    if wait_for_adb_available(timeout=30, interval=2):
                        continue
                    logger.warning("Detected segmentation fault in emulator log; terminating and will restart")
                    try:
                        os.kill(qemu_pid, signal.SIGTERM)
                    except Exception:
                        pass
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break
                try:
                    now = time.time()
                    if unresponsive_threshold > 0 and (now - last_responsive_check) >= responsive_check_interval:
                        last_responsive_check = now
                        try:
                            responsive = _check_device_responsive(timeout=10)
                        except Exception as e:
                            logger.warning(f"Device responsiveness check failed: {e}")
                            responsive = False
                        if responsive:
                            logger.debug("Device responsive via adb; updating last_responsive_time")
                            last_responsive_time = now
                        else:
                            inactive = now - last_responsive_time if last_responsive_time else now
                            if inactive >= unresponsive_threshold:
                                logger.warning(
                                    f"Device unresponsive for {inactive:.0f}s (threshold {unresponsive_threshold}s). Treating emulator as hung and restarting")
                                try:
                                    os.kill(qemu_pid, signal.SIGTERM)
                                except Exception:
                                    pass
                                try:
                                    proc.terminate()
                                except Exception:
                                    pass
                                break
                except Exception:
                    logger.debug('device responsiveness check failed', exc_info=True)
                time.sleep(1)
            logger.info(f"qemu subprocess {qemu_pid} has exited or was terminated")
            try:
                ret = proc.wait(timeout=5)
                logger.info(f"Wrapper process exited with return code {ret}")
            except subprocess.TimeoutExpired:
                logger.info("Wrapper did not exit promptly after qemu exit; continuing")
        else:
            logger.info("Could not locate qemu subprocess; waiting for wrapper to exit")
            ret = proc.wait()
            logger.info(f"Wrapper process exited with return code {ret}")
            if ret != 0:
                logger.warning(f"Wrapper exited with non-zero code {ret}")

        restart_count += 1
        if restart_count >= max_restarts:
            logger.error(f"Reached max restarts ({max_restarts}); exiting with code 3")
            sys.exit(3)

        logger.info(f"Sleeping {cooldown}s before restart (restart_count={restart_count})")
        time.sleep(cooldown)
        logger.info("Restarting wrapper now")


if __name__ == "__main__":
    main()