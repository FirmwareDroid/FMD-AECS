import hashlib
import json
import logging
import os.path
import re
import shutil
import subprocess
import tempfile
import traceback
import zipfile
from asyncore import write

from jinja2 import Environment, FileSystemLoader
from ConfigManager import ConfigManager
from aosp_post_build_app_injector import get_signing_key_path, sign_apk_file, verify_apk_file, \
    sign_apex_container_apksigner, sign_apex_container_signapk
from common import extract_vendor_name, remove_vendor_name_from_filename, check_shared_object_architecture, \
    get_path_up_to_first_term
#from conv_apex_manifest import convert_manifest_from_json
from parse_lddtree_to_json import run_lddtree
from shell_command import execute_shell_command
from config_post_injector import *

POST_INJECTOR_CONFIG = {}

def handle_apex_modules(file_path, aosp_path, lunch_target, target_out_path, aosp_version):
    """
    Merges two APEX files into one. Overwrites the vendor's APEX for later injection.
    """
    global POST_INJECTOR_CONFIG
    POST_INJECTOR_CONFIG = ConfigManager.get_config("POST_INJECTOR_CONFIG")
    if not POST_INJECTOR_CONFIG:
        raise Exception("No POST_INJECTOR_CONFIG found")
    logging.info(f"Handling APEX merge modules: {file_path} | {aosp_path} | {lunch_target} | {target_out_path}")
    is_merge_success = False
    log_message = ""
    apex_out_file, org_apex_file = backup_original_apex_file(file_path)

    try:
        apex_emulator_folder = find_emulator_apex_folder(target_out_path, file_path)
        if apex_emulator_folder and os.path.exists(apex_emulator_folder):
            logging.info(f"Emulator APEX folder found for: {file_path} and {apex_emulator_folder}")
            is_merge_success, log_message = merge_apex_files(apex_emulator_folder, file_path, apex_out_file, lunch_target, aosp_path, target_out_path, aosp_version)
            if os.path.exists(apex_out_file):
                try:
                    replace_org_apex_file(file_path, apex_out_file)
                    is_merge_success = True
                except Exception as err:
                    logging.error(f"Error replacing APEX file: {err}")
            else:
                logging.warning(f"APEX output file does not exist. Restoring original APEX: {org_apex_file}")
                is_merge_success = False
                shutil.copyfile(org_apex_file, file_path)
            logging.info(
                f"Merging APEX file complete: {apex_out_file} overwrites {file_path} | merge success: {is_merge_success} | {log_message}")
        else:
            log_message = f"Error merging APEX file: {file_path}. No emulator folder found in: {target_out_path}"
            logging.error(log_message)
    except Exception as e:
        log_message = f"Unexpected error while handling APEX modules: {e}"
        logging.error(log_message)

    return is_merge_success, log_message

def backup_original_apex_file(file_path):
    org_apex_file = f"{file_path}.original_apex"
    if os.path.exists(org_apex_file):
        logging.info(f"Original APEX file found - restoring: {org_apex_file}")
        restore_original_apex(file_path, org_apex_file)
    else:
        shutil.copyfile(file_path, org_apex_file)
        logging.info(f"Original APEX file not found, creating new one: {org_apex_file}")

    apex_out_file = prepare_apex_out_file(file_path)
    if os.path.exists(apex_out_file):
        os.remove(apex_out_file)
    return apex_out_file, org_apex_file

def replace_org_apex_file(file_path, apex_out_file):
    os.remove(file_path)
    shutil.copyfile(apex_out_file, file_path)
    os.remove(apex_out_file)
    logging.info(f"Replaced original APEX with new APEX file: org: {file_path} overwrite by: {apex_out_file}")


def rename_file(file_path, new_name):
    """
    Renames a file based on its file path.

    :param file_path: str - The full path to the file.
    :param new_name: str - The new name for the file (without the directory path).
    """
    try:
        directory = os.path.dirname(file_path)
        new_file_path = os.path.join(directory, new_name)
        os.rename(file_path, new_file_path)
        return new_file_path
    except Exception as e:
        logging.error(f"Error renaming file {file_path} to {new_name}: {e}")
        raise


def prepare_capex(file_path, output_dir, output_filename):
    """
    Unzips the capex file into a temporary directory, then copies the apex file to the output directory.
    """
    logging.info(f"Unzipping capex file: {file_path}")
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            apex_file = os.path.join(temp_dir, "original_apex")
            if os.path.exists(apex_file):
                logging.info(f"APEX file extracted: {apex_file}")
                out_file = str(os.path.join(output_dir, output_filename))
                shutil.copy(apex_file, out_file)
                return out_file
            else:
                logging.error(f"APEX file not found in capex: {file_path}")
    except zipfile.BadZipFile as e:
        logging.error(f"Error unzipping capex file {file_path}: {e}")
    except Exception as e:
        logging.error(f"Unexpected error while preparing capex: {e}")
    return None


def repackage_apex_file(aosp_path, apex_file_path, lunch_target, aosp_version):
    """
    Extracts the APEX file using deapexer, repackages it using apexer, and signs all the APK files in the APEX using apksigner.

    :param aosp_path: str - path to the AOSP source code.
    :param apex_file_path: str - path to the APEX file.
    :param lunch_target: str - lunch target for the AOSP build.

    :return: tuple - (bool, str) - True if the repackage was successful, False otherwise. String containing the log.

    """
    global POST_INJECTOR_CONFIG
    POST_INJECTOR_CONFIG = ConfigManager.get_config("POST_INJECTOR_CONFIG")

    filename = str(os.path.basename(apex_file_path)).replace(".apex", "").replace(".capex", "")
    logging.info(f"Repackaging APEX file: {apex_file_path}")
    is_success = False

    apex_out_file, org_apex_file = backup_original_apex_file(apex_file_path)
    try:
        apex_root_path = tempfile.mkdtemp(suffix=f"_{filename}_apex_repack")
        apex_extract_dir_path = tempfile.mkdtemp(dir=apex_root_path, suffix=f"_{filename}_extract")
        extract_success, log_message = extract_apex_file(aosp_path, apex_file_path, apex_extract_dir_path, lunch_target, aosp_version)
        if extract_success:
            logging.info(f"APEX extracted: {apex_file_path} to {apex_extract_dir_path}")
            with tempfile.NamedTemporaryFile(delete=False, dir=apex_root_path) as canned_fs_config:
                generate_canned_fs_config(apex_extract_dir_path, canned_fs_config.name, allow_filtering=False)
            logging.info(f"Canned FS config file: {canned_fs_config.name}")
            apex_file_name = str(os.path.basename(apex_file_path))
            is_manifest_found, apex_manifest_path = move_apex_manifest_file(apex_extract_dir_path, apex_root_path, apex_file_name, aosp_path, lunch_target)
            logging.info(f"APEX manifest: {apex_manifest_path}|{is_manifest_found}")

            if apex_manifest_path and os.path.exists(apex_manifest_path):
                is_success, log_message, avb_pub_key_path, priv_pem_file_path, private_key_path, cert_apex_apk_path = create_and_sign_apex_repack_container(
                        apex_manifest_path=apex_manifest_path,
                        apex_extract_dir_path=apex_extract_dir_path,
                        apex_root_path=apex_root_path,
                        aosp_path=aosp_path,
                        apex_out_file=apex_out_file,
                        lunch_target=lunch_target,
                        canned_fs_config=canned_fs_config,
                        apex_file_path=apex_file_path,
                        file_contexts_path=None,
                        aosp_version=aosp_version
                    )
            else:
                log_message = f"APEX manifest file not found after extraction: {apex_extract_dir_path} | apex_manifest_path: {apex_manifest_path}"
        else:
            log_message = f"APEX extraction failed. {apex_file_path} | {log_message}"
    except Exception as e:
        log_message = f"Error repackaging APEX file: {apex_file_path} | {str(e)}"
    return is_success, log_message


def create_and_sign_apex_repack_container(apex_manifest_path,
                                            apex_extract_dir_path,
                                            apex_root_path,
                                            aosp_path,
                                            apex_out_file,
                                            lunch_target,
                                            canned_fs_config,
                                            apex_file_path=None,
                                            file_contexts_path=None,
                                            aosp_version=None):
    copy_android_prebuilt_jar(aosp_path, apex_root_path)
    is_success, log_message, avb_pub_key_path, priv_pem_file_path, private_key_path, cert_apex_apk_path \
        = create_apex_container(apex_manifest_path=apex_manifest_path,
                                apex_extract_dir_path=apex_extract_dir_path,
                                apex_root_path=apex_root_path,
                                aosp_path=aosp_path,
                                output_file_path=apex_out_file,
                                lunch_target=lunch_target,
                                canned_fs_config=canned_fs_config,
                                is_repack=True,
                                file_contexts_path=file_contexts_path,
                                aosp_version=aosp_version)
    if is_success:
        is_success, error_message = sign_apex_file(apex_out_file,
                                                   aosp_path,
                                                   private_key_path,
                                                   cert_apex_apk_path,
                                                   lunch_target)
        if is_success:
            log_message = f"APEX signing success: {apex_out_file}"
            if apex_file_path is not None:
                replace_org_apex_file(apex_file_path, apex_out_file)
        else:
            log_message = f"APEX signing failed: {apex_out_file} | {error_message}"
    else:
        log_message = f"APEX repack creation failed. {apex_out_file} | {log_message}"
    return is_success, log_message, avb_pub_key_path, priv_pem_file_path, private_key_path, cert_apex_apk_path

