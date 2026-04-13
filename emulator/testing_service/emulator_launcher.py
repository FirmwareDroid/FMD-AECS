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


def run_cmd(cmd):
    return subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def find_qemu_child(wrapper_pid, avd_name, timeout=10.0, ignore_pids=None):
    """Try to find a qemu-system child process for the given wrapper pid.
    Return PID int if found, otherwise None.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        # check children of wrapper
        try:
            out = subprocess.check_output(["pgrep", "-P", str(wrapper_pid)], text=True)
            for line in out.strip().splitlines():
                if not line:
                    continue
                pid = line.strip()
                # skip pids we already marked as zombies
                if ignore_pids and int(pid) in ignore_pids:
                    continue
                try:
                    comm = subprocess.check_output(["ps", "-p", pid, "-o", "comm="], text=True).strip()
                except subprocess.CalledProcessError:
                    continue
                if "qemu" in comm or "qemu-system" in comm:
                    # ensure it's not a zombie process
                    if is_process_zombie(int(pid)):
                        if ignore_pids is not None:
                            ignore_pids.add(int(pid))
                        continue
                    return int(pid)
        except subprocess.CalledProcessError:
            pass
        # fallback: global qemu-system processes that mention the AVD name
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
                    # ensure it's not a zombie
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
    """Return True if the process exists but is a zombie (defunct).
    Uses ps to query the process STAT field and looks for 'Z'.
    """
    try:
        out = subprocess.check_output(["ps", "-p", str(pid), "-o", "stat="], text=True)
        stat = out.strip()
        # STAT may contain multiple characters like 'Z+' or 'S', so check for 'Z'
        return 'Z' in stat
    except subprocess.CalledProcessError:
        # ps failed (process not found)
        return False
    except Exception:
        return False


def tail_contains(path, needle):
    try:
        out = subprocess.check_output(["tail", "-n", "200", path], text=True, stderr=subprocess.DEVNULL)
        return needle.lower() in out.lower()
    except Exception:
        return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--script", required=True, help="Path to emulator_start.sh wrapper")
    p.add_argument("--log", required=True, help="Path to log file to monitor")
    p.add_argument("--avd", default=None, help="AVD name to help find qemu process (optional)")
    p.add_argument("--cooldown", type=int, default=int(os.environ.get("RESTART_COOLDOWN", "30")))
    p.add_argument("--max-restarts", type=int, default=int(os.environ.get("MAX_RESTARTS", "20")))
    args = p.parse_args()

    launcher_log = args.log
    script = args.script
    avd_name = args.avd or ""
    cooldown = args.cooldown
    max_restarts = args.max_restarts

    restart_count = 0
    # Track PIDs that we've seen as zombies/defunct so we don't treat them as
    # valid newly-started qemu processes when searching.
    zombie_pids = set()

    # Write launcher-specific logfile into the same directory as this launcher
    # script. This creates: <launcher_dir>/emulator_launcher_py.log
    script_dir = os.path.dirname(os.path.abspath(__file__))
    launch_logfile = os.path.join(script_dir, "out/emulator_launcher.log")
    os.makedirs(script_dir, exist_ok=True)
    logger = logging.getLogger("emulator_launcher")
    logger.setLevel(logging.INFO)
    # avoid duplicate handlers on repeated calls
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

    # use logger.info / logger.error / logger.exception directly

    # handle termination signals by exiting (child processes will be left to OS)
    def on_term(sig, frame):
        logger.info(f"Received signal {sig}; exiting launcher.")
        sys.exit(0)

    signal.signal(signal.SIGINT, on_term)
    signal.signal(signal.SIGTERM, on_term)

    # Reap child processes to avoid creating zombies when this process runs as PID 1.
    # On Linux, when running as PID 1 in a container the parent must wait() for
    # exited children; install a SIGCHLD handler that performs non-blocking wait.
    def _reap_children(signum, frame):
        try:
            while True:
                # -1 means wait for any child, WNOHANG makes it non-blocking
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
                logger.debug(f"Reaped child pid={pid} status={status}")
        except ChildProcessError:
            # No child processes
            pass
        except Exception as e:
            # Log and continue
            logger.exception(f"Exception while reaping children: {e}")

    # Register handler for SIGCHLD if available on the platform
    try:
        signal.signal(signal.SIGCHLD, _reap_children)
    except AttributeError:
        # Windows or platforms without SIGCHLD
        pass

    while True:
        if restart_count >= max_restarts:
            logger.error(f"Maximum restart limit reached ({max_restarts}); giving up.")
            sys.exit(3)

        logger.info(f"Starting wrapper script: {script} (attempt {restart_count+1}/{max_restarts})")
        # Start wrapper; let it manage its own logging. We'll not capture its output here.
        proc = subprocess.Popen(["/bin/bash", script])
        wrapper_pid = proc.pid
        logger.info(f"Wrapper started with pid {wrapper_pid}")

        # attempt to locate qemu child
        qemu_pid = find_qemu_child(wrapper_pid, avd_name, timeout=10.0, ignore_pids=zombie_pids)
        if qemu_pid:
            logger.info(f"Found qemu subprocess pid {qemu_pid}; monitoring until it exits")
            # monitor qemu; also watch log for fatal KVM error or zombie (defunct) state
            while True:
                # If the pid no longer exists, treat as exited
                if not pid_is_running(qemu_pid):
                    logger.info(f"qemu subprocess {qemu_pid} no longer running")
                    break
                # If the process is a zombie/defunct, treat as crashed
                if is_process_zombie(qemu_pid):
                    logger.warning(f"qemu subprocess {qemu_pid} is in zombie (defunct) state; treating as exited")
                    zombie_pids.add(qemu_pid)
                    # attempt to terminate wrapper as well
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break
                # check for KVM error or segfault hints in launcher_log
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
                time.sleep(1)
            logger.info(f"qemu subprocess {qemu_pid} has exited or was terminated")
            # give wrapper a moment and capture its exit code if it exits
            try:
                ret = proc.wait(timeout=5)
                logger.info(f"Wrapper process exited with return code {ret}")
            except subprocess.TimeoutExpired:
                logger.info("Wrapper did not exit promptly after qemu exit; continuing")
        else:
            # no qemu found within timeout, wait for wrapper to exit and report
            logger.info("Could not locate qemu subprocess; waiting for wrapper to exit")
            ret = proc.wait()
            logger.info(f"Wrapper process exited with return code {ret}")
            # If wrapper aborted/segfaulted, print and decide restart
            if ret != 0:
                logger.warning(f"Wrapper exited with non-zero code {ret}")

        # increment restart counter and decide whether to restart
        restart_count += 1
        if restart_count >= max_restarts:
            logger.error(f"Reached max restarts ({max_restarts}); exiting with code 3")
            sys.exit(3)

        logger.info(f"Sleeping {cooldown}s before restart (restart_count={restart_count})")
        time.sleep(cooldown)
        logger.info("Restarting wrapper now")
        # loop continues


if __name__ == "__main__":
    main()

