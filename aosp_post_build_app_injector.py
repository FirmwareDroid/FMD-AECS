import logging
import os
import shutil
import traceback

from ConfigManager import ConfigManager
from common import get_md5_from_file
from fmd_backend_requests import fetch_app_manifest
from shell_command import execute_command
from config_post_injector import *

def handle_apk_signing(file_path, aosp_path, firmware_id, cookies):
    global POST_INJECTOR_CONFIG
    POST_INJECTOR_CONFIG = ConfigManager.get_config("POST_INJECTOR_CONFIG")
    error_message = None
    output = None
    is_success = False
    signing_key = get_signing_key_from_module(file_path, firmware_id, cookies)

    if not signing_key:
        error_message = f"Signing key name not found for {file_path}"
    signing_key_path = get_signing_key_path(aosp_path, signing_key)

    if not os.path.exists(signing_key_path):
        error_message = f"Signing key not found at {signing_key_path}"

    if not error_message:
        is_success, log_message = sign_apk_file(file_path, signing_key_path)
        if not is_success:
            error_message = (f"Error signing APK file with apksigner: "
                             f"Key:{signing_key}|"
                             f"Key Path: {signing_key_path}|"
                             f"Message:{log_message}")
        else:
            logging.info(f"APK file signed: {file_path} with key: {signing_key}")

    return is_success, output, (error_message, file_path, signing_key, signing_key_path)


def get_shared_user_from_manifest(firmware_id, android_apk_file_path, cookies):
    logging.info(f"Getting shared user from manifest: {android_apk_file_path}")
    filename = os.path.basename(android_apk_file_path)
    graphql_url = POST_INJECTOR_CONFIG['GRAPHQL_API_URL']
    manifest = fetch_app_manifest(graphql_url, cookies, firmware_id, filename)
    if manifest:
        logging.info(f"Found manifest for {android_apk_file_path} - {manifest}")

    if manifest and '@ns0:sharedUserId' in manifest:
        shared_user_id = manifest['@ns0:sharedUserId']
        logging.info(f"Shared User ID from manifest for {android_apk_file_path}: {shared_user_id}")
        return shared_user_id
    else:
        logging.warning(f"Shared User ID not found in manifest for {android_apk_file_path}")
    return None



def get_signing_key_from_module(android_apk_file_path, firmware_id, cookies):
    file_name = os.path.basename(android_apk_file_path)
    module_name = file_name.split(".")[0]
    android_mk_file_path = os.path.join(EXTRACTED_PACKAGES_PATH, module_name, "Android.mk")
    if not os.path.exists(android_mk_file_path):
        android_mk_file_path = os.path.join(EXTRACTED_PACKAGES_PATH, module_name, "Android.bp")

    logging.debug(f"Android.mk/Android.bp file path: {android_mk_file_path}")

    signing_key = None
    if os.path.exists(android_mk_file_path):
        with open(android_mk_file_path, "r") as file:
            for line in file:
                if "LOCAL_CERTIFICATE" in line:
                    signing_key = line.split("=")[1].strip()
                elif "certificate:" in line:
                    signing_key = line.split(":")[1].strip().replace('"', '').replace("'", "").replace(",", "")
                if signing_key is not None:
                    return signing_key.lower()
    else:
        logging.warning(f"Android.mk/Android.bp Module not found: {module_name} path {android_mk_file_path}."
                        f"File {android_apk_file_path} - fallback to FMD API.")
        shared_user_id = get_shared_user_from_manifest(firmware_id, android_apk_file_path, cookies)
        if shared_user_id in POST_INJECTOR_CONFIG["SHARED_USER_ID_MAPPING_DICT"].values():
            logging.debug(f"Shared User ID from FMD API for {android_apk_file_path}: {shared_user_id}")
            for key, value in POST_INJECTOR_CONFIG["SHARED_USER_ID_MAPPING_DICT"].items():
                if value == shared_user_id:
                    signing_key = key
                    return signing_key.lower()
        else:
            logging.error(f"APK SIGNING ERROR: Shared User ID not found or not mapped for {android_apk_file_path}: {shared_user_id}. "
                         f"Fallback to default signing key 'platform'.")
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


def sign_apk_file(apk_file_path, signing_key_path, v2_signing_enabled=True, v3_signing_enabled=True, v4_signing_enabled=True):
    """
    Signs the APK file with apksigner.

    :param apk_file_path: str - path to the APK file.
    :param signing_key_path: str - path to the signing key.

    """
    if not os.path.exists(apk_file_path):
        return False, f"Error: APK file not found for signing: {apk_file_path}"
    elif not os.path.exists(signing_key_path):
        return False, f"Error: Signing key not found for signing: {signing_key_path}"

    sign_command = ['sudo', 'apksigner', 'sign',
                    '--ks', signing_key_path,
                    '--v2-signing-enabled', str(v2_signing_enabled).lower(),
                    '--v3-signing-enabled', str(v3_signing_enabled).lower(),
                    '--v4-signing-enabled', str(v4_signing_enabled).lower(),
                    '--ks-pass', 'pass:',
                    '--verbose',
                    '--in', apk_file_path,
                    '--out', apk_file_path]
    success, log_message = execute_command(sign_command)
    logging.info(f"Signing APK file: {apk_file_path} with key: {signing_key_path} - success: {success} - {log_message} - sign_command: {sign_command}")
    return success, log_message

