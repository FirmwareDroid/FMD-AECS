import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from jinja2 import Environment, FileSystemLoader
from aosp_module_type import get_module_type
from aosp_post_build_app_injector import get_signing_key_path, sign_apk_file, verify_apk_file, sign_apex_container
from shell_command import execute_shell_command
from config_post_injector import *

def handle_apex_modules(file_path, aosp_path, lunch_target, target_out_path):
    """
    Merges two apex file into one. Overwrite the apex of the vendor for later injection.
    """
    is_merge_success = False
    org_apex_file = f"{file_path}.original_apex"
    if os.path.exists(org_apex_file):
        logging.info(f"Original APEX file found - restoring: {org_apex_file}")
        restore_original_apex(file_path, org_apex_file)
    else:
        shutil.copyfile(file_path, org_apex_file)
        logging.info(f"Original APEX file not found creating new one: {org_apex_file}")

    apex_out_file = prepare_apex_out_file(file_path)
    if os.path.exists(apex_out_file):
        os.remove(apex_out_file)

    apex_emulator_folder = find_emulator_apex_folder(target_out_path, file_path)
    if apex_emulator_folder and os.path.exists(apex_emulator_folder):
        logging.info(f"Emulator APEX folder found for: {file_path} and {apex_emulator_folder}")
        is_merge_success, log_message = merge_apex_files(apex_emulator_folder, file_path, apex_out_file, lunch_target, aosp_path, target_out_path)
        os.remove(file_path)
        shutil.copyfile(apex_out_file, file_path)
        os.remove(apex_out_file)
        logging.info(f"Merging APEX file complete: {apex_out_file} overwrites {file_path} | {is_merge_success} | {log_message}")
    else:
        log_message = f"Error merging APEX file: {file_path} no emulator folder found in: {target_out_path}"
    return is_merge_success, log_message

def repackage_apex_file(aosp_path, apex_file_path, apex_out_file, lunch_target):
    """
    Extracts the APEX file using deapexer, repackages it using apexer, and signs all the APK files in the APEX using apksigner.

    :param aosp_path: str - path to the AOSP source code.
    :param apex_file_path: str - path to the APEX file.
    :param apex_out_file: str - path to the output file where the repackage APEX file will be saved.
    :param lunch_target: str - lunch target for the AOSP build.

    :return: tuple - (bool, str) - True if the repackage was successful, False otherwise. String containing the log.

    """
    filename = str(os.path.basename(apex_file_path)).replace(".apex", "")
    logging.info(f"Repackaging APEX file: {apex_file_path}")
    is_success = False
    try:
        apex_root_path = tempfile.mkdtemp(suffix=f"_{filename}_apex_repack")
        apex_extract_dir_path = tempfile.mkdtemp(dir=apex_root_path, suffix=f"_{filename}_extract")
        extract_success, log_message = extract_apex_file(aosp_path, apex_file_path, apex_extract_dir_path, lunch_target)
        if extract_success:
            logging.info(f"APEX extracted: {apex_file_path} to {apex_extract_dir_path}")
            with tempfile.NamedTemporaryFile(delete=False, dir=apex_root_path) as canned_fs_config:
                generate_canned_fs_config(apex_extract_dir_path, canned_fs_config.name)
            logging.info(f"Canned FS config file: {canned_fs_config.name}")

            raise RuntimeError("Need to implement the following functions")
            is_manifest_found, apex_manifest_path = move_apex_manifest_file(apex_extract_dir_path, apex_root_path,
                                                                            aosp_path, filename)
            if apex_manifest_path:
                copy_android_prebuilt_jar(aosp_path, apex_root_path)
                success, log_message, avb_pub_key_path, priv_pem_file_path, private_key_path, cert_apex_apk_path \
                    = create_apex_container(apex_manifest_path,
                                                                                                      apex_extract_dir_path,
                                                                                                      apex_root_path,
                                                                                                      aosp_path,
                                                                                                      apex_out_file,
                                                                                                      lunch_target,
                                                                                                      canned_fs_config)
                is_success, log_message = inject_apex_keys_module(apex_file_path, avb_pub_key_path, apex_out_file, priv_pem_file_path)
                if is_success:
                    logging.info(f"APEX extraction success: {apex_out_file}")
                    is_success, error_message = sign_apex_file(apex_out_file, aosp_path)
                    if is_success:
                        logging.info(f"APEX signing success: {apex_out_file}")
                        success, log_message = verify_apk_file(apex_out_file)
                        logging.info(f"APEX file verified: {apex_out_file} | {success} | {log_message}")
                    else:
                        logging.error(f"APEX signing failed: {apex_out_file} | {error_message}")
                        log_message = f"APEX signing failed. {error_message}"
                else:
                    log_message = f"APEX extraction failed. {apex_out_file} | {log_message}"
        else:
            log_message = f"APEX extraction failed. {apex_file_path} | {log_message}"
    except Exception as e:
        log_message = f"Error repackaging APEX file: {apex_file_path} | {str(e)}"
    return is_success, log_message

def create_apex_manifest(output_dir, apex_name):
    """
    Creates an apex_manifest.json file with default values.
    """
    manifest_content = f"{{\"name\": \"{apex_name}\",\n\"version\": 1}}"
    manifest_path = str(os.path.join(output_dir, "apex_manifest.json"))
    with open(manifest_path, "w") as manifest_file:
        manifest_file.write(manifest_content)