def find_lib64_folders(root_dir, folder_name="lib64", include_subfolders=True):
    """
    Recursively finds all folders named 'lib64' under root_dir,
    and adds all their subfolders to the result list as well.
    """
    lib64_paths = []

    for dirpath, dirnames, _ in os.walk(root_dir):
        if folder_name in dirnames:
            lib64_dir = os.path.join(dirpath, folder_name)
            if "com_android_vndk_current_apex" not in lib64_dir:
                lib64_paths.append(lib64_dir)
                # Add all subfolders of lib64
                if include_subfolders:
                    for subdir_root, subdir_names, _ in os.walk(lib64_dir):
                        for subdir in subdir_names:
                            subfolder_path = os.path.join(subdir_root, subdir)
                            lib64_paths.append(subfolder_path)
            else:
                logging.info(f"Skipped vndk lib64 folder: {lib64_dir}")
    return lib64_paths


def add_new_apex_file(aosp_path, binary_file_path, lunch_target, partition_name, aosp_version):
    """
    Creates a new APEX file with the given binary file path. Collects all necessary native libraries for the binary
    by using lddtree, and adds them into the APEX file. The native libraries are searched within the source tree of the
    vendor firmware, and copied into the APEX file.
    """
    global POST_INJECTOR_CONFIG
    POST_INJECTOR_CONFIG = ConfigManager.get_config("POST_INJECTOR_CONFIG")
    if not POST_INJECTOR_CONFIG:
        raise Exception("No POST_INJECTOR_CONFIG found")

    logging.info(f"Adding new APEX file. Target Binary: {binary_file_path} | {aosp_path} | {lunch_target}")
    filename = str(os.path.basename(binary_file_path))
    apex_file_name = f"com.android.fmd.{filename}.apex"

    # Copy the template APEX file to a temporary location
    template_folder_abs_path = os.path.join(ROOT_PATH, TEMPLATE_FOLDER, "apex")
    apex_template_file = os.path.join(template_folder_abs_path, "com.android.fmd.apex")
    tempdir = tempfile.TemporaryDirectory()
    apex_in_file = str(os.path.join(tempdir.name, apex_file_name))
    try:
        shutil.copyfile(apex_template_file, apex_in_file)
        logging.info(f"Copied APEX template file: {apex_template_file} to {apex_in_file}")
    except Exception as e:
        logging.error(f"Error copying APEX template file: {e}")
        return False, f"Error copying APEX template file: {e}"

    # Extract the APEX file to a temporary directory
    apex_root_path = tempfile.mkdtemp(suffix=f"_{filename}_apex_repack")
    apex_extract_dir_path = tempfile.mkdtemp(dir=apex_root_path, suffix=f"_{filename}_extract")
    extract_success, log_message = extract_apex_file(aosp_path, apex_in_file, apex_extract_dir_path, lunch_target, aosp_version)
    if os.path.exists(apex_in_file):
        logging.info(f"APEX file {apex_in_file} still exists after extraction. Removing it.")
        os.remove(apex_in_file)

    if not extract_success:
        logging.error(f"Error extracting APEX file: {log_message}")
        return False, log_message

    # Inject the binary file into the APEX in the extract temporary directory
    bin_dir_path =  os.path.join(apex_extract_dir_path, "bin")
    os.makedirs(bin_dir_path, exist_ok=True)
    dst_file_path = os.path.join(bin_dir_path, filename)
    try:
        shutil.copyfile(binary_file_path, dst_file_path)
        os.chmod(dst_file_path, 0o700)
        logging.info(f"Copied binary file {binary_file_path} to APEX: {dst_file_path}")
    except Exception as e:
        logging.error(f"Error copying binary file {binary_file_path} to APEX {apex_file_name}: {e}")
        return False, f"Error copying binary file: {e}"

    # Run lddtree on the binary file to collect all necessary native libraries
    partition_root = get_path_up_to_first_term(binary_file_path, partition_name)
    logging.info(f"Partition root {apex_file_name}: {partition_root}")
    if not os.path.exists(partition_root):
        logging.error(f"Partition root not found: {partition_root}. Cannot proceed with APEX creation for {apex_file_name}.")
        return False, f"Partition root not found: {partition_root}"

    ## Construct LD_LIBRARY_PATH for lddtree
    lib64_path_list = find_lib64_folders(partition_root)
    extra_paths = []
    if lib64_path_list:
        extra_paths.extend(lib64_path_list)
        logging.info(f"Extra paths for lddtree: {extra_paths} for APEX {apex_file_name}")
    else:
        logging.warning(f"No 'lib64' folders found in partition root: {partition_root}. Using default paths. {apex_file_name}")

    env = {"LD_LIBRARY_PATH": ":".join(extra_paths)} if extra_paths else None

    try:
        libs, libs_not_found = run_lddtree(binary_file_path, extra_env=env)
        logging.info(f"Collected libraries from lddtree - {apex_file_name} libs found: {libs}")
        logging.info(f"Collected libraries from lddtree - {apex_file_name} libs_not_found: {libs_not_found}")

    except Exception as e:
        logging.error(f"Error running lddtree on {binary_file_path} - {apex_file_name}: {e}")
        return False, f"Error running lddtree: {e}"

    apex_lib64_path = os.path.join(apex_extract_dir_path, "lib64")
    os.makedirs(apex_lib64_path, exist_ok=True)

    # Manually added libaries
    libs_not_found.append("heapprofd_client_api.so")
    libs_not_found.append("libandroid.so")
    libs_not_found.append("libartpalette-system.so")

    # Exclude specific libraries from being copied
    exclude_list = ["libc.so"] # "libandroid.so"
    exclude_keyword = ["bionic"]
    # Copy libraries
    for lib_path in libs:
        lib_name = os.path.basename(lib_path)
        if lib_name in exclude_list or any(keyword in lib_path for keyword in exclude_keyword):
            logging.info(f"Skipping excluded library {lib_name} for APEX {apex_file_name}")
            continue
        logging.info(f"Adding APEX lib path - {apex_file_name}: {lib_path}")
        dst_lib_path = os.path.join(apex_lib64_path, os.path.basename(lib_path))
        try:
            logging.info(f"Copying library {lib_path} to {dst_lib_path} : APEX {apex_file_name}")
            shutil.copyfile(lib_path, dst_lib_path)
        except Exception as e:
            logging.error(f"Error copying library {lib_path} to APEX {apex_file_name}: {e}")
            return False, f"Error copying library {lib_path}: {e}"

    # Search for the native libraries in the AOSP source tree and copy them to the APEX directory if found
    for lib_name in libs_not_found:
        if lib_name in exclude_list or any(keyword in lib_name for keyword in exclude_keyword):
            logging.info(f"Skipping excluded library {lib_name} for APEX {apex_file_name}")
            continue
        logging.info(f"Searching for library {lib_name} in partition root: {partition_root} for APEX {apex_file_name}")
        for root, dirs, files in os.walk(partition_root):
            if lib_name in files:
                src_lib_path = os.path.join(root, lib_name)
                if "com_android_vndk_current_apex" in src_lib_path:
                    logging.info(f"Skipping VNDK library {lib_name} in {src_lib_path} for APEX {apex_file_name}")
                    continue
                if any(keyword in src_lib_path for keyword in exclude_keyword):
                    logging.info(f"Skipping excluded library {lib_name} for APEX {apex_file_name}")
                    continue
                if os.path.exists(src_lib_path):
                    if check_shared_object_architecture(src_lib_path) == "64-bit":
                        dst_lib_path = os.path.join(apex_lib64_path, lib_name)
                        try:
                            shutil.copyfile(src_lib_path, dst_lib_path)
                            logging.info(f"Copied 64-bit library {lib_name} from {src_lib_path} to {dst_lib_path}: APEX {apex_file_name}")
                            break  # Stop searching after finding the 64-bit version
                        except Exception as e:
                            logging.error(f"Error copying library {lib_name} from {src_lib_path} to {dst_lib_path} for {apex_file_name}: {e}")
                            return False, f"Error copying library {lib_name}: {e}"
                    else:
                        logging.info(f"Found 32-bit library {lib_name} in {src_lib_path}, skipping. {apex_file_name}")
                else:
                    logging.error(f"Library {lib_name} not found in {partition_root}. Skipping. {apex_file_name}")

    add_all_lib64_libraries = True
    if add_all_lib64_libraries:
        for lib64_path in lib64_path_list:
            if not "apex" in lib64_path:
                for root, dirs, files in os.walk(lib64_path):
                    for file in files:
                        if file in exclude_list:
                            logging.info(f"Skipping excluded library {file} for APEX {apex_file_name}")
                            continue
                        if file.endswith(".so"):
                            src_lib_path = os.path.join(root, file)
                            pre_path = get_path_up_to_first_term(root, "lib64")
                            post_path = str(src_lib_path.replace(pre_path, ""))
                            logging.info(f"Pre-path: {pre_path}, Post-path: {post_path} for "
                                         f"lib64 {file} in APEX {apex_file_name}, src_lib_path: {src_lib_path}")
                            dst_lib_path = os.path.join(apex_extract_dir_path, "lib64", post_path)
                            if check_shared_object_architecture(src_lib_path) == "64-bit":
                                if os.path.exists(dst_lib_path):
                                    logging.info(
                                        f"Library {src_lib_path} already exists in APEX {apex_file_name}, skipping copy.")
                                    continue
                                try:
                                    os.makedirs(os.path.dirname(dst_lib_path), exist_ok=True)
                                    shutil.copyfile(src_lib_path, dst_lib_path)
                                    logging.info(f"Copied library {src_lib_path} to {dst_lib_path}: APEX {apex_file_name}")
                                except Exception as e:
                                    logging.error(f"Error copying library {src_lib_path} to APEX {apex_file_name}: {e}")
                                    return False, f"Error copying library {src_lib_path}: {e}"

    add_all_apex_libraries = True
    if add_all_apex_libraries:
        for lib64_path in lib64_path_list:
            if not "vndk" in lib64_path: # "apex" in lib64_path and
                if "apex" in lib64_path and ("adbd" in lib64_path or "art" in lib64_path or "runtime" in lib64_path):
                    logging.info(f"Copying all libraries from {lib64_path} to APEX {apex_file_name}")
                    for root, dirs, files in os.walk(lib64_path):
                        for file in files:
                            if file in exclude_list:
                                logging.info(f"Skipping excluded library {file} for APEX {apex_file_name}")
                                continue
                            if file.endswith(".so"):
                                src_lib_path = os.path.join(root, file)
                                pre_path = get_path_up_to_first_term(root, "lib64")
                                post_path = str(src_lib_path.replace(pre_path, ""))
                                logging.info(f"Pre-path: {pre_path}, Post-path: {post_path} for "
                                             f"lib64 {file} in APEX {apex_file_name}, src_lib_path: {src_lib_path}")
                                dst_lib_path = os.path.join(apex_extract_dir_path, "lib64", post_path)
                                if check_shared_object_architecture(src_lib_path) == "64-bit":
                                    if os.path.exists(dst_lib_path):
                                        logging.info(f"Library {src_lib_path} already exists in APEX {apex_file_name}, skipping copy.")
                                        continue
                                    try:
                                        os.makedirs(os.path.dirname(dst_lib_path), exist_ok=True)
                                        shutil.copyfile(src_lib_path, dst_lib_path)
                                        logging.info(f"Copied APEX library {src_lib_path} to {dst_lib_path}: APEX {apex_file_name}")
                                    except Exception as e:
                                        logging.error(f"Error copying library {src_lib_path} to APEX {apex_file_name}: {e}")
                                        return False, f"Error copying library {src_lib_path}: {e}"

    javalib_folder_list = find_lib64_folders(partition_root, "javalib")

    logging.info(f"Javalib folders found: {javalib_folder_list} for APEX {apex_file_name}")
    add_javalibs = False
    if add_javalibs:
        for javalib_path in javalib_folder_list:
            if not "vndk" in javalib_path and "apex" in javalib_path and "art" in javalib_path:
                logging.info(f"Copying all javalib libraries from {javalib_path} to APEX {apex_file_name}")
                for root, dirs, files in os.walk(javalib_path):
                    for file in files:
                        if file.endswith(".fmd-aecs-lock") or file.endswith(".fmd-aecs-processed"):
                            continue
                        src_lib_path = os.path.join(root, file)
                        pre_path = get_path_up_to_first_term(root, "javalib")
                        post_path = str(src_lib_path.replace(pre_path, ""))
                        logging.info(f"Pre-path: {pre_path}, Post-path: {post_path} for javalib {file} in APEX {apex_file_name}, src_lib_path: {src_lib_path}")
                        dst_lib_path = os.path.join(apex_extract_dir_path, "javalib", post_path)
                        if os.path.exists(dst_lib_path):
                            logging.info(f"Javalib {src_lib_path} already exists in APEX {apex_file_name}, skipping copy.")
                            continue
                        try:
                            os.makedirs(os.path.dirname(dst_lib_path), exist_ok=True)
                            shutil.copyfile(src_lib_path, dst_lib_path)
                            logging.info(f"Copied javalib {src_lib_path} to {dst_lib_path}: APEX {apex_file_name}")
                        except Exception as e:
                            logging.error(f"Error copying javalib {src_lib_path} to APEX {apex_file_name}: {e}")
                            return False, f"Error copying javalib {src_lib_path}: {e}"



    # Create the new APEX file
    ## Create Canned FS config file
    with tempfile.NamedTemporaryFile(delete=False, dir=apex_root_path) as canned_fs_config:
        generate_canned_fs_config(apex_extract_dir_path, canned_fs_config.name, allow_filtering=False)

    ## Create APEX Manifest file
    apex_manifest_name = "apex_manifest.json"
    apex_manifest_path = os.path.join(apex_root_path, apex_manifest_name)
    apex_file_name = f"com.android.fmd.{filename}.apex"
    apex_version = 1
    apex_manifest = f"{{\n\t\"name\": \"{apex_file_name}\",\n\t\"version\": {apex_version}\n}}\n"
    with open(apex_manifest_path, "w") as apex_manifest_file:
        apex_manifest_file.write(apex_manifest)
    logging.info(f"Created {apex_manifest_path} with content: {apex_manifest} for APEX file {apex_file_name}")

    # Remove the old manifest file if it exists
    is_manifest_found, old_apex_manifest_path = move_apex_manifest_file(apex_extract_dir_path, apex_root_path, apex_file_name, aosp_path, lunch_target)
    logging.info(f"APEX manifest: {apex_manifest_path}|{is_manifest_found}")
    if os.path.exists(old_apex_manifest_path):
        logging.info(f"Removing existing APEX manifest Protobuf file: {old_apex_manifest_path}")
        os.remove(old_apex_manifest_path)

    #Create new Manifest file
    apex_manifest_name_pb = "apex_manifest.pb"
    apex_manifest_path_pb = os.path.join(apex_root_path, apex_manifest_name_pb)
    logging.info(f"Converting APEX manifest from JSON to Protobuf format: {apex_manifest_path} to {apex_manifest_path_pb}")
    convert_manifest_from_json(apex_manifest_path=apex_manifest_path, out_file_path=apex_manifest_path_pb, aosp_path=aosp_path, lunch_target=lunch_target)
    if not os.path.exists(apex_manifest_path_pb):
        logging.error(f"APEX manifest Protobuf file not created: {apex_manifest_path_pb} for APEX file {apex_file_name}")
        return False, f"APEX manifest Protobuf file not created: {apex_manifest_path_pb}"
    else:
        logging.info(f"APEX manifest Protobuf file created: {apex_manifest_path_pb} for APEX file {apex_file_name}. "
                     f"Removing old manifest file: {old_apex_manifest_path}")
        os.remove(apex_manifest_path)

    try:
        ## Create SELinux File Contexts for the new APEX file
        file_context_source_path = os.path.join(template_folder_abs_path, "file_contexts")
        file_context_dst_path = os.path.join(aosp_path, "system", "sepolicy", "apex", f"com.android.fmd.{filename}-file_contexts")
        logging.info(f"Copying {file_context_source_path} to {file_context_dst_path} for APEX file {apex_file_name}")
        shutil.copyfile(file_context_source_path, file_context_dst_path)
        with open(file_context_dst_path, 'a') as file_contexts_file:
            # TODO Add SELinux context for the binary file dynamically
            file_contexts_file.write(f"\n/bin/{filename}    u:object_r:zygote_exec:s0\n")
    except Exception as e:
        logging.error(f"Error copying SELinux file contexts: {e}")
        return False, f"Error copying SELinux file contexts: {e}"

    file_contexts_path = os.path.join(aosp_path, "system", "sepolicy", "apex", f"com.android.fmd.{filename}-file_contexts")

    if aosp_version == "13":
        apex_out_file = os.path.join(aosp_path, "out", "target", "product", "emulator64_arm64", partition_name, "apex",
                                     apex_file_name)
    elif aosp_version == "14":
        apex_out_file = os.path.join(aosp_path, "out", "target", "product", "emu64a", partition_name, "apex",
                                     apex_file_name)
    else:
        apex_out_file = os.path.join(aosp_path, "out", "target", "product", "emulator_arm64", partition_name, "apex",
                                     apex_file_name)



    is_success, log_message, avb_pub_key_path, priv_pem_file_path, private_key_path, cert_apex_apk_path \
        = create_and_sign_apex_repack_container(apex_manifest_path=apex_manifest_path_pb,
                                                apex_extract_dir_path=apex_extract_dir_path,
                                                apex_root_path=apex_root_path,
                                                aosp_path=aosp_path,
                                                apex_out_file=apex_out_file,
                                                lunch_target=lunch_target,
                                                canned_fs_config=canned_fs_config,
                                                file_contexts_path=file_contexts_path,
                                                aosp_version=aosp_version,
                                                apex_file_path=None)
    # Copy the APEX file to the injection source directory for later direct injection
    if not is_success:
        logging.error(f"Error creating APEX container file {apex_file_name}: {log_message}")
        return False, log_message

    logging.info(f"APEX container file {apex_file_name} successfully created to: {apex_out_file}")

    return is_success, log_message


