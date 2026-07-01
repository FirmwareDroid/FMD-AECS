import logging
import os
import subprocess
import traceback

def execute_shell_command(command, aosp_root_path, lunch_target):
    env_copy = os.environ.copy()
    if "ANDROID_HOST_OUT" not in env_copy:
        final_command = f"source build/envsetup.sh && lunch {lunch_target} && {command}"
    else:
        final_command = command
    result = subprocess.run(
        final_command,
        shell=True,
        executable='/bin/bash',
        cwd=aosp_root_path,
        capture_output=True,
        env=env_copy
    )
    log_out = result.stdout.decode('utf-8', errors='ignore').strip()
    log_err = result.stderr.decode('utf-8', errors='ignore').strip()
    is_success = result.returncode == 0
    log = f"is_success: {is_success} result.returncode: {result.returncode}, stdout: {log_out} | error: {log_err}"
    return is_success, log

def execute_command(command, cwd=None, shell=False, env=None):
    """
    Execute a command and checks if it has an exit code of 0.

    :param command: list - the command and its arguments to execute.

    :return: tuple - (bool, str) - True if the command was successful, False otherwise.
    """
    if not cwd:
        cwd = os.getcwd()
    is_success = False
    try:
        if env:
            result = subprocess.run(command, capture_output=True, text=False, cwd=cwd, shell=shell, env=env)
        else:
            env_copy = os.environ.copy()
            result = subprocess.run(command, capture_output=True, text=False, cwd=cwd, shell=shell, env=env_copy)
        logging.debug(f"Executed command: {command} - {result.returncode}")
        if result.returncode == 0:
            is_success = True
            log = result.stdout.decode('utf-8', errors='ignore').strip()
        else:
            is_success  = False
            log = (
                f"Error executing command: {command}\n"
                f"Working directory: {cwd}\n"
                f"Return code: {result.returncode}\n"
                f"Stdout: {result.stdout.decode('utf-8', errors='ignore').strip()}\n"
                f"Stderr: {result.stderr.decode('utf-8', errors='ignore').strip()}"
            )
            log = f"Return code: {result.returncode} with message: {result.stderr.decode('utf-8', errors='ignore').strip()}"
    except Exception as e:
        log = (
            f"Exception while executing command: {command}\n"
            f"Working directory: {cwd}\n"
            f"Error: {e}\n"
            f"Stack trace:\n{traceback.format_exc()}"
        )

    return is_success, log