def inject_apex_keys_module(input_apex, avb_pub_key_path, output_file_path, priv_pem_file_path):
    logging.info(f"Injecting AVB public key in APEX module for repacker: {input_apex}")
    apex_main_folder = os.path.dirname(input_apex)
    is_success, log_message, public_key_name = copy_avb_public_key_to_apex_module(input_apex, apex_main_folder, avb_pub_key_path)
    key_id = public_key_name.replace(".pem", "")
    public_key_name = f"{key_id}.avbpubkey"
    priv_key_name = f"{key_id}.pem"
    priv_pem_filename = os.path.basename(priv_pem_file_path)
    if is_success:
        android_bp_file = os.path.join(apex_main_folder, "Android.bp")
        if os.path.exists(android_bp_file):
            with open(android_bp_file, 'r+') as android_bp:
                content = android_bp.read()
                if "apex_key" not in content:
                    insert_position = content.find('name:')
                    if insert_position != -1:
                        #content = content[:insert_position] + f"apex_key: {{public_key: \"{key_id}\",}},\n key: \"{key_id}\",\n" + content[insert_position:]
                        content += f'\n\napex_key {{\n    name: \"{key_id}\",\n    public_key: \"{public_key_name}\",\n    private_key: \"{priv_key_name}\", \n    installable: true\n}}'
                        android_bp.seek(0)
                        android_bp.write(content)
                        android_bp.truncate()
                        is_success = True
                        logging.info(f"AVB public key injected in APEX module Android.bp file: {android_bp_file}")
                        output_dir_path = os.path.dirname(output_file_path)
                        shutil.copyfile(avb_pub_key_path, os.path.join(output_dir_path, public_key_name))
                        shutil.copy2(android_bp_file, output_dir_path, follow_symlinks=False)
                        priv_pem_out_path = os.path.join(output_dir_path, priv_key_name)
                        shutil.copyfile(priv_pem_file_path, priv_pem_out_path)
                        logging.info(f"AVB public key and Android.bp file copied to APEX module: {output_file_path}")
                    else:
                        logging.error(f"Error injecting AVB public key in APEX module: {android_bp_file}")
                        is_success = False
                else:
                    logging.info(f"AVB public key already injected in APEX module: {android_bp_file}")
                    is_success = True
    else:
        logging.error(f"Error copying AVB public key to APEX module: {log_message}")
    return is_success, log_message

def copy_avb_public_key_to_apex_module(input_apex, apex_main_folder, avb_pub_key_path):
    is_success = False
    log_message = ""
    apex_name = os.path.basename(input_apex).replace(".apex", "")
    public_key_name = f"{apex_name}_pubkey.pem"
    public_key_out = os.path.join(apex_main_folder, public_key_name)
    shutil.copy2(avb_pub_key_path, public_key_out, follow_symlinks=False)
    logging.info(f"AVB public key copied to APEX build module: {public_key_out}")
    if os.path.exists(public_key_out):
        is_success = True
        logging.info(f"AVB public key copied to APEX build module: {public_key_out}")
    else:
        log_message = f"AVB public key not found in APEX build module: {public_key_out}"
    return is_success, log_message, public_key_name


def get_apex_build_intermediate_folder(target_out_path):
    apex_folder_path = os.path.join(target_out_path, "apex")
    if os.path.exists(apex_folder_path):
        apex_folder = apex_folder_path
    else:
        raise ValueError(f"APEX build intermediate folder not found: {apex_folder_path}")
    logging.info(f"APEX build intermediate folder: {apex_folder}")
    return apex_folder


def find_emulator_apex_folder(target_out_path, file_path):
    filename = str(os.path.basename(file_path))
    filename_no_vendor = filename.replace(".google", "").replace(".apex", "").replace(".capex", "")
    apex_emulator_folder_root = get_apex_build_intermediate_folder(target_out_path)
    logging.info(f"Searching for APEX module folder: {filename_no_vendor} in {apex_emulator_folder_root} for apex file {file_path}")
    apex_module_folder = os.path.join(apex_emulator_folder_root, filename_no_vendor)
    if os.path.exists(apex_module_folder):
        logging.info(f"APEX module folder found: {apex_module_folder} for apex {file_path}")
    else:
        apex_module_folder = None
        logging.warning(f"APEX module folder not found: {filename_no_vendor} for apex {file_path}")
    return apex_module_folder