def clean_json_file(input_path, output_path):
    with open(input_path, 'r') as f:
        lines = f.readlines()
    cleaned_lines = [
        line for line in lines
        if "Placeholder module version to be replaced during build." not in line
        and "Do not change!" not in line
    ]
    for i, line in enumerate(cleaned_lines):
        if "\"version\": 0" in line:
            cleaned_lines[i] = line.replace("0", "999")

    with open(output_path, 'w') as f:
        f.writelines(cleaned_lines)
    with open(output_path, 'r') as f:
        json.load(f)

def convert_manifest_from_json(apex_manifest_path, out_file_path, aosp_path, lunch_target):
    """
    Executes the binary "conv_apex_manifest" to convert an apex_manifest.json file to
    an apex_manifest.pb Protobuf file.

    usage: conv_apex_manifest [-h] {strip,proto,setprop,print} ...

    positional arguments:
      {strip,proto,setprop,print}
        strip               remove unknown keys from APEX manifest (JSON)
        proto               write protobuf binary format
        setprop             change property value
        print               print APEX manifest

    options:
      -h, --help            show this help message and exit
    """
    conv_bin_candidates = [
        os.path.join(aosp_path, "out/soong/host/linux-x86/bin/conv_apex_manifest"),
        os.path.join(aosp_path, "out/host/linux-x86/bin/conv_apex_manifest"),
    ]
    converter_path = next((p for p in conv_bin_candidates if os.path.exists(p)), None)
    if not converter_path:
        message = "APEX conv_apex_manifest failed: conv_apex_manifest tool not found in any known location."
        logging.info(message)
        return False, {f"{message}"}

    base_dir = os.path.dirname(apex_manifest_path)
    cleaned_manifest = os.path.join(base_dir, "apex_manifest_cleaned.json")
    clean_json_file(apex_manifest_path, cleaned_manifest)

    info = f"APEX: conv_apex_manifest tool path: {converter_path}|{cleaned_manifest}|{out_file_path}|{lunch_target}"
    logging.info(info)
    command = f"bash -c 'cd {aosp_path} && source {aosp_path}build/envsetup.sh && lunch {lunch_target} " \
               f"&& {converter_path} proto -o {out_file_path} {cleaned_manifest}'"
    is_success, log = execute_shell_command(command, aosp_path)
    if not is_success:
        logging.error(f"APEX: conv_apex_manifest conversion command failed. Trying again: {command} | {is_success} | {log}")
        is_success, log = execute_shell_command(command, aosp_path)
    logging.info(f"APEX: conv_apex_manifest extraction command: {command} | {is_success} | {log}")
    return is_success, {f"ERROR: {log}| More infos: {info}"}



