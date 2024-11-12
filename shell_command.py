import os
import subprocess

def execute_shell_command(command, aosp_root_path):
    current_directory = os.path.dirname(os.path.realpath(__file__))
    os.chdir(aosp_root_path)
    result = subprocess.run(command, shell=True, capture_output=True, text=False)
    log_out = result.stdout.decode('utf-8', errors='ignore').strip()
    log_err = result.stderr.decode('utf-8', errors='ignore').strip()

    is_success = result.returncode == 0 or "error" not in log_err.lower()

    os.chdir(current_directory)
    log = f"is_success: {is_success} result.returncode: {result.returncode}, stdout: {log_out} | error: {log_err}"
    return is_success, log

def execute_command(command):
    """
    Execute a command and checks if it has an exit code of 0.

    :param command: list - the command and its arguments to execute.

    :return: tuple - (bool, str) - True if the command was successful, False otherwise.
    """
    is_success = False
    result = subprocess.run(command, capture_output=True, text=False)
    if result.returncode == 0:
        is_success = True
        log = result.stdout.decode('utf-8', errors='ignore').strip()
    else:
        log = result.stderr.decode('utf-8', errors='ignore').strip()

    return is_success, log