# Keep the structure of the original apex
# Inject additional files into the apex
def merge_apex_files(apex_emulator_folder, input_apex, apex_out_file, lunch_target, aosp_path, target_out_path):
    """
    Merges the emulator APEX file with a vendor apex in case they have the same name.
    Keeps the structure of the emulator apex and injects additional files into the apex.
    Writes the merged apex to the apex_out_file
    """
    filename_input = str(os.path.basename(input_apex))
    logging.info(f"Merging APEX files: {apex_emulator_folder} and {input_apex}")
    is_success, log_message = False, None
    apex_root_path = tempfile.mkdtemp(suffix=f"_{filename_input}_merged")
    merged_apex_extract_dir_path = os.path.join(apex_root_path, "extract")
    apex_vendor_extract_dir_path = tempfile.mkdtemp(suffix=f"_{filename_input}_vendor")
    extract_success, log_message = extract_apex_file(aosp_path, input_apex, apex_vendor_extract_dir_path, lunch_target)
    if extract_success:
        shutil.copytree(apex_emulator_folder, merged_apex_extract_dir_path, dirs_exist_ok=True)
        if INJECT_APEX_VENDOR_FILES:
            logging.info(f"Injecting APEX vendor files: {apex_vendor_extract_dir_path} into {apex_emulator_folder}")
            inject_apex_vendor_files(merged_apex_extract_dir_path, apex_vendor_extract_dir_path, apex_emulator_folder)
        with tempfile.NamedTemporaryFile(delete=False) as canned_fs_config:
            generate_canned_fs_config(merged_apex_extract_dir_path, canned_fs_config.name)

        is_manifest_found, apex_manifest_path = move_apex_manifest_file(merged_apex_extract_dir_path, apex_root_path,
                                                                        aosp_path, filename_input)
        if is_manifest_found and os.path.exists(apex_manifest_path):
            if apex_manifest_path:
                copy_android_prebuilt_jar(aosp_path, apex_root_path)
                logging.info(f"APEX manifest file found: {apex_manifest_path}...start container creation")
                is_success, log_message, avb_pub_key_path, priv_pem_file_path, private_key_path, cert_apex_apk_path = create_apex_container(apex_manifest_path,
                                                                                  merged_apex_extract_dir_path,
                                                                                  apex_root_path,
                                                                                  aosp_path,
                                                                                  apex_out_file,
                                                                                  lunch_target,
                                                                                  canned_fs_config)
                if is_success:
                    is_success, error_message = sign_apex_file(apex_out_file,
                                                               aosp_path,
                                                               private_key_path,
                                                               cert_apex_apk_path)
                    if is_success:
                        logging.info(f"APEX signing success: {apex_out_file}")
                        is_success, log_message = verify_apk_file(apex_out_file)
                        logging.info(f"APEX file verified: {apex_out_file} | {is_success} | {log_message}")
                    else:
                        logging.error(f"APEX signing failed: {apex_out_file} | {error_message}")
                        log_message = f"APEX signing failed. {error_message}"

                    if REPLACE_AVB_KEYS:
                        logging.info(f"Overwriting AVB keys for APEX: {apex_out_file}")
                        is_success, log_message = inject_apex_avb_public_key(input_apex,
                                                                             avb_pub_key_path,
                                                                             target_out_path)
                else:
                    logging.error(f"APEX container creation failed: {apex_out_file} | {log_message}")
                    log_message = f"APEX container creation failed. {log_message}"
        else:
            log_message = f"APEX manifest file not found. {input_apex} | apex_manifest_path: {apex_manifest_path}"
    logging.info(f"APEX merge_apex_files success: {is_success} | {log_message} | out: {apex_out_file}")
    return is_success, log_message


def inject_apex_vendor_files(merged_apex_extract_dir_path, apex_vendor_extract_dir_path, apex_emulator_folder):
    logging.info(f"Injecting APEX vendor files: {apex_vendor_extract_dir_path} into {apex_emulator_folder}")
    files_coped_list = []
    for root, dirs, files in os.walk(apex_vendor_extract_dir_path):
        for file in files:
            file_path = str(os.path.join(root, file))
            module_type = get_module_type(file_path, is_apex=True)
            #if module_type == "SKIPPED":
            #    logging.error(f"APEX: Skipping file from APEX container {apex_emulator_folder}. Known problematic filename: {file_path}")
            #    continue
            if os.path.islink(file_path):
                dst_file_path = merged_apex_extract_dir_path + root.replace(apex_vendor_extract_dir_path, "").replace("//", "/")
                command = f'sudo cp -a {file_path} {dst_file_path}'
                result = subprocess.run(command, shell=True, capture_output=True, text=True)
                if result.returncode != 0:
                    logging.error(
                        f"Error copying file in APEX container: {file_path} with {dst_file_path} | {result.stderr}")
                if os.path.exists(dst_file_path):
                    logging.info(f"Copied symlink in APEX container: {file_path} with {dst_file_path}")
                    files_coped_list.append(dst_file_path)
            else:
                file_path_no_vendor = str(file_path.replace(".Google", "").replace(".google", ""))
                file_path_no_vendor = file_path_no_vendor.replace(apex_vendor_extract_dir_path, "")
                dst_file_path = os.path.join(merged_apex_extract_dir_path, file_path_no_vendor)
                logging.info(f"APEX: Tst {file_path} | {dst_file_path} | ")
                try:
                    if not os.path.islink(dst_file_path):
                        os.makedirs(os.path.dirname(dst_file_path), exist_ok=True)
                except PermissionError as e:
                    logging.error(f"Permission denied: {e.filename}")
                if "apex_manifest.pb" in dst_file_path or "apex_manifest.pb" in file_path or "fmd-aecs-lock" in file_path:
                    continue
                if file in DISALLOW_APEX_FILE_OVERWRITE:
                    logging.error(f"APEX: File in DISALLOW_APEX_FILE_OVERWRITE. Thus, not included: {file_path}")
                    continue

                try:
                    shutil.copy2(file_path, dst_file_path)
                    logging.info(f"Copied file into APEX container: {file_path} with {dst_file_path}")
                    files_coped_list.append(dst_file_path)
                except FileNotFoundError as e:
                    logging.error(f"APEX: File not found: {e.filename}")
                except PermissionError as e:
                    logging.error(f"APEX: Permission denied: {e.filename} | {e}")
                except Exception as e:
                    logging.error(f"APEX: Error copying file: {file_path} | {dst_file_path} | {e}")

        logging.info(f"APEX: Files copied into container: {files_coped_list};\n")