def create_apex_build_module(aosp_path, apex_file_path, avb_pub_key_path, priv_pem_file_path, private_key_path, cert_apex_apk_path):
    key_id = str(os.path.basename(priv_pem_file_path).replace(".pem", ""))
    module_out_folder_path = str(os.path.join(aosp_path, MODULE_BASE_INJECT_DIR, key_id.replace(".", "_")))
    is_success, log_message = inject_apex_keys_module(apex_file_path, module_out_folder_path, key_id)
    if is_success:
        key_path_list = [avb_pub_key_path, priv_pem_file_path, private_key_path, cert_apex_apk_path]
        for key_path in key_path_list:
            shutil.copy(key_path, module_out_folder_path)
            if not os.path.exists(key_path):
                logging.error(f"Key file not copied: {key_path} to {module_out_folder_path}")
                is_success = False
            else:
                logging.info(f"APEX Key file copied: {key_path} to {module_out_folder_path}")
    else:
        log_message = f"Error injecting Android.bp file: {module_out_folder_path}"


def create_apex_manifest(output_dir, apex_name):
    """
    Creates an apex_manifest.json file with default values.
    """
    manifest_content = f"{{\"name\": \"{apex_name}\",\n\"version\": 1}}"
    manifest_path = str(os.path.join(output_dir, "apex_manifest.json"))
    with open(manifest_path, "w") as manifest_file:
        manifest_file.write(manifest_content)


def inject_apex_keys_module(input_apex, out_folder_path, key_id):
    logging.info(f"Add Android.bp file for APEX: {input_apex}")
    os.makedirs(out_folder_path, exist_ok=True)
    android_bp_file = os.path.join(out_folder_path, "Android.bp")
    logging.info(f"Creating Android.bp file for APEX: {android_bp_file}")
    log_message = ""
    with open(android_bp_file, 'w') as android_bp:
        content = f'\n\napex_key {{\n    name: \"{key_id}.key\",\n    public_key: \"{key_id}.avbpubkey\",\n    private_key: \"{key_id}.pem\", \n    installable: true\n}}'
        content += f"\n\nandroid_app_certificate {{\n    name: \"{key_id}.certificate\",\n    certificate: \"{key_id}\"\n}}"
        android_bp.write(content)
        is_success = True
    if not os.path.exists(android_bp_file):
        log_message = f"Error creating Android.bp APEX module: {android_bp_file}"
        logging.error(log_message)
        is_success = False
    return is_success, log_message

def copy_keys_to_apex_folder(input_apex, apex_main_folder, avb_pub_key_path):
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
    logging.info(f"APEX build intermediate folder to look for: {apex_folder_path}")
    if os.path.exists(apex_folder_path):
        apex_folder = apex_folder_path
    else:
        raise ValueError(f"APEX build intermediate folder not found: {apex_folder_path}")
    logging.info(f"APEX build intermediate folder: {apex_folder}")
    return apex_folder



def get_match_existing_emulator_folders(filename_no_vendor):
    if "tzdata" in filename_no_vendor:
        filename_no_vendor = re.sub(r'tzdata\d+', 'tzdata', filename_no_vendor)

    for key in POST_INJECTOR_CONFIG["APEX_DEFAULT_EMULATOR_PATHS_DICT"]:
        if key in filename_no_vendor:
            if key == "media" and ("mediaprovider" in filename_no_vendor or "swcodec" in filename_no_vendor):
                continue
            logging.info(f"Found APEX default path for {filename_no_vendor}: {POST_INJECTOR_CONFIG['APEX_DEFAULT_EMULATOR_PATHS_DICT'][key]}")
            return POST_INJECTOR_CONFIG["APEX_DEFAULT_EMULATOR_PATHS_DICT"][key]
    return None


def find_emulator_apex_folder(target_out_path, file_path):
    filename = str(os.path.basename(file_path))
    filename_no_vendor = remove_vendor_name_from_filename(filename)
    filename_no_vendor = filename_no_vendor.replace(".apex", "").replace(".capex", "")

    apex_emulator_folder_root = get_apex_build_intermediate_folder(target_out_path)
    logging.info(f"Searching for APEX module folder: {filename_no_vendor} in {apex_emulator_folder_root} for apex file {file_path}")
    apex_emulator_folder_name = get_match_existing_emulator_folders(filename_no_vendor)
    apex_module_folder = os.path.join(apex_emulator_folder_root, apex_emulator_folder_name)
    logging.info(f"Test if APEX module folder exists: {apex_module_folder}")
    if os.path.exists(apex_module_folder):
        logging.info(f"APEX module folder found: {apex_module_folder} for apex {file_path}")
    else:
        apex_module_folder = None
        logging.warning(f"APEX module folder not found: {filename_no_vendor} for apex {file_path}")
    return apex_module_folder


def get_last_two_as_int(input_string):
    try:
        return int(input_string[-2:])
    except (ValueError, IndexError):
        return 0

def get_vndk_version(file_path):
    """
    Extracts the VNDK version from a binary file by identifying readable strings and searching for 'vndk'.
    :param file_path: str - Path to the binary file.
    :return: str - The VNDK version if found, otherwise None.
    """
    version = 0
    try:
        with open(file_path, 'rb') as file:
            data = file.read()
            strings = re.findall(rb'[ -~]{4,}', data)
            logging.debug(f"Found VNDK strings: {strings}")
            for string in strings:
                if b'com.android.vndk.v' in string.lower():
                    version_string = string.decode('utf-8')
                    logging.debug(f"Found VNDK string: {version_string}")
                    version = get_last_two_as_int(version_string)
                    if version != 0:
                        logging.info(f"Extracted VNDK version: {version}")
                        return int(version)
    except Exception as e:
        logging.error(f"Error reading file {file_path}: {e}")
    return version

def allow_vndk_merge(apex_path, apex_filename):
    if "vndk" in apex_filename:
        vendor_vndk_version = get_vndk_version(apex_path)
        if vendor_vndk_version == 0:
            logging.error(f"APEX: Vendor VNDK version not found in {apex_path}. Cannot merge APEX files.")
            return False
        if POST_INJECTOR_CONFIG['EMULATOR_VNDK_VERSION'] > vendor_vndk_version:
            logging.info(f"APEX: Emulator VNDK version {POST_INJECTOR_CONFIG['EMULATOR_VNDK_VERSION']} "
                         f"is higher than vendor VNDK version {vendor_vndk_version}.")
            return False
    return True


def get_matching_apex_key(filename, config):
    """
    Returns the first key from config that matches a substring in the filename.
    """
    for key in config:
        if key in filename:
            return key
    return None


