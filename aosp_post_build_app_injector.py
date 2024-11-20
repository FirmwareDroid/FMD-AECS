import logging
import os
import shutil
from shell_command import execute_command
from config_post_injector import *


def handle_apk_signing(file_path, aosp_path):
    error_message = None
    output = None
    is_success = False
    signing_key = get_signing_key_from_module(file_path)

    if not signing_key:
        error_message = f"Signing key name not found for {file_path}"
    signing_key_path = get_signing_key_path(aosp_path, signing_key)

    if not os.path.exists(signing_key_path):
        error_message = f"Signing key not found at {signing_key_path}"

    if not error_message:
        is_success, log_message = sign_apk_file(file_path, signing_key_path)
        if not is_success:
            error_message = f"Error signing APK file: {signing_key}|{signing_key_path}|{error_message}"
        else:
            logging.info(f"APK file signed: {file_path} with key: {signing_key}")

    return is_success, output, (error_message, file_path, signing_key, signing_key_path)

def get_signing_key_from_module(android_apk_file_path):
    file_name = os.path.basename(android_apk_file_path)
    module_name = file_name.split(".")[0]
    android_mk_file_path = os.path.join(EXTRACTED_PACKAGES_PATH, module_name, "Android.mk")
    logging.info(f"Android.mk file path: {android_mk_file_path}")
    if os.path.exists(android_mk_file_path):
        with open(android_mk_file_path, "r") as file:
            for line in file:
                if "LOCAL_CERTIFICATE" in line:
                    signing_key = line.split("=")[1].strip()
                    return signing_key.lower()
    else:
        logging.warning(f"Android.mk Module not found: {module_name} path {android_mk_file_path}."
                        f"File {android_apk_file_path} - using platform key.")
        return "platform"


def get_signing_key_path(aosp_path, signing_key_name):
    key_file_path = f"{aosp_path}build/target/product/security/{signing_key_name}.p12"
    key_file_path = key_file_path.replace("//", "/")
    return key_file_path

def align_apk_file(apk_file_path):
    logging.info(f"Align apk file: {apk_file_path}")
    out_file_path = f"{apk_file_path}.aligned"
    command = ['zipalign', '-P', '16', '-v', '4', apk_file_path, out_file_path]
    success, log_message = execute_command(command)
    if success:
        shutil.move(out_file_path, apk_file_path)
    return success, log_message


def sign_apk_file(apk_file_path, signing_key_path):
    """
    Signs the APK file with apksigner.

    :param apk_file_path: str - path to the APK file.
    :param signing_key_path: str - path to the signing key.

    """
    logging.info(f"Signing APK file: {apk_file_path}")
    sign_command = ['apksigner', 'sign',
                    '--ks', signing_key_path,
                    '--v2-signing-enabled', 'true',
                    '--v3-signing-enabled', 'true',
                    '--v4-signing-enabled', 'true',
                    '--ks-pass', 'pass:',
                    '--in', apk_file_path,
                    '--out', apk_file_path]
    success, log_message = execute_command(sign_command)
    return success, log_message

def verify_apk_file(apk_file_path):
    logging.info(f"Verifying APK file: {apk_file_path}")
    verify_command = ['apksigner', 'verify', apk_file_path]
    success, log_message = execute_command(verify_command)
    return success, log_message