def change_file_permission(file_path, permission):
    try:
        command = ['sudo', 'chmod', permission, file_path]
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        print(f"Permissions for {file_path} changed to {permission}")
    except subprocess.CalledProcessError as e:
        print(f"Error changing permissions for {file_path}: {e.stderr}")

def change_file_ownership(file_path):
    try:
        current_user = os.getlogin()
        command = ['sudo', 'chown', current_user, file_path]
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        print(f"Ownership of {file_path} changed to {current_user}")
    except subprocess.CalledProcessError as e:
        print(f"Error changing ownership of {file_path}: {e.stderr}")

def can_read_file(file_path):
    return os.access(file_path, os.R_OK)

def get_aosp_default_keys(aosp_path):
    priv_key_path = os.path.join(aosp_path, "build/target/product/security/testkey.pem")
    pub_key_path = os.path.join(aosp_path, "build/target/product/security/testkey.avbpubkey")
    return priv_key_path, pub_key_path

def get_apex_default_keys(aosp_path, apex_file_name):
    apex_split_name_list = apex_file_name.split(".")
    logging.info(f"APEX: Getting default keys for: {apex_split_name_list}")
    for key, value in APEX_DEFAULT_PATHS_DICT.items():
        if key in apex_split_name_list:
            apex_file_name_no_extension = f"com.android.{key}"
            if key == "vndk":
                apex_file_name_no_extension = f"com.android.vndk.current"
            elif key == "statsd":
                apex_file_name_no_extension = f"com.android.os.statsd"
            elif key == "swcodec":
                apex_file_name_no_extension = f"com.android.media.swcodec"

            module_path = str(os.path.join(aosp_path, value))
            priv_pem_file_path = os.path.join(module_path, apex_file_name_no_extension + ".pem")
            priv_key_file_path = os.path.join(module_path, apex_file_name_no_extension + ".pk8")
            avb_pub_key_path = os.path.join(module_path, apex_file_name_no_extension + ".avbpubkey")
            cert_apex_apk_path = os.path.join(module_path, apex_file_name_no_extension + ".x509.pem")

            if (os.path.exists(priv_key_file_path)
                    and os.path.exists(priv_pem_file_path)
                    and os.path.exists(avb_pub_key_path)
                    and os.path.exists(cert_apex_apk_path)):
                logging.info(f"APEX: Default keys found: {priv_key_file_path} "
                             f"| {priv_pem_file_path} "
                             f"| {avb_pub_key_path} "
                             f"| {apex_file_name_no_extension}")
                return str(priv_key_file_path), str(priv_pem_file_path), str(avb_pub_key_path), str(cert_apex_apk_path)
            else:
                raise ValueError(f"Error getting APEX default keys: {apex_file_name}. "
                                 f"Key files not found in {module_path} with privat: {priv_pem_file_path}.")
    raise ValueError(f"Error getting APEX default keys: {apex_file_name}. Key files not found in {APEX_DEFAULT_PATHS_DICT}")


def create_apex_container(apex_manifest_path, apex_extract_dir_path, apex_root_path, aosp_path, output_file_path, lunch_target, canned_fs_config):
    success = False
    logging.info(f"APEX manifest file found: {apex_manifest_path}")
    resign_apex_apk_files(aosp_path, apex_extract_dir_path)
    apexer_bin_path = os.path.join(aosp_path, "out/soong/host/linux-x86/bin/apexer")
    apex_file_name = os.path.basename(output_file_path)
    info = f"APEX: Apexer tool path: {apexer_bin_path}|{lunch_target}|{apex_manifest_path}|{apex_extract_dir_path}|{output_file_path}|{canned_fs_config.name}|{FILE_CONTEXT_TEMPLATE_PATH}"
    logging.info(info)

    if REPLACE_AVB_KEYS:
        logging.info(f"Generating new AVB keys for APEX: {apex_file_name}")
        is_success, log_message, temp_keys_dir, private_key_path, priv_pem_file_path, public_key_path, avb_pub_key_path = (
            generate_apex_keys(apex_root_path, apex_file_name))
        extract_avb_public_key(aosp_path, private_key_path, avb_pub_key_path)
    else:
        logging.info(f"Using default AVB keys for APEX: {apex_file_name}")
        private_key_path, priv_pem_file_path, avb_pub_key_path, cert_apex_apk_path = get_apex_default_keys(aosp_path, apex_file_name)

    command = f"cd {apex_root_path} && {apexer_bin_path} --verbose " \
              f"--key={priv_pem_file_path} " \
              f"--pubkey={avb_pub_key_path} " \
              f"--apexer_tool_path={aosp_path}out/host/linux-x86/bin/:{aosp_path}out/soong/host/linux-x86/bin/ " \
              f"--file_contexts={FILE_CONTEXT_TEMPLATE_PATH} " \
              f"--canned_fs_config={canned_fs_config.name} " \
              f"--include_build_info " \
              f"--force " \
              f"{apex_extract_dir_path} " \
              f"{output_file_path}"
    logging.info(f"Apexer Repacking command: {command}")

    if os.path.exists(apex_root_path) \
        and os.path.exists(apexer_bin_path) \
        and os.path.exists(apex_manifest_path) \
        and os.path.exists(apex_extract_dir_path) \
        and os.path.exists(canned_fs_config.name) \
        and os.path.exists(FILE_CONTEXT_TEMPLATE_PATH) \
        and os.path.exists(avb_pub_key_path) \
        and os.path.exists(priv_pem_file_path):
        log_files_in_dir(apex_root_path)
        is_success, log_message = execute_shell_command(command, aosp_path)
        if is_success and os.path.exists(output_file_path):
            logging.info(f"APEX create_apex_container success: {output_file_path}. Command-Log: {log_message}")
            success = True
        else:
            log_message = f"APEX create_apex_container failed. Error-Info: {log_message} | Debug INFO: {info}"
            logging.error(f"{log_message}")
    else:
        log_message = f"APEX create_apex_container failed. Error-Info: Missing files. Debug INFO: {info}"

    return success, log_message, avb_pub_key_path, priv_pem_file_path, private_key_path, cert_apex_apk_path