def load_apex_manifest_from_aosp(apex_emulator_folder, merged_apex_extract_dir_path, filename_input, aosp_path, apex_root_path, lunch_target):
    apex_manifest_path_pb = os.path.join(apex_emulator_folder, "apex_manifest.pb")
    logging.info(f"Checking for existing APEX manifest in emulator APEX: {apex_manifest_path_pb}")
    if os.path.exists(apex_manifest_path_pb):
        logging.info(f"Copy manifest from original APEX: {apex_manifest_path_pb}")
        shutil.copy2(apex_manifest_path_pb, merged_apex_extract_dir_path)
        if not os.path.exists(merged_apex_extract_dir_path):
            logging.error(f"ERROR: APEX Manifest was not copied to: {merged_apex_extract_dir_path}. EXIT PROGRAM!")
            traceback.print_stack()
            exit(-1)
    else:
        logging.info(f"Load Manifest from AOSP source tree for APEX: {filename_input}")
        apex_manifest_name_pb = "apex_manifest.pb"
        apex_manifest_path_pb = os.path.join(apex_root_path, apex_manifest_name_pb)
        apex_keyword = get_matching_apex_key(filename_input, POST_INJECTOR_CONFIG["APEX_DEFAULT_PATHS_DICT"])
        if not apex_keyword:
            logging.error(
                f"APEX: No matching keyword found in APEX_DEFAULT_PATHS_DICT for {filename_input}. EXIT PROGRAM!")
            traceback.print_stack()
            exit(-1)
        logging.info(f"APEX Keyword: {apex_keyword} found for apex: {filename_input}")
        apex_module_path = str(os.path.join(aosp_path, POST_INJECTOR_CONFIG["APEX_DEFAULT_PATHS_DICT"][apex_keyword]))

        candidate_file_names = ["apex_manifest.json", "manifest.json", "manifest-art.json"]
        apex_manifest_path = None
        for fname in candidate_file_names:
            candidate_path = os.path.join(apex_module_path, fname)
            if os.path.exists(candidate_path):
                apex_manifest_path = candidate_path
                break
        if apex_manifest_path is None:
            logging.error(
                f"APEX: No manifest file found in APEX module path: {apex_module_path}. EXIT PROGRAM!")
            traceback.print_stack()
            exit(-1)

        logging.info(f"APEX manifest path used: {apex_manifest_path}")
        if os.path.exists(apex_manifest_path):
            logging.info(
                f"Converting APEX manifest from JSON to Protobuf format: {apex_manifest_path} to {apex_manifest_path_pb}")
            convert_manifest_from_json(apex_manifest_path=apex_manifest_path, out_file_path=apex_manifest_path_pb, aosp_path=aosp_path, lunch_target=lunch_target)
            if not os.path.exists(apex_manifest_path_pb):
                logging.error(f"APEX manifest Protobuf file not created: {apex_manifest_path_pb}. EXIT PROGRAM!")
                traceback.print_stack()
                exit(-1)
            else:
                logging.info(f"APEX manifest Protobuf file created: {apex_manifest_path_pb}")
                shutil.copy2(apex_manifest_path_pb, merged_apex_extract_dir_path)
                logging.info(f"APEX manifest Protobuf file copied {apex_manifest_path_pb} to: {merged_apex_extract_dir_path}")
        else:
            logging.error("APEX Manifest path is invalid. EXIT PROGRAM!")
            traceback.print_stack()
            exit(-1)
    return apex_manifest_path_pb

# Keep the structure of the original apex
# Inject additional files into the apex
def merge_apex_files(apex_emulator_folder, input_apex, apex_out_file, lunch_target, aosp_path, target_out_path, aosp_version):
    """
    Merges the emulator APEX file with a vendor apex in case they have the same name.
    Keeps the structure of the emulator apex and injects additional files into the apex.
    Normal Mode: Replaces the original emulator apex file with the vendor one.
    If ALLOW_MIXED_APEX_FILES then two apex files are merged together.
    Writes the merged apex to the apex_out_file
    """
    filename_input = str(os.path.basename(input_apex))
    if POST_INJECTOR_CONFIG["CHECK_VNDK_VERSION_MISMATCH"]:
        if not allow_vndk_merge(input_apex, filename_input):
            return False, "APEX: Emulator VNDK version is higher than vendor VNDK version. Merging not allowed."

    logging.info(f"Merging APEX files: {apex_emulator_folder} and {input_apex}")
    is_success, log_message = False, None
    apex_root_path = tempfile.mkdtemp(suffix=f"_{filename_input}_merged")
    merged_apex_extract_dir_path = tempfile.mkdtemp(suffix=f"extract", dir=apex_root_path)
    apex_vendor_extract_dir_path = tempfile.mkdtemp(suffix=f"_{filename_input}_vendor")
    extract_success, log_message = extract_apex_file(aosp_path, input_apex, apex_vendor_extract_dir_path, lunch_target, aosp_version)
    if extract_success:
        if POST_INJECTOR_CONFIG["ALLOW_MIXED_APEX_FILES"] and any(keyword in filename_input for keyword in POST_INJECTOR_CONFIG["ALLOW_MIXED_APEX_KEYWORD_LIST"]):
            logging.info(f"APEX: CREATING MIXED APEX: {apex_emulator_folder} and vendor APEX: {input_apex}")
            shutil.copytree(apex_emulator_folder, merged_apex_extract_dir_path, dirs_exist_ok=True)
            logging.info(f"Copied emulator APEX folder: {apex_emulator_folder} to {merged_apex_extract_dir_path}")
            log_files_in_dir(merged_apex_extract_dir_path)
        else:
            load_apex_manifest_from_aosp(apex_emulator_folder,
                                         merged_apex_extract_dir_path,
                                         filename_input,
                                         aosp_path,
                                         apex_root_path,
                                         lunch_target)

        apk_name_list = []
        if POST_INJECTOR_CONFIG["INJECT_APEX_VENDOR_FILES"]:
            logging.info(f"Injecting APEX vendor files: {apex_vendor_extract_dir_path} into {merged_apex_extract_dir_path}")
            inject_apex_vendor_files(merged_apex_extract_dir_path, apex_vendor_extract_dir_path)
        else:
            logging.info("Injecting APEX vendor files is disabled.")

        if POST_INJECTOR_CONFIG["INJECT_APEX_VENDOR_APPS"]:
            logging.info(f"Injecting APEX vendor app: {apex_vendor_extract_dir_path} into {merged_apex_extract_dir_path}")
            apk_name_list = inject_apex_vendor_apps(merged_apex_extract_dir_path, apex_vendor_extract_dir_path)
        else:
            logging.info("Injecting APEX vendor apps is disabled.")

        with tempfile.NamedTemporaryFile(delete=False) as canned_fs_config:
            generate_canned_fs_config(merged_apex_extract_dir_path, canned_fs_config.name, apk_name_list)


        is_manifest_found, apex_manifest_path = move_apex_manifest_file(merged_apex_extract_dir_path,
                                                                        apex_root_path,
                                                                        filename_input,
                                                                        aosp_path,
                                                                        lunch_target)
        logging.info(f"APEX manifest: {apex_manifest_path}|{is_manifest_found}")
        if is_manifest_found and os.path.exists(apex_manifest_path):
            if apex_manifest_path:
                copy_android_prebuilt_jar(aosp_path, apex_root_path)
                logging.info(f"APEX manifest file found: {apex_manifest_path}...start container creation")
                is_success, log_message, avb_pub_key_path, priv_pem_file_path, private_key_path, cert_apex_apk_path = create_apex_container(apex_manifest_path=apex_manifest_path,
                                                                                  apex_extract_dir_path=merged_apex_extract_dir_path,
                                                                                  apex_root_path=apex_root_path,
                                                                                  aosp_path=aosp_path,
                                                                                  output_file_path=apex_out_file,
                                                                                  lunch_target=lunch_target,
                                                                                  is_repack=False,
                                                                                  canned_fs_config=canned_fs_config,
                                                                                  file_contexts_path=None,
                                                                                  aosp_version=aosp_version)
                if is_success:
                    is_success, error_message = sign_apex_file(apex_out_file,
                                                               aosp_path,
                                                               private_key_path,
                                                               cert_apex_apk_path,
                                                               lunch_target)
                    logging.info(f"Completed APEX merge successfully: {apex_out_file}")
                    if POST_INJECTOR_CONFIG["REPLACE_AVB_KEYS"]:
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


def inject_apex_vendor_apps(merged_apex_extract_dir_path, apex_vendor_extract_dir_path):
    files_coped_list = []
    for root, dirs, files in os.walk(apex_vendor_extract_dir_path):
        for file in files:
            file_path = os.path.join(root, file)
            file_extension = os.path.splitext(file)[1]
            if file_extension == ".apk" \
                    and not os.path.islink(file_path) \
                    and os.path.exists(file_path) \
                    and os.path.isfile(file_path):
                extract_dir = root.replace(apex_vendor_extract_dir_path,"").replace("//", "/")
                if not extract_dir.endswith("/"):
                    extract_dir += "/"

                dst_file_path = (merged_apex_extract_dir_path
                                 + extract_dir
                                 + file)
                parent_dir = os.path.dirname(dst_file_path)
                if "@" in parent_dir:
                    logging.info(f"Found TAG in APEX vendor app path: {dst_file_path}. Removing TAG.")
                    get_after_split = parent_dir.split('@', 1)[1]
                    dst_file_path = (merged_apex_extract_dir_path
                                     + extract_dir
                                     + file)
                    dst_file_path = dst_file_path.replace(get_after_split, "").replace("@", "")
                    logging.info(f"APEX extract dir after TAG removal: {extract_dir} | {dst_file_path}")
                current_username = os.getlogin()
                command = (f'sudo mkdir -p "$(dirname {dst_file_path})" 2>/dev/null '
                           f'&& sudo cp {file_path} {dst_file_path} '
                           f'&& sudo chown {current_username}:{current_username} {dst_file_path} '
                           f'&& sudo chmod 0755 {dst_file_path}')
                logging.info(f"Copy APEX vendor app: {file_path} into {dst_file_path} with command: {command}")
                result = subprocess.run(command, shell=True, capture_output=True, text=True)
                if result.returncode != 0:
                    logging.error(
                        f"Error copying file in APEX container (when injecting APKs): {file_path} with {dst_file_path} | {result.stderr}")

                if os.path.exists(dst_file_path):
                    logging.info(f"Copied APK file into APEX: {file_path} with {dst_file_path}")
                    files_coped_list.append(file)
                else:
                    logging.error(f"APK file does not exist after coping in APEX")
    logging.info(f"APEX: APK Files copied into container: {files_coped_list};\n")
    return files_coped_list


