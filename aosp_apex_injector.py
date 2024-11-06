import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from fileinput import filename

from jinja2 import Environment, FileSystemLoader
from aosp_module_type import get_module_type
from aosp_post_build_app_injector import get_signing_key_path, sign_apk_file
from shell_command import execute_shell_command
from config_post_injector import *

def handle_apex_modules(file_path, aosp_path, lunch_target, target_out_path):
    org_apex_file = f"{file_path}.original_apex"
    if os.path.exists(org_apex_file):
        restore_original_apex(file_path, org_apex_file)
    apex_out_file = prepare_apex_out_file(file_path)
    if os.path.exists(apex_out_file):
        os.remove(apex_out_file)

    apex_emulator_folder = find_emulator_apex_folder(target_out_path, file_path)
    if apex_emulator_folder and os.path.exists(apex_emulator_folder):
        logging.info(f"Emulator APEX folder found for: {file_path} and {apex_emulator_folder}")
        is_repack_success, log_message = merge_apex_files(apex_emulator_folder, file_path, apex_out_file, lunch_target, aosp_path)
    else:
        is_repack_success, log_message = repackage_apex_file(aosp_path,
                                                             file_path,
                                                             apex_out_file,
                                                             lunch_target,
                                                             target_out_path)

    if is_repack_success:
        error_message = sign_apex_file(file_path, aosp_path, apex_out_file)
    else:
        error_message = f"Error repackaging/merging APEX file: {file_path}|{log_message}"

    return error_message

def repackage_apex_file(aosp_path, apex_file_path, output_file_path, lunch_target, target_out_path):
    """
    Extracts the APEX file using deapexer, repackages it using apexer, and signs all the APK files in the APEX using apksigner.

    :param aosp_path: str - path to the AOSP source code.
    :param apex_file_path: str - path to the APEX file.
    :param output_file_path: str - path to the output file where the repackage APEX file will be saved.
    :param lunch_target: str - lunch target for the AOSP build.

    :return: tuple - (bool, str) - True if the repackage was successful, False otherwise. String containing the log.
    """
    filename = str(os.path.basename(apex_file_path))
    logging.info(f"Repackaging APEX file: {apex_file_path}")
    success = False
    try:
        apex_root_path = tempfile.mkdtemp(suffix=f"_{filename}_apex_repack")
        apex_extract_dir_path = os.path.join(apex_root_path, "extract")
        extract_success, log_message = extract_apex_file(aosp_path, apex_file_path, apex_extract_dir_path, lunch_target)
        if extract_success:
            logging.info(f"APEX extracted: {apex_file_path}")
            with tempfile.NamedTemporaryFile(delete=False, dir=apex_root_path) as canned_fs_config:
                generate_canned_fs_config(apex_extract_dir_path, canned_fs_config.name)
            logging.info(f"Canned FS config file: {canned_fs_config.name}")

            is_manifest_found, apex_manifest_path = move_apex_manifest_file(apex_extract_dir_path, apex_root_path)
            copy_android_prebuilt_jar(aosp_path, apex_root_path)
            if is_manifest_found and os.path.exists(apex_manifest_path):
                success, log_message, avb_pub_key_path = create_apex_container(apex_manifest_path, apex_extract_dir_path, apex_root_path, aosp_path, output_file_path, lunch_target, canned_fs_config)
                if success:
                    is_success, log_message = inject_apex_avb_public_key(apex_file_path, avb_pub_key_path, target_out_path)
                else:
                    log_message = f"APEX repackaging failed. {log_message}"
            else:
                log_message = f"APEX manifest file not found. {apex_file_path} | apex_manifest_path: {apex_manifest_path}"
        else:
            log_message = f"APEX extraction failed. {apex_file_path} | {log_message}"
    except Exception as e:
        log_message = f"Error repackaging APEX file: {apex_file_path} | {str(e)}"
    return success, log_message

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
    logging.info(f"Searching for APEX module folder: {filename_no_vendor} in {apex_emulator_folder_root}")
    apex_module_folder = os.path.join(apex_emulator_folder_root, filename_no_vendor)
    if os.path.exists(apex_module_folder):
        logging.info(f"APEX module folder found: {apex_module_folder} for apex {file_path}")
    else:
        apex_module_folder = None
        logging.warning(f"APEX module folder not found: {filename_no_vendor} for apex {file_path}")
    return apex_module_folder