def log_files_in_dir(dir_path):
    files_and_dirs = []
    for root, dirs, files in os.walk(dir_path):
        for file in files:
            files_and_dirs.append(os.path.join(root, file))
        for dir_name in dirs:
            files_and_dirs.append(os.path.join(root, dir_name))
    logging.info(f"APEX: Files and directories in {dir_path}: {files_and_dirs}")


def sign_apex_file(file_path, aosp_path, priv_key_apex_apk_path, apex_apk_certificate_path):
    error_message = None
    #signing_key_path = get_signing_key_path(aosp_path, "platform")
    is_success, log_message = sign_apex_container(file_path, priv_key_apex_apk_path, apex_apk_certificate_path)
    if is_success:
        logging.info(f"APEX file signed: {file_path} with key: {priv_key_apex_apk_path}")
        success, log_message = verify_apk_file(file_path)
        logging.info(f"APEX file verified: {file_path} | {success} | {log_message}")
    else:
        error_message = f"Error signing APEX file: {file_path}|{priv_key_apex_apk_path}|{log_message}"
    #else:
    #    logging.error(f"Error generating APEX keys:  {log_message}")
    #    error_message = f"Error generating APEX keys: {log_message}"
    return is_success, error_message


def convert_apex_keys_to_p12(private_key_path, public_key_path, p12_path):
    """
    Converts the private and public keys to a p12 file.
    :param private_key_path: str - path to the private key.
    :param public_key_path: str - path to the public key.
    :param p12_path: str - path to the p12 file.

    :return: Tuple - (bool, str) - True if the conversion was successful, False otherwise. String containing the log.
    """
    if not os.path.exists(private_key_path) or not os.path.exists(public_key_path):
        return False, "Private or public key not found."
    is_success = False
    command = f"openssl pkcs12 -export -out {p12_path} -inkey {private_key_path} -in {public_key_path} -passout pass:"
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    if result.returncode == 0:
        is_success = True
        log_message = result.stdout
    else:
        log_message = result.stderr
    return is_success, log_message


def restore_original_apex(file_path, org_apex_file):
    os.remove(file_path)
    shutil.copyfile(org_apex_file, file_path)


def prepare_apex_out_file(file_path):
    apex_filename_new = str(os.path.basename(file_path).replace(".apex", ".v2.apex"))
    apex_dir_path = str(os.path.dirname(file_path))
    return str(os.path.join(apex_dir_path, apex_filename_new))


def get_apex_signing_key_from_filename(file_path):
    file_name = os.path.basename(file_path)
    if "media" in file_name:
        return "media"
    elif any(keyword in file_name for keyword in ["network", "tethering", "wifi", "bluetooth", "cellbroadcast"]):
        return "networkstack"
    else:
        return "platform"


def generate_canned_fs_config(apex_extract_dir_path, output_file):
    """
    Generates a canned_fs_config file for the given directory. The config contains the file paths and their
    permissions. The method gives all the files and directories the default permissions.

    :param apex_extract_dir_path: str - path to the directory where the extracted apex files reside.
    :param output_file: str - path to the output file where the canned_fs_config will be saved.

    """
    file_inserted_entries = []
    with open(output_file, 'w') as out_file:
        out_file.write(f"/ 1000 1000 0755\n")
        for root, dirs, files in os.walk(apex_extract_dir_path):
            for dir_name in dirs:
                dir_path = str(os.path.join(root, dir_name))
                relative_dir_path = os.path.relpath(dir_path, apex_extract_dir_path)
                user_id = 0  # root
                group_id = 2000  # system
                mode = '0755'
                out_file.write(f"/{relative_dir_path} {user_id} {group_id} {mode}\n")
                file_inserted_entries.append(f"/{relative_dir_path} {user_id} {group_id} {mode}")

            for file_name in files:
                file_path = str(os.path.join(root, file_name))
                #module_type = get_module_type(file_path, is_apex=True)

                # if module_type == "SKIPPED":
                #     try:
                #         logging.error(f"APEX: SKIPPED module type. File not included into canned_fs: {file_path}")
                #         os.remove(file_path)
                #     except Exception as e:
                #         logging.error(f"Error deleting file from canned_fs: {file_path} | {e}")
                #     continue
                # else:
                #     logging.info(f"APEX: Adding file to canned_fs: {file_path}")
                is_apk = file_path.endswith(".apk")
                if is_apk:
                    try:
                        logging.error(f"APEX: SKIPPED module type. File not included into canned_fs: {file_path}")
                        os.remove(file_path)
                    except Exception as e:
                        logging.error(f"Error deleting file from canned_fs: {file_path} | {e}")
                    continue
                elif "apex_pubkey" in file_name:
                    logging.info(f"APEX: SKIPPED apex_pubkey file. File not included: {file_path}")
                    os.remove(file_path)

                relative_file_path = os.path.relpath(file_path, apex_extract_dir_path)
                user_id = 1000  # system
                group_id = 1000  # system
                mode = '0644'
                if os.access(file_path, os.X_OK):
                    mode = '0755'  # Executable files get 0755
                out_file.write(f"/{relative_file_path} {user_id} {group_id} {mode}\n")
                file_inserted_entries.append(f"/{relative_file_path} {user_id} {group_id} {mode}")
                # Workaround for boot.art file in APEX
                if "boot" in file_name and "arm64" in file_path:
                    parent_dir = os.path.dirname(file_path)
                    grandparent_dir = os.path.dirname(parent_dir)
                    copy_dst = os.path.join(grandparent_dir, file_name)
                    logging.info(f"APEX Copying boot.art javalib: {file_path}:{copy_dst}")
                    shutil.copyfile(file_path, copy_dst)
                    relative_file_path = os.path.relpath(copy_dst, apex_extract_dir_path)
                    logging.info(f"APEX Write new boot.art path: {relative_file_path}:{copy_dst}:{parent_dir}")
                    out_file.write(f"/{relative_file_path} {user_id} {group_id} {mode}\n")
                    file_inserted_entries.append(f"/{relative_file_path} {user_id} {group_id} {mode}")
    logging.info(f"APEX: Canned FS Config file created: {output_file} | {file_inserted_entries}")