def inject_apex_vendor_files(merged_apex_extract_dir_path, apex_vendor_extract_dir_path):
    files_coped_list = []
    current_username = os.getlogin()
    for root, dirs, files in os.walk(apex_vendor_extract_dir_path):
        for file in files:
            file_path = str(os.path.join(root, file))
            if file_path.endswith(".apk"):
                continue
            if os.path.islink(file_path):
                dst_file_path = merged_apex_extract_dir_path + root.replace(apex_vendor_extract_dir_path, "").replace("//", "/")
                if not dst_file_path.endswith("/"):
                    dst_file_path += "/"
                command = (f'sudo cp -a {file_path} {dst_file_path} '
                           f'&& sudo chown -R {current_username}:{current_username} {dst_file_path} '
                           f'&& sudo chmod -R 0755 {dst_file_path}')
                logging.info(f"Copying symlink in APEX container: {file_path} into {dst_file_path} with command: {command}")
                result = subprocess.run(command, shell=True, capture_output=True, text=True)
                if result.returncode != 0:
                    logging.error(
                        f"Error copying symlink in APEX container: {file_path} with {dst_file_path} | {result.stderr}")
                if os.path.exists(dst_file_path):
                    logging.info(f"Copied symlink in APEX container: {file_path} with {dst_file_path}")
                    files_coped_list.append(dst_file_path)
            else:
                #file_path_no_vendor = str(file_path.replace(".Google", "").replace(".google", ""))
                file_path_no_vendor = file_path.replace(apex_vendor_extract_dir_path, "")
                if file_path_no_vendor.startswith("/"):
                    file_path_no_vendor = file_path_no_vendor[1:]
                dst_file_path = os.path.join(merged_apex_extract_dir_path, file_path_no_vendor)
                try:
                    if os.path.isfile(dst_file_path):
                        directory_path = os.path.dirname(dst_file_path)
                        logging.info(f"Creating directory for APEX container: {directory_path}")
                        command = (f'sudo mkdir -p {directory_path} '
                                   f'&& sudo chown -R {current_username}:{current_username} {directory_path} '
                                   f'&& sudo chmod -R 0755 {directory_path}')
                        result = subprocess.run(command, shell=True, capture_output=True, text=True)
                        if result.returncode != 0:
                            logging.error(
                                f"Error creating directory in APEX container: {directory_path} | {result.stderr}")
                except PermissionError as e:
                    logging.error(f"Permission denied to create directory: {e}")

                if "fmd-aecs-lock" in file_path:
                    continue

                if "apex_manifest.pb" in dst_file_path or "apex_manifest.pb" in file_path:
                    continue

                file_ext = os.path.splitext(file)[1]

                if file_ext in ["", None] and POST_INJECTOR_CONFIG["DISABLE_APEX_BINARY_INJECTION"]:
                    logging.error(f"SKIPPED APEX Binary Injection: DISABLE_APEX_BINARY_INJECTION is set to True: {file_path}")
                    continue

                if file in POST_INJECTOR_CONFIG["DISALLOW_APEX_FILE_OVERWRITE"]:
                    logging.error(f"SKIPPED APEX File: File in DISALLOW_APEX_FILE_OVERWRITE: {file_path}")
                    continue

                if file_ext not in ["", None] and (len(POST_INJECTOR_CONFIG["ALLOWED_APEX_FILE_INJECTION_EXTENSIONS"]) > 0
                        and file_ext not in POST_INJECTOR_CONFIG["ALLOWED_APEX_FILE_INJECTION_EXTENSIONS"]):
                    logging.error(f"SKIPPED APEX File Injection: File not in ALLOWED_APEX_FILE_INJECTION_EXTENSIONS: {file_path}")
                    continue

                if file_ext not in ["", None] and file_ext in POST_INJECTOR_CONFIG["DISALLOW_APEX_FILE_INJECTION_EXTENSIONS"]:
                    logging.error(f"SKIPPED APEX File: File in DISALLOW_APEX_FILE_EXTENSIONS: {file_path}")
                    continue


                try:
                    logging.info(f"APEX Vendor: {merged_apex_extract_dir_path} | dst: {dst_file_path}")
                    if (os.path.exists(file_path)
                            and os.path.isfile(file_path)
                            and dst_file_path.startswith(merged_apex_extract_dir_path)
                            and dst_file_path.startswith("/tmp")):
                        dir_path = os.path.dirname(dst_file_path)
                        command = (f'sudo mkdir -p {dir_path} '
                                   f'&& sudo cp -f {file_path} {dst_file_path} '
                                   f'&& sudo chown -R {current_username}:{current_username} {dst_file_path} '
                                   f'&& sudo chmod -R 0755 {dst_file_path}')
                        logging.info(f"Run APEX copy command: {command}")
                        result = subprocess.run(command, shell=True, capture_output=True, text=True)
                        if result.returncode != 0:
                            logging.error(
                                f"Error copying file in APEX container: statuscode: {result.returncode}: "
                                f"{file_path} with {dst_file_path} | stderr: {result.stderr} | stdout: {result.stdout}")
                        else:
                            logging.info(f"Copied file into APEX container: {file_path} with {dst_file_path}")
                            files_coped_list.append(dst_file_path)
                    else:
                        logging.error(f"Incorrect copy path for APEX file: src: {file_path} dst: {dst_file_path}")
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


def get_apex_file_mapping(key):
    apex_file_name_no_extension = f"com.android.{key}"
    if key == "vndk":
        apex_file_name_no_extension = f"com.android.vndk.current"
    elif key == "statsd":
        apex_file_name_no_extension = f"com.android.os.statsd"
    elif key == "swcodec":
        apex_file_name_no_extension = f"com.android.media.swcodec"
    elif key == "tzdata3" or key == "tzdata4" or key == "tzdata5" or key == "tzdata":
        apex_file_name_no_extension = f"com.android.tzdata"
    return apex_file_name_no_extension

def remove_apex_build_strings(apex_split_name_list):
    for split in apex_split_name_list:
        if "_compressed" in split or "_trimmed" in split:
            apex_split_name_list.remove(split)
            split = split.replace("_compressed", "").replace("_trimmed", "")
            apex_split_name_list.append(split)
    return apex_split_name_list

def get_apex_default_keys(aosp_path, apex_file_name):
    apex_split_name_list = apex_file_name.split(".")
    apex_split_name_list = remove_apex_build_strings(apex_split_name_list)
    logging.info(f"APEX: Getting default keys for: {apex_split_name_list}")
    for key, value in POST_INJECTOR_CONFIG["APEX_DEFAULT_PATHS_DICT"].items():
        if key.lower() in [s.lower() for s in apex_split_name_list]:
            apex_file_name_no_extension = get_apex_file_mapping(key)
            module_path = str(os.path.join(aosp_path, value))
            priv_pem_file_path = os.path.join(module_path, apex_file_name_no_extension + ".pem")
            priv_key_file_path = os.path.join(module_path, apex_file_name_no_extension + ".pk8")
            avb_pub_key_path = os.path.join(module_path, apex_file_name_no_extension + ".avbpubkey")
            if not os.path.exists(avb_pub_key_path):
                is_success = extract_avb_public_key(aosp_path, priv_pem_file_path, avb_pub_key_path)
                if not is_success:
                    raise ValueError(f"Error extracting AVB public key for APEX: {apex_file_name}. "
                                     f"Private PEM file: {priv_pem_file_path} to {avb_pub_key_path}")
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
                                 f"Key files not found in {module_path} with private: {priv_pem_file_path}.")
    raise ValueError(f"Error getting APEX default keys: {apex_file_name}. Key files not found in {POST_INJECTOR_CONFIG['APEX_DEFAULT_PATHS_DICT']}")


def get_aosp_file_context_file_name(key):
    file_context_name = f"com.android.{key}-file_contexts"
    if key == "bluetooth":
        file_context_name = f"com.android.{key}.updatable-file_contexts"
    elif key == "swcodec":
        file_context_name = f"com.android.media.{key}-file_contexts"
    elif key == "statsd":
        file_context_name = f"com.android.os.{key}-file_contexts"
    elif key == "tzdata3" or key == "tzdata" or key == "tzdata4" or key == "tzdata5":
        file_context_name = f"com.android.tzdata-file_contexts"
    return file_context_name


