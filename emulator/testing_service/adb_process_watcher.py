import logging
import os
import subprocess
import time
import psutil

# Configuration
TARGET_CMD = "adb -L tcp:5037 fork-server server --reply-fd 7"
ADB_BIN = "/android/sdk/platform-tools/adb"
CHECK_INTERVAL = 120  # 2 minutes in seconds
LOG_FILE_PATH = "/android/log/adb_monitor.log"

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE_PATH),
        logging.StreamHandler(),  # Keeps console output active as well
    ],
)


def is_process_running(target_command):
    """Checks if a process with the exact target command line is running."""
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline_list = proc.info.get("cmdline")
            if cmdline_list:
                cmdline_str = " ".join(cmdline_list)
                if target_command in cmdline_str:
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False


def restart_adb_server():
    """Kills the current ADB server and starts a new one in the background."""
    logging.info("Target process detected! Restarting ADB server...")

    try:
        # 1. Kill the server
        subprocess.run([ADB_BIN, "kill-server"], check=True)

        # 2. Start the new server in the background
        cmd = [ADB_BIN, "-a", "-P", "5037", "nodaemon", "server"]

        if os.name == "nt":
            subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                cmd,
                preexec_fn=os.setsid,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        logging.info("ADB server restarted successfully.")

    except subprocess.CalledProcessError as e:
        logging.error(f"Error while killing ADB server: {e}")
    except Exception as e:
        logging.error(f"Failed to start new ADB server: {e}")


def main():
    logging.info(f"Monitoring started. Checking every {CHECK_INTERVAL} seconds.")
    while True:
        if is_process_running(TARGET_CMD):
            restart_adb_server()
        else:
            logging.debug(
                "Target process not running."
            )  # Changed to debug to avoid bloating log file

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()