def extract_apex_file(aosp_path, apex_file_path, output_dir_path, lunch_target):
    """
    Extracts the APEX file using deapexer.

    :param aosp_path: str - path to the AOSP source code.
    :param apex_file_path: str - path to the APEX file.
    :param output_dir_path: str - path to the output directory where the apex will be extracted to.
    :param lunch_target: str - lunch target for the AOSP build.

    :return: bool - True if the extraction was successful, False otherwise.

    """
    logging.info(f"Extracting APEX file: {apex_file_path}")
    #deapexer_tool_path = f"{aosp_path}out/host/linux-x86/bin/deapexer"
    deapexer_tool_path = f"{aosp_path}out/soong/host/linux-x86/bin/deapexer"
    info = f"APEX: Deapexer tool path: {deapexer_tool_path}|{lunch_target}|{apex_file_path}|{output_dir_path}"
    logging.info(info)
    command = f"bash -c 'source {aosp_path}build/envsetup.sh && lunch {lunch_target} " \
               f"&& {deapexer_tool_path} extract {apex_file_path} {output_dir_path}'"
    is_success, log = execute_shell_command(command, aosp_path)
    logging.info(f"APEX: Deapexer extraction command: {command} | {is_success} | {log}")
    return is_success, {f"{log}|{info}"}


def create_apex_manifest_file(apex_extract_dir_path, apex_package_name):
    manifest_file_name = "AndroidManifest.json"
    manifest_file_path = os.path.join(apex_extract_dir_path, manifest_file_name)
    with open(manifest_file_path, 'w') as manifest_file:
        template_folder_abs_path = os.path.join(ROOT_PATH, TEMPLATE_FOLDER)
        environment = Environment(loader=FileSystemLoader(str(template_folder_abs_path)))
        template = environment.get_template(manifest_file_name)
        rendered_template = template.render(package=apex_package_name, versionCode=999)
        manifest_file.write(rendered_template)


def move_apex_manifest_file(apex_extract_dir_path, output_dir_path, aosp_path, apex_file_name):
    """
    Searches for the APEX manifest file in the APEX extract directory and moves it to the current directory.
    Moving is necessary because the apexer tool requires the manifest file to be in the same directory as the APEX files and not in
    the subdirectory.

    :param apex_extract_dir_path: str - path to the APEX extract directory.
    :param output_dir_path: str - path to the output directory where the APEX manifest file will be copied to.

    :return: bool - True if the APEX manifest file was found and copied, False otherwise.

    """
    logging.info(f"Copying APEX manifest file.")
    is_apex_manifest_file_found = False
    manifest_dst = None
    for root, dirs, files in os.walk(apex_extract_dir_path):
        for file in files:
            if file == "apex_manifest.pb":
                file_path = str(os.path.join(root, file))
                manifest_dst = os.path.join(output_dir_path, "apex_manifest.pb")
                shutil.move(file_path, manifest_dst)
                if os.path.exists(file_path):
                    logging.info(f"Found APEX manifest file: {file_path} to delete")
                    os.remove(file_path)
                #manifest_json_file_path = get_apex_manifest_from_aosp(aosp_path, apex_file_name)
                #convert_apex_manifest_json_to_pb(manifest_json_file_path, manifest_dst)
                logging.info(f"Copied APEX manifest file: {file_path} to {manifest_dst}.")
                if os.path.exists(manifest_dst):
                    is_apex_manifest_file_found = True
                    logging.info(f"APEX manifest file found: {manifest_dst}")
                break
    return is_apex_manifest_file_found, str(manifest_dst)