def get_existing_file_context(apex_file_name, aosp_path):
    file_contexts_path = None
    apex_split_name_list = apex_file_name.split(".")
    apex_split_name_list = remove_apex_build_strings(apex_split_name_list)
    for key, value in POST_INJECTOR_CONFIG["APEX_DEFAULT_PATHS_DICT"].items():
        if key.lower() in [s.lower() for s in apex_split_name_list]:
            file_context_name = get_aosp_file_context_file_name(key)
            file_contexts_path = os.path.join(aosp_path, "system/sepolicy/apex", file_context_name)
            if os.path.exists(file_contexts_path):
                logging.info(f"APEX: File contexts found: {file_contexts_path}")
                return file_contexts_path
            else:
                return FILE_CONTEXT_TEMPLATE_PATH
                #raise ValueError(f"Error getting APEX file context file from AOSP: {apex_file_name}. file_context_name: {file_context_name}")
    return file_contexts_path


def create_apex_container(apex_manifest_path, apex_extract_dir_path, apex_root_path, aosp_path, output_file_path, lunch_target, canned_fs_config, is_repack=False, file_contexts_path=None, aosp_version=None):
    success = False
    resign_apex_apk_files(aosp_path, apex_extract_dir_path, aosp_version)

    apexer_bin_candidates = [
        os.path.join(aosp_path, "out/soong/host/linux-x86/bin/apexer"),
        os.path.join(aosp_path, "out/host/linux-x86/bin/apexer"),
    ]
    apexer_bin_path = next((p for p in apexer_bin_candidates if os.path.exists(p)), None)
    if not apexer_bin_path:
        message = "APEX create_apex_container failed: Apexer tool not found in any known location."
        logging.info(message)
        return False, f"{message}", None, None, None, None, None

    apex_file_name = os.path.basename(output_file_path)
    info = f"APEX: Apexer tool path: {apexer_bin_path}|{lunch_target}|{apex_manifest_path}|{apex_extract_dir_path}|{output_file_path}|{canned_fs_config.name}|{FILE_CONTEXT_TEMPLATE_PATH}"
    logging.info(info)

    if not is_repack:
        logging.info(f"Using default AVB keys for APEX: {apex_file_name}")
        private_key_path, priv_pem_file_path, avb_pub_key_path, cert_apex_apk_path = get_apex_default_keys(aosp_path, apex_file_name)
        if not file_contexts_path:
            file_contexts_path = get_existing_file_context(apex_file_name, aosp_path)
    else:
        logging.info(f"Creating key material for APEX: {apex_file_name}")
        is_success, log_message, temp_keys_dir, private_key_path, priv_pem_file_path, pub_key_path, avb_pub_key_path, \
                            cert_apex_apk_path = generate_apex_keys(aosp_path, apex_file_name)
        logging.info(f"Key material created for APEX: {temp_keys_dir}, "
                     f"{private_key_path},"
                     f"{priv_pem_file_path}, {pub_key_path}, {avb_pub_key_path}, {cert_apex_apk_path}")
        if not file_contexts_path:
            file_contexts_path = FILE_CONTEXT_TEMPLATE_PATH
        if not is_success:
            logging.error(f"Error generating APEX keys: {log_message}")
            return False, f"Error generating APEX keys: {log_message}", None, None, None, None, None


    command = f"cd {apex_root_path} && {apexer_bin_path} --verbose " \
              f"--key={priv_pem_file_path} " \
              f"--pubkey={avb_pub_key_path} " \
              f"--apexer_tool_path={aosp_path}out/host/linux-x86/bin/:{aosp_path}out/soong/host/linux-x86/bin/ " \
              f"--file_contexts={file_contexts_path} " \
              f"--canned_fs_config={canned_fs_config.name} " \
              f"--include_build_info " \
              f"--force " \
              f"{apex_extract_dir_path} " \
              f"{output_file_path}"
    logging.info(f"Apexer Container creation command: {command}")

    if os.path.exists(apex_root_path) \
        and os.path.exists(apexer_bin_path) \
        and os.path.exists(apex_manifest_path) \
        and os.path.exists(apex_extract_dir_path) \
        and os.path.exists(canned_fs_config.name) \
        and os.path.exists(file_contexts_path) \
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
        info = f"Container Creation not started for {apex_file_name} because of missing files:\n" \
                    f"APEX root path: {apex_root_path} | {os.path.exists(apex_root_path)}\n" \
                    f"APEXer tool path: {apexer_bin_path} | {os.path.exists(apexer_bin_path)}\n" \
                    f"APEX manifest path: {apex_manifest_path} | {os.path.exists(apex_manifest_path)}\n" \
                    f"APEX extract dir path: {apex_extract_dir_path} | {os.path.exists(apex_extract_dir_path)}\n" \
                    f"Canned fs config path: {canned_fs_config.name} | {os.path.exists(canned_fs_config.name)}\n" \
                    f"File contexts path: {file_contexts_path} | {os.path.exists(file_contexts_path)}\n" \
                    f"AVB public key path: {avb_pub_key_path} | {os.path.exists(avb_pub_key_path)}\n" \
                    f"Private PEM file path: {priv_pem_file_path} | {os.path.exists(priv_pem_file_path)}\n"
        logging.error(info)
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


def sign_apex_file(file_path, aosp_path, priv_key_apex_apk_path, apex_apk_certificate_path, lunch_target):
    error_message = None
    #signing_key_path = get_signing_key_path(aosp_path, "platform")
    use_apksigner = False
    if use_apksigner:
        logging.info(f"Signing APEX using apksigner: {file_path}")
        is_success, log_message = sign_apex_container_apksigner(file_path, priv_key_apex_apk_path, apex_apk_certificate_path)
    else:
        logging.info(f"Signing APEX using signapk: {file_path}")
        is_success, log_message = sign_apex_container_signapk(file_path,
                        priv_key_apex_apk_path,
                        apex_apk_certificate_path,
                        aosp_path,
                        lunch_target)

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


def generate_canned_fs_config(apex_extract_dir_path, output_file, apk_name_list=None, allow_filtering=True):
    """
    Generates a canned_fs_config file for the given directory. The config contains the file paths and their
    permissions. The method gives all the files and directories the default permissions.

    :param apex_extract_dir_path: str - path to the directory where the extracted apex files reside.
    :param output_file: str - path to the output file where the canned_fs_config will be saved.

    """
    if apk_name_list is None:
        apk_name_list = []
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
                if allow_filtering:
                    is_apk = file_path.endswith(".apk")
                    if is_apk and not any(apk_name in file_path for apk_name in apk_name_list):
                        try:
                            logging.error(f"APEX: SKIPPED apk. File not included into canned_fs: {file_path}")
                            os.remove(file_path)
                        except Exception as e:
                            logging.error(f"Error deleting file from canned_fs: {file_path} | {e}")
                        continue

                if "apex_pubkey" in file_name:
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
                # if "boot" in file_name and "arm64" in file_path:
                #     parent_dir = os.path.dirname(file_path)
                #     grandparent_dir = os.path.dirname(parent_dir)
                #     copy_dst = os.path.join(grandparent_dir, file_name)
                #     logging.info(f"APEX Copying boot.art javalib: {file_path}:{copy_dst}")
                #     shutil.copyfile(file_path, copy_dst)
                #     relative_file_path = os.path.relpath(copy_dst, apex_extract_dir_path)
                #     logging.info(f"APEX Write new boot.art path: {relative_file_path}:{copy_dst}:{parent_dir}")
                #     out_file.write(f"/{relative_file_path} {user_id} {group_id} {mode}\n")
                #     file_inserted_entries.append(f"/{relative_file_path} {user_id} {group_id} {mode}")
    logging.info(f"APEX: Canned FS Config file created: {output_file} | {file_inserted_entries}")

def extract_apex_file(aosp_path, apex_file_path, output_dir_path, lunch_target, aosp_version):
    """
    Extracts the APEX file using deapexer.

    :param aosp_path: str - path to the AOSP source code.
    :param apex_file_path: str - path to the APEX file.
    :param output_dir_path: str - path to the output directory where the apex will be extracted to.
    :param lunch_target: str - lunch target for the AOSP build.

    :return: bool - True if the extraction was successful, False otherwise.

    """
    logging.info(f"Extracting APEX file: {apex_file_path}")
    deapexer_candidates = [
        os.path.join(aosp_path, "out/soong/host/linux-x86/bin/deapexer"),
        os.path.join(aosp_path, "out/host/linux-x86/bin/deapexer"),
    ]
    deapexer_tool_path = next((p for p in deapexer_candidates if os.path.exists(p)), None)
    if not deapexer_tool_path:
        message = "APEX extract_apex_file failed: Deapexer tool not found in any known location."
        logging.info(message)
        return False, {f"{message}"}

    info = f"APEX: Deapexer tool path: {deapexer_tool_path}|{lunch_target}|{apex_file_path}|{output_dir_path}"
    logging.info(info)
    command = f"bash -c 'cd {aosp_path} && source {aosp_path}build/envsetup.sh && lunch {lunch_target} " \
               f"&& {deapexer_tool_path} extract {apex_file_path} {output_dir_path}'"
    is_success, log = execute_shell_command(command, aosp_path)
    if not is_success:
        logging.warning(f"APEX: Deapexer extraction failed - retry: {log}")
        is_success, log = execute_shell_command(command, aosp_path)

    logging.info(f"APEX: Deapexer extraction command: {command} | {is_success} | {log}")
    return is_success, {f"ERROR: {log}| More infos: {info}"}