# Keep the structure of the original apex
# Inject additional files into the apex
def merge_apex_files(apex_emulator_folder, input_apex, apex_out_file, lunch_target, aosp_path):
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
        inject_apex_vendor_files(merged_apex_extract_dir_path, apex_vendor_extract_dir_path, apex_emulator_folder)
        with tempfile.NamedTemporaryFile(delete=False) as canned_fs_config:
            generate_canned_fs_config(merged_apex_extract_dir_path, canned_fs_config.name)
        is_manifest_found, apex_manifest_path = move_apex_manifest_file(merged_apex_extract_dir_path, apex_root_path)
        if is_manifest_found and os.path.exists(apex_manifest_path):
            success, log_message, avb_pub_key_path = create_apex_container(apex_manifest_path, merged_apex_extract_dir_path, apex_root_path, aosp_path, apex_out_file, lunch_target, canned_fs_config)
            if success:
                is_success, log_message = inject_apex_avb_public_key(input_apex, avb_pub_key_path, apex_out_file)
        else:
            log_message = f"APEX manifest file not found. {input_apex} | apex_manifest_path: {apex_manifest_path}"
    return is_success, log_message


def inject_apex_vendor_files(merged_apex_extract_dir_path, apex_vendor_extract_dir_path, apex_emulator_folder):
    logging.info(f"Injecting APEX vendor files: {apex_vendor_extract_dir_path} into {apex_emulator_folder}")
    for root, dirs, files in os.walk(apex_vendor_extract_dir_path):
        for file in files:
            file_path = str(os.path.join(root, file))
            module_type = get_module_type(file_path)
            if module_type == "SKIPPED":
                logging.error(f"Skipping file from APEX {apex_emulator_folder}. Known problematic filename: {file_path}")
            else:
                dst = os.path.join(merged_apex_extract_dir_path, os.path.relpath(file_path, apex_vendor_extract_dir_path))
                logging.info(f"Copying file to APEX destination: {dst}")
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if not os.path.exists(dst):
                    logging.info(f"Adding file to APEX: from {file_path} to {merged_apex_extract_dir_path}")
                    shutil.copy(file_path, dst)
                else:
                    if file in ALLOW_APEX_FILE_OVERWRITE:
                        logging.info(f"Overwriting file in APEX: {file_path}")
                        shutil.copy(file_path, dst)
                    else:
                        logging.error(f"File already exists in APEX and will not be copied: {file_path}")


def create_apex_container(apex_manifest_path, apex_extract_dir_path, apex_root_path, aosp_path, output_file_path, lunch_target, canned_fs_config):
    success = False
    logging.info(f"APEX manifest file found: {apex_manifest_path}")
    resign_apex_apk_files(apex_extract_dir_path)
    apexer_bin_path = os.path.join(aosp_path, "out/soong/host/linux-x86/bin/apexer")
    info = f"Apexer tool path: {apexer_bin_path}|{lunch_target}|{apex_manifest_path}|{apex_extract_dir_path}|{output_file_path}|{canned_fs_config.name}|{FILE_CONTEXT_TEMPLATE_PATH}"
    logging.info(info)
    temp_keys_dir = tempfile.mkdtemp(dir=apex_root_path)
    priv_key_path = os.path.join(temp_keys_dir, "priv.key")
    pub_key_path = os.path.join(temp_keys_dir, "pub.key")
    avb_pub_key_path = os.path.join(temp_keys_dir, "apex_pubkey")
    generate_apex_keys(priv_key_path, pub_key_path)
    extract_avb_public_key(aosp_path, priv_key_path, avb_pub_key_path)
    # f"--android_manifest={apex_manifest_path} " \
    command = f"cd {apex_root_path} && {apexer_bin_path} --verbose " \
              f"--key={priv_key_path} " \
              f"--pubkey={avb_pub_key_path} " \
              f"--apexer_tool_path={aosp_path}out/host/linux-x86/bin/:{aosp_path}out/soong/host/linux-x86/bin/ " \
              f"--file_contexts={FILE_CONTEXT_TEMPLATE_PATH} " \
              f"--canned_fs_config={canned_fs_config.name} " \
              f"{apex_extract_dir_path} " \
              f"{output_file_path}"
    logging.info(f"Apexer Repacking command: {command}")
    is_success, log_message = execute_shell_command(command, aosp_path)
    if is_success:
        logging.info(f"APEX repackaged: {output_file_path}")
        success = True
    else:
        log_message = f"APEX repackaging failed. {log_message} | {info}"

    return success, log_message, avb_pub_key_path