def verify_apk_file(apk_file_path):
    logging.info(f"Verifying APK file: {apk_file_path}")
    verify_command = ['apksigner', 'verify', apk_file_path]
    success, log_message = execute_command(verify_command)
    return success, log_message


def sign_apex_container_apksigner(apex_file_path,
                        signing_key_path,
                        signing_key_certificate_path,
                        v2_signing_enabled=True,
                        v3_signing_enabled=True,
                        v4_signing_enabled=True):
    """
    Signs the APEX file with apksigner.

    :param apex_file_path: str - path to the APK file.
    :param signing_key_path: str - path to the signing key.
    :param signing_key_certificate_path: str - path to the signing certificate (pem).
    :param v4_signing_enabled: bool - enable v4 signing.
    :param v3_signing_enabled: bool - enable v3 signing.
    :param v2_signing_enabled: bool - enable v2 signing.

    """

    sign_command = ['sudo', 'apksigner', 'sign',
                    '--key', signing_key_path,
                    '--cert', signing_key_certificate_path,
                    '--v2-signing-enabled', str(v2_signing_enabled).lower(),
                    '--v3-signing-enabled', str(v3_signing_enabled).lower(),
                    '--v4-signing-enabled', str(v4_signing_enabled).lower(),
                    '--verbose',
                    '--in', apex_file_path,
                    '--out', apex_file_path]
    success, log_message = execute_command(sign_command)
    logging.info(f"Signing APEX container file: {apex_file_path} "
                 f"with key: {signing_key_path} - {success} - {log_message} "
                 f"- sign_command: {sign_command}")
    return success, log_message


def sign_apex_container_signapk(apex_file_path,
                        signing_key_path,
                        signing_key_certificate_path,
                        aosp_path,
                        lunch_target):
    """
    java \
      -Djava.library.path=$(dirname out/host/linux-x86/lib64/libconscrypt_openjdk_jni.so)\
      -jar out/host/linux-x86/framework/signapk.jar \
      -a 4096 \
      <apk_certificate_file> \
      <apk_private_key_file> \
      <unsigned_input_file> \
      <signed_output_file>

    """
    if not os.path.exists(signing_key_path) or not os.path.exists(signing_key_certificate_path):
        return False, f"APEX ignoring key or certificate not found: {signing_key_path} - {signing_key_certificate_path}"
    elif not os.path.exists(apex_file_path):
        return False, f"APEX file not found for signing: {apex_file_path}"
    elif not os.path.exists(aosp_path):
        return False, f"AOSP path not found for signing: {aosp_path}"

    current_directory = os.path.dirname(os.path.realpath(__file__))
    os.chdir(aosp_path)

    try:
        apex_out_file_path = f"{apex_file_path}.signed"
        env_setup_command = f"bash -c 'cd {aosp_path} && source {aosp_path}build/envsetup.sh && lunch {lunch_target} && "
        sign_command = env_setup_command +  f"java -Djava.library.path={aosp_path}out/host/linux-x86/lib64/ " \
                                            f"-jar out/host/linux-x86/framework/signapk.jar " \
                                            f"--min-sdk-version 28 " \
                                            f"-a 4096 " \
                                            f"{signing_key_certificate_path} " \
                                            f"{signing_key_path} " \
                                            f"{apex_file_path} " \
                                            f"{apex_out_file_path}'"
        success, log_message = execute_command(sign_command, cwd=aosp_path, shell=True)
        logging.info(f"Signed APEX container file: {apex_file_path} "
                     f"with key: {signing_key_path} - {success} - {log_message} "
                     f"- sign_command: {sign_command}")
        if success:
            logging.info(f"Moving signed APEX container file: {apex_out_file_path} to {apex_file_path}")
            shutil.move(apex_out_file_path, apex_file_path)
            success = True
            log_message = None
        else:
            log_message = f"Error signing APEX container file: {apex_file_path} with key: {signing_key_path} - {log_message}"

        if os.path.exists(apex_out_file_path):
            logging.info(f"Removing signed APEX container file: {apex_out_file_path}")
            os.remove(apex_out_file_path)

    except Exception as e:
        success = False
        traceback.print_exc()
        log_message = f"Error signing APEX container file: {apex_file_path} with key: {signing_key_path} - {e}"
    finally:
        os.chdir(current_directory)
    return success, log_message