def create_apex_manifest_file(apex_extract_dir_path, apex_package_name):
    manifest_file_name = "AndroidManifest.json"
    manifest_file_path = os.path.join(apex_extract_dir_path, manifest_file_name)
    with open(manifest_file_path, 'w') as manifest_file:
        template_folder_abs_path = os.path.join(ROOT_PATH, TEMPLATE_FOLDER)
        environment = Environment(loader=FileSystemLoader(str(template_folder_abs_path)))
        template = environment.get_template(manifest_file_name)
        rendered_template = template.render(package=apex_package_name, versionCode=999)
        manifest_file.write(rendered_template)


def move_apex_manifest_file(apex_extract_dir_path, output_dir_path, apex_filename, aosp_path, lunch_target):
    """
    Searches for the APEX manifest file in the APEX extract directory and moves it to the current directory.
    Moving is necessary because the apexer tool requires the manifest file to be in the same directory as the APEX files and not in
    the subdirectory.

    :param apex_extract_dir_path: str - path to the APEX extract directory.
    :param output_dir_path: str - path to the output directory where the APEX manifest file will be moved to.

    :return: bool - True if the APEX manifest file was found and moved, False otherwise.

    """
    logging.debug(f"Copying APEX manifest file.")
    manifest_dst = os.path.join(output_dir_path, "apex_manifest.pb")
    is_apex_manifest_file_found = False
    try:
        for root, dirs, files in os.walk(apex_extract_dir_path):
            for file in files:
                logging.info(f"Scanning for APEX manifest file: {file}")
                if file == "apex_manifest.pb":
                    file_path = str(os.path.join(root, file))
                    if os.path.exists(file_path):
                        logging.info(f"Found APEX manifest file: {file_path} to delete")
                        shutil.move(file_path, manifest_dst)
                    else:
                        logging.error(f"No APEX manifest found in {apex_extract_dir_path} | {file_path}")
                        exit(1)
                    #manifest_json_file_path = get_apex_manifest_from_aosp(aosp_path, apex_file_name)
                    #convert_apex_manifest_json_to_pb(manifest_json_file_path, manifest_dst)
                    logging.info(f"Copied APEX manifest file: {file_path} to {manifest_dst}.")
                    if os.path.exists(manifest_dst):
                        is_apex_manifest_file_found = True
                        logging.info(f"APEX manifest file found: {manifest_dst}")
                    break
    except Exception as ex:
        logging.error(f"Failed to move APEX manifest file: {ex}")
        traceback.print_exc()
        traceback.print_stack()

    if not is_apex_manifest_file_found:
        if not is_apex_manifest_file_found:
            manifest_json_str = f"""{{
          "name": "{apex_filename.replace(".apex", "").replace(".capex", "")}",
          "version": 999999
        }}
        """
            with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode='w',
                                             encoding='utf-8') as temp_manifest_file:
                temp_manifest_file.write(manifest_json_str)
                temp_manifest_path = temp_manifest_file.name
        convert_manifest_from_json(apex_manifest_path=temp_manifest_path, out_file_path=manifest_dst, aosp_path=aosp_path, lunch_target=lunch_target)
        if os.path.exists(manifest_dst):
            is_apex_manifest_file_found = True
            logging.info(f"APEX manifest file created from template: {manifest_dst}")

    return is_apex_manifest_file_found, str(manifest_dst)

def convert_apex_manifest_json_to_pb(apex_manifest_path, output_file_path):
    command = f"export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python && python3 ./conv_apex_manifest.py proto {apex_manifest_path} -o {output_file_path}"
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    logging.info(f"Converting APEX manifest file to pb: {command}")
    if result.returncode == 0:
        logging.info(f"APEX: APEX manifest file converted to pb: {output_file_path}")
    else:
        raise ValueError(f"APEX: Error converting APEX manifest file to pb: {result.stderr}")

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
    for key, shared_uid_list in POST_INJECTOR_CONFIG["SHARED_USER_ID_MAPPING_DICT"].items():
        for shared_uid in shared_uid_list:
            if search_string_in_apk(apk_file, shared_uid):
                signing_key = key
                break
    return signing_key

def get_signing_key_from_filename(apk_file, aosp_version):
    file_name = os.path.basename(apk_file).lower()
    if "media" in file_name:
        key = "media"
    elif any(keyword in file_name for keyword in ["network", "tethering", "cellbroadcast"]):
        key = "networkstack"
    else:
        key = "platform"

    if aosp_version and int(aosp_version) >= 13 and "bluetooth" in file_name:
        key = "bluetooth"

    return key


def resign_apex_apk_files(aosp_path, apex_extract_dir_path, aosp_version):
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
                    signing_key = get_signing_key_from_filename(apk_file_path, aosp_version)
                    logging.info(f"Signing key not found in APK manifest. Using filename to determine key: {apk_file_path} | {signing_key}")
                else:
                    logging.info(f"Signing key found in APK manifest: {apk_file_path}. Using manifest to determine key: {signing_key}")

                signing_key_path = get_signing_key_path(aosp_path, signing_key)
                success, log_message = sign_apk_file(apk_file_path, signing_key_path, v4_signing_enabled=False)
                if success:
                    logging.info(f"APEX: Success resigning APK file: {file}|{apk_file_path} with key {signing_key_path} ")
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


def create_key_paths(apex_file_name):
    temp_keys_dir = tempfile.mkdtemp(suffix="_apex_keys")
    apex_file_name = apex_file_name.replace(".apex", "").replace(".capex", "")
    priv_key_path = os.path.join(temp_keys_dir, f"{apex_file_name}.pk8")
    pub_key_path = os.path.join(temp_keys_dir, f"{apex_file_name}.cert")
    priv_pem_file_path = os.path.join(temp_keys_dir, f"{apex_file_name}.pem")
    avb_pub_key_path = os.path.join(temp_keys_dir, f"{apex_file_name}.avbpubkey")
    apex_apk_cert = os.path.join(temp_keys_dir, f"{apex_file_name}.x509.pem")
    return temp_keys_dir, priv_key_path, pub_key_path, priv_pem_file_path, avb_pub_key_path, apex_apk_cert


def generate_apex_keys(aosp_path, apex_file_name):
    temp_keys_dir, priv_key_path, pub_key_path, priv_pem_file_path, avb_pub_key_path, apex_apk_cert = create_key_paths(apex_file_name)
    is_success = False
    log_message = ""

    # Generate private and public keys in PEM format
    command_pem = [
        'openssl', 'genpkey', '-algorithm', 'RSA', '-out', priv_pem_file_path, '-pkeyopt', 'rsa_keygen_bits:4096'
    ]
    result_pem = subprocess.run(command_pem, capture_output=True, text=True)
    if result_pem.returncode != 0 or not os.path.exists(priv_pem_file_path):
        log_message = f"Error generating PEM keys: {result_pem.stderr}"
        logging.error(log_message)
    else:
        logging.info(f"PEM keys generated successfully: {priv_pem_file_path}")
        log_message = result_pem.stdout

    # Convert private key from PEM to PK8 format
    command_pk8 = [
        'openssl', 'pkcs8', '-topk8', '-inform', 'PEM', '-outform', 'DER', '-in', priv_pem_file_path, '-out', priv_key_path, '-nocrypt'
    ]
    result_pk8 = subprocess.run(command_pk8, capture_output=True, text=True)
    if result_pk8.returncode != 0 or not os.path.exists(priv_key_path):
        log_message += f"\nError converting PEM to PK8: {result_pk8.stderr}"
        logging.error(log_message)
    else:
        logging.info(f"PK8 key generated successfully: {priv_key_path}")
        log_message += result_pk8.stdout

    # Generate x509 certificate using the private key in PEM format
    command_x509 = [
        'openssl', 'req', '-x509', '-key', priv_pem_file_path,
        '-out', apex_apk_cert, '-days', '365', '-nodes', '-subj', '/CN=example.com'
    ]
    result_x509 = subprocess.run(command_x509, capture_output=True, text=True)
    if result_x509.returncode != 0 or not os.path.exists(apex_apk_cert):
        log_message += f"\nError generating x509 certificate: {result_x509.stderr}"
        logging.error(log_message)
    else:
        logging.info(f"x509 certificate generated successfully: {apex_apk_cert}")
        log_message += result_x509.stdout
        is_success = True

    is_success = extract_avb_public_key(aosp_path, priv_key_path, avb_pub_key_path)
    if not is_success:
        log_message += f"\nError extracting AVB public key for APEX: {apex_file_name}. Private key file: {priv_key_path} to {avb_pub_key_path}"
        logging.error(log_message)
    else:
        logging.info(f"AVB public key extracted successfully: {avb_pub_key_path}")

    return is_success, log_message, temp_keys_dir, priv_key_path, priv_pem_file_path, pub_key_path, avb_pub_key_path, apex_apk_cert



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
    is_success = True
    try:
        avbtool_path = os.path.join(aosp_path, "out/host/linux-x86/bin/avbtool")
        avb_extract_command = [avbtool_path, 'extract_public_key', "--key", key, "--output", avb_pub_out_path]
        subprocess.run(avb_extract_command, check=True)
        logging.info(f"AVB public key extracted at: {avb_pub_out_path}")
    except Exception as e:
        logging.error(f"Error extracting AVB public key: {e}")
        is_success = False
    return is_success


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

    # .replace(".google", "").replace("Google", "")
    apex_filename_no_ext = os.path.splitext(apex_filename)[0]
    remove_vendor_name_from_filename(apex_filename_no_ext)

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