def convert_apex_manifest_json_to_pb(apex_manifest_path, output_file_path):
    command = f"export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python && python3 ./conv_apex_manifest.py proto {apex_manifest_path} -o {output_file_path}"
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    logging.info(f"Converting APEX manifest file to pb: {command}")
    if result.returncode == 0:
        logging.info(f"APEX: APEX manifest file converted to pb: {output_file_path}")
    else:
        raise ValueError(f"APEX: Error converting APEX manifest file to pb: {result.stderr}")

def get_apex_manifest_from_aosp(aosp_path, apex_file_name):
    apex_split_name_list = apex_file_name.split(".")
    for key, value in APEX_DEFAULT_PATHS_DICT.items():
        if key in apex_split_name_list:
            manifest_file_path = os.path.join(aosp_path, value, "apex_manifest.json")
            manifest_file_path2 = os.path.join(aosp_path, value, "manifest.json")
            if os.path.exists(manifest_file_path):
                logging.info(f"APEX apex_manifest.json file found: {manifest_file_path}")
                return manifest_file_path
            elif os.path.exists(manifest_file_path2):
                logging.info(f"APEX apex_manifest.json file found: {manifest_file_path2}")
                return manifest_file_path2
            else:
                raise ValueError(f"Error getting APEX manifest file: {apex_file_name}. Manifest file not found in {manifest_file_path}.")


def search_string_in_apk(apk_file, search_string):
    is_user_id_found = False
    with zipfile.ZipFile(apk_file, 'r') as apk:
        for file_info in apk.infolist():
            if file_info.filename == "AndroidManifest.xml":
                with apk.open(file_info) as android_manifest_file:
                    try:
                        content = android_manifest_file.read().decode('utf-8', errors='ignore')
                        if search_string in content:
                            logging.info(f"Found string in APK: {apk_file}")
                            is_user_id_found = True
                            break
                    except UnicodeDecodeError:
                        pass
                break
    return is_user_id_found


def get_signing_key_from_manifest(apk_file):
    signing_key = None
    for key, shared_uid_list in SHARED_USER_ID_MAPPING_DICT.items():
        for shared_uid in shared_uid_list:
            if search_string_in_apk(apk_file, shared_uid):
                signing_key = key
                break
    return signing_key

def get_signing_key_from_filename(apk_file):
    file_name = os.path.basename(apk_file).lower()
    if "media" in file_name:
        key = "media"
    elif any(keyword in file_name for keyword in ["network", "tethering", "cellbroadcast"]):
        key = "networkstack"
    else:
        key = "platform"
    return key


def resign_apex_apk_files(aosp_path, apex_extract_dir_path):
    """
    Searches for apk files within the apex extract directory. Signs all the apk files of the apex file.

    :param apex_extract_dir_path: str - path to the APEX extract directory.

    """
    logging.info(f"Resigning APK files in APEX.")
    for root, dirs, files in os.walk(apex_extract_dir_path):
        for file in files:
            if file.endswith(".apk"):
                apk_file_path = os.path.join(root, file)
                signing_key = get_signing_key_from_manifest(apk_file_path)
                if signing_key is None:
                    signing_key = get_signing_key_from_filename(apk_file_path)
                    logging.info(f"Signing key not found in APK manifest. Using filename to determine key: {apk_file_path} | {signing_key}")
                else:
                    logging.info(f"Signing key found in APK manifest: {apk_file_path}. Using manifest to determine key: {signing_key}")

                signing_key_path = get_signing_key_path(aosp_path, signing_key)
                success, log_message = sign_apk_file(apk_file_path, signing_key_path, v4_signing_enabled=False)
                if success:
                    logging.info(f"APEX: Success resigning APK file: {file}|{apk_file_path} with key {signing_key_path}")
                    is_signature_verified, log_message = verify_apk_file(apk_file_path)
                    logging.info(f"APEX: APK file verified: {apk_file_path} | {is_signature_verified} | {log_message}")
                else:
                    logging.error(f"APEX: Error resigning APK file: {file}|{apk_file_path} with key {signing_key_path} | {log_message}")
    logging.info(f"Resigning APK files in APEX complete.")


def copy_android_prebuilt_jar(aosp_path, apex_root_path):
    prebuilt_folder = "prebuilts/sdk/current/public/"
    jar_name = "android.jar"
    android_jar_file_path = os.path.join(aosp_path, prebuilt_folder, jar_name)
    extract_android_jar_file_path = os.path.join(apex_root_path, prebuilt_folder)
    os.makedirs(extract_android_jar_file_path, exist_ok=True)
    if not os.path.exists(android_jar_file_path):
        logging.error(f"Android jar file not found: {android_jar_file_path}")
    else:
        logging.info(f"Copying Android jar file: {android_jar_file_path} to {extract_android_jar_file_path}")
        shutil.copy2(android_jar_file_path, extract_android_jar_file_path, follow_symlinks=False)


def create_key_paths(apex_root_path, apex_file_name):
    temp_keys_dir = tempfile.mkdtemp(dir=apex_root_path, suffix="_apex_keys")
    priv_key_path = os.path.join(temp_keys_dir, f"{apex_file_name}.key")
    priv_pem_file_path = os.path.join(temp_keys_dir, f"{apex_file_name}.pem")
    pub_key_path = os.path.join(temp_keys_dir, f"{apex_file_name}.pubkey")
    avb_pub_key_path = os.path.join(temp_keys_dir, f"{apex_file_name}.avbpubkey")
    return temp_keys_dir, priv_key_path, priv_pem_file_path, pub_key_path, avb_pub_key_path