def sign_apex_file(file_path, aosp_path, apex_out_file):
    error_message = None
    signing_key = get_apex_signing_key_from_filename(file_path)
    signing_key_path = get_signing_key_path(aosp_path, signing_key)
    if signing_key_path:
        is_success, log_message = sign_apk_file(file_path, signing_key_path)
        if is_success:
            logging.info(f"APEX file signed: {file_path} with key: {signing_key}")
            if not os.path.exists(f"{file_path}.original_apex"):
                shutil.move(file_path, f"{file_path}.original_apex")
            shutil.move(apex_out_file, file_path)
        else:
            error_message = f"Error signing APEX file: {file_path}|{signing_key}|{signing_key_path}|{log_message}"
    else:
        error_message = f"Signing key name not found for {file_path}"
    return error_message

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
    file_inserted_counter = 0
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

            for file_name in files:
                file_path = str(os.path.join(root, file_name))
                module_type = get_module_type(file_path)
                if module_type == "SKIPPED":
                    try:
                        os.remove(file_path)
                    except Exception as e:
                        logging.error(f"Error deleting file: {file_path} | {e}")
                    continue
                else:
                    logging.info(f"Adding file to APEX: {file_path}")

                relative_file_path = os.path.relpath(file_path, apex_extract_dir_path)
                user_id = 1000  # system
                group_id = 1000  # system
                mode = '0644'
                if os.access(file_path, os.X_OK):
                    mode = '0755'  # Executable files get 0755
                out_file.write(f"/{relative_file_path} {user_id} {group_id} {mode}\n")
                file_inserted_counter += 1
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
                    file_inserted_counter += 1

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
    deapexer_tool_path = f"{aosp_path}out/host/linux-x86/bin/deapexer"
    info = f"Deapexer tool path: {deapexer_tool_path}|{lunch_target}|{apex_file_path}|{output_dir_path}"
    logging.info(info)
    command = f"bash -c 'source {aosp_path}build/envsetup.sh && lunch {lunch_target} " \
               f"&& {deapexer_tool_path} extract {apex_file_path} {output_dir_path}'"
    is_success, log = execute_shell_command(command, aosp_path)
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


def move_apex_manifest_file(apex_extract_dir_path, output_dir_path):
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
    result_file_path = None
    for root, dirs, files in os.walk(apex_extract_dir_path):
        for file in files:
            if file == "apex_manifest.pb":
                file_path = str(os.path.join(root, file))
                shutil.move(file_path, str(os.path.join(output_dir_path, file)))
                logging.info(f"Copied APEX manifest file: {file_path} to {output_dir_path}.")
                result_file_path = str(os.path.join(output_dir_path, file))
                if os.path.exists(result_file_path):
                    is_apex_manifest_file_found = True
                    logging.info(f"APEX manifest file found: {result_file_path}")
                break
    return is_apex_manifest_file_found, result_file_path

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
    signing_key = "platform"
    for key, shared_uid_list in SHARED_USER_ID_MAPPING_DICT.items():
        for shared_uid in shared_uid_list:
            if search_string_in_apk(apk_file, shared_uid):
                signing_key = key
                break
    return signing_key


def resign_apex_apk_files(apex_extract_dir_path):
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
                signing_key_path = get_signing_key_path(apk_file_path, signing_key)
                sign_apk_file(apk_file_path, signing_key_path)


def copy_android_prebuilt_jar(aosp_path, apex_root_path):
    prebuilt_folder = "prebuilts/sdk/current/public/"
    jar_name = "android.jar"
    android_jar_file_path = os.path.join(aosp_path, prebuilt_folder, jar_name)
    extract_android_jar_file_path = os.path.join(apex_root_path, prebuilt_folder)
    os.makedirs(extract_android_jar_file_path, exist_ok=True)
    shutil.copy(android_jar_file_path, extract_android_jar_file_path)


def generate_apex_keys(private_key_path, public_key_path):
    private_key_command = [
        'openssl', 'genpkey', '-algorithm', 'RSA', '-out', private_key_path, '-pkeyopt', 'rsa_keygen_bits:4096'
    ]
    with open(os.devnull, 'w') as devnull:
        subprocess.run(private_key_command, check=True, stdout=devnull, stderr=devnull)
    logging.info(f"Private key generated at: {private_key_path}")

    # Generate the public key from the private key
    public_key_command = [
        'openssl', 'rsa', '-pubout', '-in', private_key_path, '-out', public_key_path
    ]
    with open(os.devnull, 'w') as devnull:
        subprocess.run(public_key_command, check=True, stdout=devnull, stderr=devnull)
    logging.info(f"Public key generated at: {public_key_path}")


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
        logging.info(f"AVB public key replaced in APEX: {apex_file_path}")
    else:
        log_message = f"AVB public key replacement failed. {log_message}"
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
        shutil.copy(avb_pub_key_path, apex_pub_file_path)
    return is_success, log_message