def generate_apex_keys(apex_root_path, apex_file_name):
    temp_keys_dir, private_key_path, priv_pem_file_path, public_key_path, avb_pub_key_path = create_key_paths(apex_root_path, apex_file_name)
    is_success = False
    command = [
        'openssl', 'req', '-x509', '-newkey', 'rsa:4096', '-keyout', private_key_path,
        '-out', public_key_path, '-days', '99999', '-nodes', '-subj', '/CN=example.com'
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(private_key_path) or not os.path.exists(public_key_path):
        log_message = f"Error generating keys: {result.stderr}"
        logging.error(log_message)
    else:
        logging.info(f"Keys generated successfully: {private_key_path}, {public_key_path}")
        log_message = result.stdout
        with open(private_key_path, "rb") as key_file:
            private_key = key_file.read()
        with open(priv_pem_file_path, "wb") as pem_out:
            pem_out.write(private_key)
        logging.info(f"PEM file generated successfully: {priv_pem_file_path}")
        is_success = True
    return is_success, log_message, temp_keys_dir, private_key_path, priv_pem_file_path, public_key_path, avb_pub_key_path


def generate_apex_keys_p12(private_key_path, public_key_path, p12_path):
    """
    Generates a private key, a public key, and converts them to a .p12 file.

    :param private_key_path: str - path to the private key.
    :param public_key_path: str - path to the public key.
    :param p12_path: str - path to the .p12 file.
    :return: Tuple - (bool, str) - True if the generation was successful, False otherwise. String containing the log.
    """
    is_success = False
    log_message = ""

    # Generate private and public keys
    command = [
        'openssl', 'req', '-x509', '-newkey', 'rsa:4096', '-keyout', private_key_path,
        '-out', public_key_path, '-days', '365', '-nodes', '-subj', '/CN=example.com'
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(private_key_path) or not os.path.exists(public_key_path):
        log_message = f"Error generating keys: {result.stderr}"
        logging.error(log_message)
    else:
        logging.info(f"Keys generated successfully: {private_key_path}, {public_key_path}")
        log_message = result.stdout

        # Convert keys to .p12
        command = f"openssl pkcs12 -export -out {p12_path} -inkey {private_key_path} -in {public_key_path} -passout pass:"
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            log_message = result.stdout
            logging.info(f".p12 file generated successfully: {p12_path}")
            is_success = True
        else:
            log_message = result.stderr
            logging.error(f"Error converting keys to .p12: {log_message}")

    return is_success, log_message


def extract_avb_public_key(aosp_path, key, avb_pub_out_path):
    """
    Extracts the AVB public key from the given RSA private key.

    :param key: str - path to the RSA private key.
    :param avb_pub_out_path: str - path to the output file where the AVB public key will
    :param aosp_path: str - log message to return in case of an error.

    """
    avbtool_path = os.path.join(aosp_path, "out/host/linux-x86/bin/avbtool")
    avb_extract_command = [avbtool_path, 'extract_public_key', "--key", key, "--output", avb_pub_out_path]
    subprocess.run(avb_extract_command, check=True)
    logging.info(f"AVB public key extracted at: {avb_pub_out_path}")


def inject_apex_avb_public_key(apex_file_path, avb_pub_key_path, target_out_path):
    is_success, log_message = replace_apex_avb_public_key(apex_file_path, avb_pub_key_path, target_out_path)
    if is_success:
        logging.info(f"APEX: AVB public key replaced: {apex_file_path}")
    else:
        log_message = f"APEX: AVB public key replacement failed. {log_message}"
    return is_success, log_message


def replace_apex_avb_public_key(apex_file_path, avb_pub_key_path, target_out_path):
    """
    Replaces the AVB public key in the APEX file with the given public key.
    :param apex_file_path: str - path to the APEX file.
    :param avb_pub_key_path: str - path to the AVB public key file.

    :return: tuple - (bool, str) - True if the replacement was successful, False otherwise. String containing the log.
    """
    is_success = False
    apex_filename = os.path.basename(apex_file_path)
    # TODO add better file matching
    apex_filename_no_ext = os.path.splitext(apex_filename)[0].replace(".google", "").replace("Google", "")

    apex_pub_key_obj_path = str(os.path.join(target_out_path, FOLDER_NAME_OBJECTS, "ETC",
                                             f"apex_pubkey.{apex_filename_no_ext}_intermediates"))
    apex_pub_file_path = os.path.join(apex_pub_key_obj_path, "apex_pubkey")
    log_message = None
    logging.info(f"APEX public key file path: {apex_pub_file_path} | {apex_file_path}")
    if not os.path.exists(apex_pub_file_path):
        logging.info(f"AVB public key file to replace not found: {apex_pub_file_path}")
        log_message = f"AVB public key file to replace not found: {apex_pub_file_path}"
    elif not os.path.exists(avb_pub_key_path):
        logging.info(f"AVB public key file not found: {avb_pub_key_path}")
        log_message = f"AVB public key file not found: {avb_pub_key_path}"
    else:
        is_success = True
        md5 = hashlib.md5(open(avb_pub_key_path, 'rb').read()).hexdigest()
        logging.info(f"Replacing AVB public key: src: {avb_pub_key_path}:{md5}, dst: {apex_pub_file_path}")
        shutil.copy2(avb_pub_key_path, apex_pub_file_path, follow_symlinks=False)
    return is_success, log_message
