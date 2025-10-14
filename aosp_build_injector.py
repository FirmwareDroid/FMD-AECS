"""
A command-line tool that downloads files related to the build process of an Android firmware image and stores them
on disk. Directly extract the downloaded zip content.
"""
import argparse
import json
import re
import traceback
import uuid
import logging
import shutil
import subprocess
import glob
from tqdm import tqdm
from jinja2 import Environment, FileSystemLoader
from getpass import getpass
import time
from aosp_apex_injector import repackage_apex_file
from aosp_post_build_injector import start_post_build_injector
from common import extract_zip, load_configs
from config import *
from fmd_backend_requests import download_firmware_build_files, get_csrf_token, authenticate_fmd, \
    get_firmware_ids, get_graphql_url, upload_image_as_raw
from setup_logger import setup_logger



if os.environ.get("FMD_DEBUG") == "True":
    setup_logger(logging.DEBUG)
else:
    setup_logger()


def delete_files(dir_path):
    """
    Deletes all files in the given directory.

    :param dir_path: str - path of the directory to delete files from.

    """
    files = glob.glob(dir_path)
    for f in files:
        os.remove(f)


def start_aosp_build(aosp_path, aosp_packages_path, firmware_id, lunch_target, aosp_version, skip_filtering, cookies):
    """
    Wrapper method to start the firmware injection and build process.

    :param lunch_target: str - aosp build argument to select the build arch.
    :param firmware_id: str - object-id of the firmware
    :param aosp_packages_path: str - path to the prebuilt package folder of aosp.
    :param aosp_path: str - path to aosp root folder.
    :param aosp_version: str - version of the aosp build.
    :param skip_filtering: bool - skip the filtering process.

    :returns: bool - True if the build process was successful.


    """
    pre_injector_start_time = time.time()
    is_successful = False
    logging.debug(f"Start aosp {aosp_version} build injection with firmware: {firmware_id}")
    overwrite_partition_size(aosp_path, aosp_packages_path, aosp_version)
    if aosp_version in ["11", "12"]:
        blueprint_build_command = f"bash -c 'cd {aosp_path} && source {aosp_path}/build/envsetup.sh && lunch {lunch_target} && m clean && m blueprint_tools otatools debugfs_static'"
    else:
        blueprint_build_command = f"bash -c 'cd {aosp_path} && source {aosp_path}/build/envsetup.sh && lunch {lunch_target} && m clean && m blueprint_tools otatools debugfs_static apexer deapexer avbtool'"
    execute_build_command(aosp_path, firmware_id, blueprint_build_command, aosp_path)
    logging.debug(f"Environment setup for {lunch_target} completed. Moving packages to aosp source code next.")
    try:
        move_txt_files(EXTRACTED_PACKAGES_PATH, BUILD_OUT_PATH)
        if PRE_INJECTOR_CONFIG["ENABLE_INJECTION"]:
            included_package_statistics = move_packages_to_aosp(aosp_path, EXTRACTED_PACKAGES_PATH, lunch_target, aosp_version)
        else:
            logging.debug("Skipping package injection as ENABLE_INJECTION is set to False.")
            included_package_statistics = {"apps": [], "libs": [], "apex": [], "count": 0}
    except Exception as e:
        logging.error(f"Error moving packages to aosp source code: {e}. EXIT PROGRAM!")
        traceback.print_exc()
        exit(-1)

    pre_injector_end_time = time.time()
    try:
        result = {
            "hostname": os.uname()[1],
            "firmware_id": firmware_id,
            "included_package_statistics": included_package_statistics,
            "pre_injector_duration": round(pre_injector_end_time - pre_injector_start_time, 2),
        }
        logging.info(json.dumps(result, indent=4))
        write_json_output(result, PATH_BUILD_INJECTOR_LOG)
    except Exception as err:
        logging.error(f"Error writing build injector log: {err}")
        traceback.print_exc()
        exit(-1)

    try:
        package_name_list = []
        package_name_list.extend(included_package_statistics["apps"])
        package_name_list.extend(included_package_statistics["libs"])
        package_name_list.extend(included_package_statistics["apex"])
        inject_meta_files(aosp_path, aosp_version, package_name_list)
        logging.debug(f"Injected meta files into aosp source code: {aosp_path}")
    except Exception as err:
        logging.error(f"Error injecting meta files: {err}")
        traceback.print_exc()
        exit(-1)

    retry_attempts = BUILD_RETRY_COUNT
    while not is_successful and retry_attempts > 0:
        try:
            main_build_command = get_aosp_build_command(lunch_target, aosp_version, aosp_path)
            build_start_time = time.time()
            execute_build_command(aosp_path, firmware_id, main_build_command, aosp_path)
            build_end_time = time.time()
            logging.info(f"AOSP main build completed successfully. Continuing with post-build injection.")
            target_out_path = get_target_out_path(aosp_path, lunch_target)
            all_extracted_firmware_files_path = os.path.join(EXTRACTED_PACKAGES_PATH, EXTRACTION_ALL_FILES_DIR_NAME)

            start_post_build_injector(aosp_path=aosp_path,
                                      source_folder_path=all_extracted_firmware_files_path,
                                      target_out_path=target_out_path,
                                      lunch_target=lunch_target,
                                      firmware_id=firmware_id,
                                      pre_injector_package_list=included_package_statistics["apps"],
                                      pre_injector_config_path=PRE_INJECTOR_CONFIG_PATH,
                                      post_injector_config_path=POST_INJECTOR_CONFIG_PATH,
                                      cookies=cookies,
                                      aosp_version=aosp_version
                                      )
            included_package_statistics["main_build_duration"] = round(build_end_time - build_start_time, 2)
            logging.info(f"Summary Pre-Injector: {included_package_statistics}")
            package_build_artefacts_command = get_aosp_repo_build_command(aosp_path, lunch_target, aosp_version)
            package_start_time = time.time()
            execute_build_command(aosp_path, firmware_id, package_build_artefacts_command, aosp_path)
            package_end_time = time.time()
            included_package_statistics["package_build_artefacts_duration"] = round(package_end_time - package_start_time, 2)
            is_successful = True
        except Exception as err:
            logging.error(err)
            retry_attempts -= 1
    return is_successful


def get_target_out_path(aosp_path, lunch_target):
    """
    Returns the target out path based on the lunch target.
    E.g. "/home/ubuntu/aosp_12/out/target/product/emulator_arm64/"

    :param aosp_path: str - path to the root of the aosp source code.
    :param lunch_target: str - aosp build argument to select the build arch.

    :returns: str - path to the target out path.

    """
    if lunch_target == SUPPORTED_LUNCH_TARGETS[0]:
        return os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_x86_64_PATH)
    elif lunch_target == SUPPORTED_LUNCH_TARGETS[1]:
        return os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_ARM64_PATH)
    elif lunch_target == SUPPORTED_LUNCH_TARGETS[2]:
        return os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_ARM64_x64_PATH)
    elif lunch_target == SUPPORTED_LUNCH_TARGETS[3]:
        return os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_ARM64_x64_PATH_A14)
    else:
        logging.error(f"Unknown lunch target: {lunch_target}")
        raise RuntimeError(f"Unsupported build architecture: {lunch_target}")


def get_emulator_image_path(aosp_path, lunch_target, aosp_version):
    """
    Returns the path to the emulator image zip file based on the lunch target.

    :param aosp_path: str - path to the root of the aosp source code.
    :param lunch_target: str - aosp build argument to select the build arch.

    :returns: str - path to the emulator image zip file.

    """
    image_source_path = None
    is_phone_64 = "phone64" in lunch_target
    if aosp_version in ["11", "12"]:
        if is_phone_64:
            image_source_path = os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_ARM64_x64_PATH, AOSP_EMU_ZIP_FILENAME_A12_A13)
        else:
            if aosp_version == "11":
                image_source_path = os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_ARM64_PATH,
                                                 AOSP_EMU_ZIP_FILENAME_A11)
            else:
                image_source_path = os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_ARM64_PATH, AOSP_EMU_ZIP_FILENAME_A12_A13)
    elif aosp_version == "13":
        image_source_path = os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_ARM64_x64_PATH, AOSP_EMU_ZIP_FILENAME_A12_A13)
    elif aosp_version in ["14", "15"]:
        image_source_path = os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_ARM64_x64_PATH_A14, AOSP_EMU_ZIP_FILENAME)

    if not os.path.exists(image_source_path):
        raise RuntimeError(f"Could not find image zip file: {image_source_path}. Something went wrong.")

    return image_source_path


def get_base_filename(meta_build_filename):
    """
    Returns the base filename of the aosp build file based on the meta_build_filename.

    :param meta_build_filename:

    :returns: str - base filename of the aosp build file.
    """
    if "build_product" in meta_build_filename:
        return BASE_PRODUCT_FILE_NAME
    elif "build_vendor" in meta_build_filename:
        return BASE_VENDOR_FILE_NAME
    elif "build_system_ext" in meta_build_filename:
        return BASE_SYSTEM_EXT_FILE_NAME
    elif "build_system" in meta_build_filename:
        return BASE_SYSTEM_FILE_NAME
    elif "handheld_system" in meta_build_filename:
        return BASE_HANDHELD_SYSTEM_FILE_NAME
    elif "handheld_system_ext" in meta_build_filename:
        return BASE_HANDHELD_SYSTEM_EXE_FILE_NAME
    elif "handheld_vendor" in meta_build_filename:
        return BASE_HANDHELD_VENDOR_FILE_NAME
    elif "handheld_product" in meta_build_filename:
        return BASE_HANDHELD_PRODUCT_FILE_NAME
    else:
        raise RuntimeError(f"Unsupported build architecture: {meta_build_filename}")

def read_and_render_template(meta_build_path, base_filename, aosp_version, package_name_list):
    """
    Reads the meta_build.txt file and renders the AOSP build file template with the package names.

    :param meta_build_path: str - path to the meta_build.txt file.
    :param base_filename: str - base filename of the AOSP build file to use as a template.
    :param aosp_version: str - version of the AOSP build.

    :returns: str - rendered AOSP build file template.
    """
    if os.path.exists(meta_build_path):
        package_line_list = extract_package_names(meta_build_path, package_name_list)
    else:
        package_line_list = []
    template_folder_abs_path = get_template_folder_path()
    return render_template(template_folder_abs_path, base_filename, package_line_list)


def extract_package_names(meta_build_path, package_name_list):
    """
    Extracts package names from the meta_build.txt file, filtering out blacklisted modules.

    :param meta_build_path: str - path to the meta_build.txt file.
    :param package_name_list: list - list of allowed package names to filter against.
    :returns: list - list of package names.

    """
    logging.debug(f"Package names to filter against: {package_name_list}")
    package_line_list = []
    with open(meta_build_path, 'r') as meta_build_file:
        for line in meta_build_file:
            stripped_line = clean_package_name(line)
            if stripped_line not in package_name_list:
                logging.debug(f"Removing blacklisted module from meta file {meta_build_path}: {line}")
            else:
                logging.debug(f"Allowing module meta in build: {line}")
                package_line_list.append(line)
    return package_line_list


def render_template(template_folder_abs_path, base_filename, package_name_list):
    """
    Renders the template with the given package names.

    :param template_folder_abs_path: str - path to the template folder.
    :param base_filename: str - base filename of the template.
    :param package_name_list: list - list of package names.

    :returns: str - rendered template.
    """
    logging.debug(f"Using template folder: {template_folder_abs_path} with base filename: {base_filename}")
    environment = Environment(loader=FileSystemLoader(str(template_folder_abs_path)))
    template = environment.get_template(base_filename)
    return template.render(package_name_list=package_name_list)

def get_template_folder_path():
    config_path = PRE_INJECTOR_CONFIG["PRE_INJECTOR_CONFIG_PATH"]
    base_dir = os.path.dirname(config_path)
    template_folder_abs_path = os.path.join(base_dir)
    if not os.path.isabs(template_folder_abs_path):
        template_folder_abs_path = os.path.join(ROOT_PATH, template_folder_abs_path)
        template_folder_abs_path = os.path.normpath(template_folder_abs_path)
    if not os.path.isdir(template_folder_abs_path):
        raise OSError(f"Could not find AOSP template folder: {template_folder_abs_path}")

    return template_folder_abs_path


def write_and_copy_file(content, out_file_path, aosp_base_file_path):
    """
    Writes the rendered aosp build file to the out_file_path and copies it to the aosp source code.

    :param content: str - rendered aosp build file template to be written to file.
    :param out_file_path: str - path to write the rendered aosp build file to.
    :param aosp_base_file_path: str - path to the aosp base file to copy the rendered file to.

    """
    with open(out_file_path, mode="w", encoding="utf-8") as out_file:
        out_file.write(content)
    shutil.copyfile(out_file_path, aosp_base_file_path)
    logging.debug(f"Placed {os.path.basename(out_file_path)} {aosp_base_file_path} in aosp source")


def delete_directory_if_exists(directory_path):
    """
    Deletes a directory if it exists.

    :param directory_path: str - path of the directory to delete.
    """
    if os.path.exists(directory_path) and os.path.isdir(directory_path):
        shutil.rmtree(directory_path)
        logging.debug(f"Directory {directory_path} has been deleted.")
    else:
        logging.debug(f"Directory {directory_path} does not exist.")


def get_directory_size(directory_path):
    """
    Calculate the size of directories starting with 'ib_' in bytes.

    :param directory_path: str - path to the directory to calculate the size of.

    :returns: int - size of the directories in bytes.

    """
    total = 0
    for dirpath, dirnames, filenames in os.walk(directory_path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp):
                total += os.path.getsize(fp)
    return total


def get_minimal_partition_size(aosp_path, aosp_packages_path):
    """
    Calculates the minimal partition size based on the size of the packages to inject.

    :param aosp_path: str - path to the root of the aosp source code.
    :param aosp_packages_path: str - path to the prebuilt package folder of aosp.

    :returns: int - minimal partition size in bytes.

    """
    packages_abs_path = os.path.join(aosp_path, aosp_packages_path)
    approximate_size = get_directory_size(packages_abs_path)
    default_size = 4294967296  # 4GB
    additional_gb_in_bytes = 1073741824 * 64  # 64GB
    twenty_gb_in_bytes = 1073741824 * 10  # 10GB
    while default_size < (approximate_size + twenty_gb_in_bytes):
        default_size += additional_gb_in_bytes
        logging.debug(f"Increasing partition size to: {default_size} Approximate bytes of packages "
                      f"to inject is: {approximate_size}")
    return default_size


def overwrite_partition_size(aosp_path, aosp_packages_path, aosp_version):
    """
    Overwrites the partition size in the aosp source code.

    :param aosp_path: str - path to the root of the aosp source code.
    :param aosp_packages_path: str - path to the prebuilt package folder of aosp.

    """
    minimal_partition_size = get_minimal_partition_size(aosp_path, aosp_packages_path)
    super_partition_size = minimal_partition_size + 8388608  # 8MB
    dynamic_partition_size = minimal_partition_size
    if aosp_version and int(aosp_version) >= 14:
        board_config_file_path = os.path.join(aosp_path, "build/make/target/board/BoardConfigGsiCommon.mk")
        with open(board_config_file_path, 'r') as base_file:
            lines = base_file.readlines()
        for i, line in enumerate(lines):
            if "BOARD_SUPER_PARTITION_SIZE" in line:
                lines[i] = f"  BOARD_SUPER_PARTITION_SIZE := {super_partition_size}\n"
            if "BOARD_GSI_DYNAMIC_PARTITIONS_SIZE" in line:
                lines[i] = f"  BOARD_GSI_DYNAMIC_PARTITIONS_SIZE := {dynamic_partition_size}\n"
    else:
        board_config_file_path = os.path.join(aosp_path, "build/make/target/board/BoardConfigEmuCommon.mk")
        logging.debug(f"Overwriting partition size to: {minimal_partition_size} in {board_config_file_path}")
        with open(board_config_file_path, 'r') as base_file:
            lines = base_file.readlines()
        for i, line in enumerate(lines):
            if "BOARD_SUPER_PARTITION_SIZE" in line:
                lines[i] = f"  BOARD_SUPER_PARTITION_SIZE := {super_partition_size}\n"
            if "BOARD_EMULATOR_DYNAMIC_PARTITIONS_SIZE" in line:
                lines[i] = f"  BOARD_EMULATOR_DYNAMIC_PARTITIONS_SIZE := {dynamic_partition_size}\n"
        with open(board_config_file_path, 'w') as base_file:
            base_file.writelines(lines)


def move_txt_files(source_directory, destination_directory):
    """
    Moves all text files from source_directory to destination_directory.

    :param source_directory: str - path of the source directory.
    :param destination_directory: str - path of the destination directory.
    """
    if not os.path.exists(destination_directory):
        os.makedirs(destination_directory, exist_ok=True)

    for file_name in os.listdir(source_directory):
        source_file = os.path.join(source_directory, file_name)
        if os.path.isfile(source_file) and (file_name.endswith('.txt') or file_name.endswith('.log')):
            destination_file = os.path.join(destination_directory, file_name)
            shutil.copy2(source_file, destination_file, follow_symlinks=False)


def check_file_extension(directory, file_extension_list):
    for filename in os.listdir(directory):
        file_extension = os.path.splitext(filename)[1]
        if file_extension in file_extension_list:
            return True
    return False

def get_two_levels_up(path):
    one_level_up = os.path.dirname(path)
    two_levels_up = os.path.dirname(one_level_up)
    return two_levels_up

def get_apex_file(directory_path):
    """
    Finds the apex or capex file in the folder
    """
    for filename in os.listdir(directory_path):
        if filename.lower().strip().endswith(".apex") or filename.lower().strip().endswith(".capex"):
            return os.path.join(directory_path, filename)
    return None


def move_packages_to_aosp(aosp_path, extracted_packages_path, lunch_target, aosp_version):
    """
    Moves the prebuilt packages to the AOSP source code.

    :param extracted_packages_path: str - path to the extracted packages.
    :param aosp_path: str - path to AOSP root folder.
    :param lunch_target: str - AOSP build argument to select the build arch.

    :returns: dict - statistics of included packages.
    """
    out_dir = os.path.join(aosp_path, MODULE_BASE_INJECT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    included_package_statistics = {"apps": [], "libs": [], "apex": [], "count": 0, "skipped_apps": [], "skipped_libs": [], "skipped_apex": []}
    for dir_name in os.listdir(extracted_packages_path):
        package_path = os.path.join(extracted_packages_path, dir_name)
        if os.path.isdir(package_path):
            included_package_statistics = process_package(package_path, dir_name, aosp_path, out_dir, included_package_statistics, lunch_target, aosp_version)

    included_package_statistics["count"] = len(included_package_statistics["apps"]) + \
                                           len(included_package_statistics["libs"]) + \
                                           len(included_package_statistics["apex"])
    included_package_statistics["apps"] = sorted(included_package_statistics["apps"])
    included_package_statistics["libs"] = sorted(included_package_statistics["libs"])
    included_package_statistics["apex"] = sorted(included_package_statistics["apex"])
    logging.info(f"Included package statistics: {included_package_statistics}")
    return included_package_statistics


def process_package(package_path, dir_name, aosp_path, out_dir, included_package_statistics, lunch_target, aosp_version):
    """
    Processes a single package directory and moves it to the appropriate location.

    :param package_path: str - path to the package directory.
    :param dir_name: str - name of the package directory.
    :param aosp_path: str - path to AOSP root folder.
    :param out_dir: str - output directory for injected packages.
    :param included_package_statistics: dict - statistics of included packages.
    :param lunch_target: str - AOSP build argument to select the build arch.
    """
    uuid_dir = str(uuid.uuid4())
    if is_package_skipped(dir_name, package_path):
        logging.info(f"Skipping package: {dir_name}")
        included_package_statistics["skipped_apps" if check_file_extension(package_path, [".apk"]) else
                                    "skipped_libs" if check_file_extension(package_path, [".so", ".1", ".2", ".3", ".4", ".5", ".6", ".7", ".8", ".9"]) else
                                    "skipped_apex"].append(dir_name)
        return included_package_statistics

    if check_file_extension(package_path, [".so", ".1", ".2", ".3", ".4", ".5", ".6", ".7", ".8", ".9"]):
        included_package_statistics = handle_library_package(package_path, dir_name, uuid_dir, aosp_path, out_dir, included_package_statistics)
    elif check_file_extension(package_path, [".apex", ".capex"]):
        included_package_statistics = handle_apex_package(package_path, dir_name, uuid_dir, aosp_path, out_dir, included_package_statistics, lunch_target, aosp_version)
    elif check_file_extension(package_path, [".apk"]):
        included_package_statistics = handle_app_package(package_path, dir_name, uuid_dir, out_dir, included_package_statistics)
    else:
        logging.error(f"Skipping package: {dir_name} as it does not match any known file type.")
        return included_package_statistics

    return included_package_statistics

def clean_package_name(package_name):
    """
    Cleans the package name by removing unwanted characters.

    :param package_name: str - raw package name.

    :returns: str - cleaned package name.
    """
    return package_name.replace("\\", "").replace("_FMD_APEX", "").replace("_fmd", "").strip()

def is_package_skipped(dir_name, package_path):
    """
    Checks if a package should be skipped based on its name.

    :param dir_name: str - name of the package directory.

    :returns: bool - True if the package should be skipped, False otherwise.
    """
    dir_name_cleaned = clean_package_name(dir_name)
    if dir_name_cleaned in SKIPPED_MODULE_NAMES or any(keyword in dir_name_cleaned for keyword in PRE_INJECTOR_CONFIG["BLACKLISTED_KEYWORDS"]):
        return True
    elif check_file_extension(package_path, [".apk"]):
        if not "_FMD_APEX" in dir_name:
            if any(keyword in dir_name_cleaned for keyword in PRE_INJECTOR_CONFIG["ALLOW_APP_KEYWORD_ALWAYS_LIST"]):
                logging.info(f"Injecting APK package due to always allow keyword: {dir_name_cleaned}")
                return False

            if PRE_INJECTOR_CONFIG["DISABLE_APP_INJECTION"]:
                logging.info(f"Skipping APK package due to disabled app injection: {dir_name_cleaned}")
                return True

            if any(keyword in dir_name_cleaned for keyword in PRE_INJECTOR_CONFIG["DISALLOWED_APK_KEYWORDS"]):
                logging.info(f"Skipping APK package due to disallowed keyword: {dir_name_cleaned}")
                return True

    if "_FMD_APEX" in dir_name:
        if not PRE_INJECTOR_CONFIG["DISABLE_APEX_APP_INJECTION"]:
            if "_FMD_APEX" in dir_name and any(keyword.lower() in dir_name_cleaned for keyword in
                   PRE_INJECTOR_CONFIG["APEX_PRE_INJECT_DISALLOWED_KEYWORDS"]):
                logging.info(f"Skipping APEX package due to disallowed keyword: {dir_name_cleaned}")
                return True
            else:
                logging.info(f"Injecting APEX package: {dir_name}")
                return False
        else:
            logging.info(f"Skipping APEX package: {dir_name} due to disabled APEX injection.")
            return True
    return False


def handle_library_package(package_path, dir_name, uuid_dir, aosp_path, out_dir, included_package_statistics):
    """
    Handles the injection of library packages.

    :param package_path: str - path to the package directory.
    :param dir_name: str - name of the package directory.
    :param uuid_dir: str - unique identifier for the package.
    :param aosp_path: str - path to AOSP root folder.
    :param out_dir: str - output directory for injected packages.
    :param included_package_statistics: dict - statistics of included packages.
    """
    if not PRE_INJECTOR_CONFIG["DISABLE_NATIVE_LIBRARY_INJECTION"]:
        framework_lib_path = os.path.join(aosp_path, f"{out_dir}libs/", f"{dir_name}_{uuid_dir}")
        logging.debug(f"Copying library package: {package_path} to {framework_lib_path}")
        shutil.copytree(package_path, framework_lib_path, dirs_exist_ok=True)
        included_package_statistics["libs"].append(dir_name)
    else:
        logging.debug(f"Native library injection disabled for package: {dir_name}")
    return included_package_statistics


def handle_apex_package(package_path, dir_name, uuid_dir, aosp_path, out_dir, included_package_statistics, lunch_target, aosp_version):
    """
    Handles the injection of APEX packages.

    :param package_path: str - path to the package directory.
    :param dir_name: str - name of the package directory.
    :param uuid_dir: str - unique identifier for the package.
    :param aosp_path: str - path to AOSP root folder.
    :param out_dir: str - output directory for injected packages.
    :param included_package_statistics: dict - statistics of included packages.
    :param lunch_target: str - AOSP build argument to select the build arch.
    """
    apex_file_path = get_apex_file(package_path)
    package_dir_name = str(os.path.basename(package_path).lower())
    modules_path = str(os.path.join(aosp_path, f"{out_dir}apex/", package_dir_name, uuid_dir))
    logging.debug(f"Copying APEX package: {package_path} to {modules_path}")
    shutil.copytree(package_path, modules_path, dirs_exist_ok=True)
    if PRE_INJECTOR_CONFIG["ALLOW_APEX_REPACKING_IN_PRE_INJECTOR"]:
        is_success, log_message = repackage_apex_file(aosp_path, apex_file_path, lunch_target, aosp_version)
        if is_success:
            logging.debug(f"Repackaged APEX package: {apex_file_path} successfully.")
            included_package_statistics["apex"].append(dir_name)
        else:
            logging.error(f"APEX repacking error: {log_message}. Exiting.")
            exit(1)
    return included_package_statistics


def handle_app_package(package_path, dir_name, uuid_dir, out_dir, included_package_statistics):
    """
    Handles the injection of app packages.

    :param package_path: str - path to the package directory.
    :param dir_name: str - name of the package directory.
    :param uuid_dir: str - unique identifier for the package.
    :param out_dir: str - output directory for injected packages.
    :param included_package_statistics: dict - statistics of included packages.
    """
    app_modules_path = os.path.join(out_dir, "apps", f"{dir_name}_{uuid_dir}")
    logging.debug(f"Moving app package: {dir_name} from {package_path} to {app_modules_path}")
    shutil.copytree(package_path, app_modules_path, dirs_exist_ok=True)
    included_package_statistics["apps"].append(dir_name)
    return included_package_statistics



def inject_meta_files(aosp_path, aosp_version, package_name_list):
    """
    Replaces the original base_system.mk of the AOSP source code with a modified version.
    The modified version includes all the packages to inject into the build process.
    Meta file contain the package names to inject into the aosp source code. Base files are the original files
    from the aosp source code.

    :param aosp_path: str -  path to aosp root folder.
    :param aosp_version: str - version of the aosp build.

    """
    for meta_build_filename in META_BUILD_FILENAMES:
        meta_build_path = os.path.join(BUILD_OUT_PATH, meta_build_filename)
        if not os.path.exists(meta_build_path):
            if meta_build_filename == META_BUILD_SYSTEM_FILENAME:
                raise RuntimeError(f"Could not find file: {meta_build_filename} from {meta_build_path}. Somethings wrong.")
        base_filename = get_base_filename(meta_build_filename)
        content = read_and_render_template(meta_build_path, base_filename, aosp_version, package_name_list)
        aosp_base_file_path = os.path.join(aosp_path, BASE_PATH, base_filename)
        out_file_path = os.path.join(BUILD_OUT_PATH, base_filename)
        write_and_copy_file(content, out_file_path, aosp_base_file_path)
        if not os.path.exists(aosp_base_file_path):
            raise RuntimeError(f"AOSP build file does not exist: {aosp_base_file_path}. Something went wrong injecting "
                               f"the packages into the aosp source code.")


def get_aosp_build_command(lunch_target, aosp_version, aosp_root):
    """
    Creates the aosp build command based on the lunch target and aosp version.

    :param aosp_version: str - version of the aosp build.
    :param lunch_target: str - aosp build argument to select the build arch.
    :param aosp_root: str - path to aosp root folder.

    :returns: str - aosp build command.

    """
    logging.info(f"Starting build process for {lunch_target}... this will take a long time.")

    if lunch_target not in SUPPORTED_LUNCH_TARGETS:
        raise RuntimeError("Unsupported build CPU architecture specified.")

    if aosp_version in ["11", "12"]:
        command = f"bash -c 'cd {aosp_root} && source {aosp_root}/build/envsetup.sh " \
                  f"&& lunch {lunch_target} " \
                  "&& m " \
                  "&& m sdk'"
    else:
        command = f"bash -c 'cd {aosp_root} && source {aosp_root}/build/envsetup.sh " \
                  f"&& lunch {lunch_target} " \
                  "&& m '"
    return command


def get_aosp_repo_build_command(aosp_root, lunch_target, aosp_version):
    if aosp_version in ["11"]:
        command = f"bash -c 'cd {aosp_root} && source {aosp_root}/build/envsetup.sh " \
                  f"&& lunch {lunch_target} " \
                  "&& m sdk_repo " \
                  "&& m dist'"
    elif aosp_version in ["12"]:
        command = f"bash -c 'cd {aosp_root} && source {aosp_root}/build/envsetup.sh " \
                  f"&& lunch {lunch_target} " \
                  "&& m sdk_repo " \
                  "&& m emu_img_zip'"
    else:
        command = f"bash -c 'cd {aosp_root} && source {aosp_root}/build/envsetup.sh " \
                  f"&& lunch {lunch_target} " \
                  "&& m emu_img_zip'"
    return command


def get_rebuild_jar_modules_command(aosp_root, lunch_target, included_package_name_list):
    """
    Creates the aosp build command to rebuild the jar modules.

    :param aosp_root: str - path to aosp root folder.
    :param lunch_target: str - aosp build argument to select the build arch.
    :param included_package_name_list: list(str) - list of included package names.

    :returns: list(str) - aosp build commands to rebuild the jar modules.

    """
    command_list = []
    for jar_module_name in included_package_name_list:
        if "INJECTED_PREBUILT_JAR" in jar_module_name:
            command = f"bash -c 'cd {aosp_root} && source {aosp_root}/build/envsetup.sh " \
                      f"&& lunch {lunch_target} "
            command += f"&& mmm packages/apps/{jar_module_name} '"
            command_list.append(command)
    return command_list


def execute_build_command(firmware_id, lunch_target, command, aosp_root_path):
    """
    Start the aosp build process. Pack all Android images with ("m emu_img_zip"). Copy the artefacts to the
    local image folder.

    :param lunch_target: str - aosp build argument to select the build arch.
    :param firmware_id: str - object-id of the firmware
    :param command: str - aosp build command to execute.
    :param aosp_root_path: str - root path of the AOSP source code.

    """
    current_directory = os.path.dirname(os.path.realpath(__file__))
    os.chdir(aosp_root_path)
    try:
        firmware_id = re.sub(r'\W+', '', firmware_id)
        lunch_target = re.sub(r'\W+', '', lunch_target)
        unique_id = uuid.uuid4()
        log_name = str(unique_id) + "_" + firmware_id + "_" + lunch_target + ".log"
        log_path = os.path.join(BUILD_OUT_PATH, log_name)
        logging.info(f"Executing command: {command}")
        logging.info(f"Build logs will be written to: {log_path}")
        with open(log_path, "w") as outfile:
            subprocess.run(command, shell=True, check=True, stdout=outfile, stderr=outfile)
    except subprocess.CalledProcessError as err:
        logging.error(f"Got an error building firmware: {err}")
        raise err
    os.chdir(current_directory)


def delete_unlisted_directories(directory_path, directory_names):
    """
    Deletes directories that are not listed in directory_names.

    :param directory_path: str - path of the parent directory.
    :param directory_names: list - list of directory names to keep.

    """
    for dir_name in os.listdir(directory_path):
        if dir_name not in directory_names:
            full_dir_path = os.path.join(directory_path, dir_name)
            if os.path.isdir(full_dir_path):
                shutil.rmtree(full_dir_path)
                logging.debug(f"Cleanup: Directory {full_dir_path} has been removed.")


def clear_packages(aosp_packages_path):
    """
    Deletes injected apk packages and .txt and .zip files from the aosp source code.

    :param aosp_packages_path:

    """
    logging.debug(f"Clearing packages from {aosp_packages_path}")
    try:
        delete_unlisted_directories(aosp_packages_path, PRE_INJECTOR_CONFIG["AOSP_DEFAULT_PACKAGE_NAMES"])
        txt_files = glob.glob(os.path.join(aosp_packages_path, '*.txt'))
        zip_files = glob.glob(os.path.join(aosp_packages_path, '*.zip'))
        for file in txt_files + zip_files:
            os.remove(file)
    except Exception as err:
        logging.error(err)
    logging.debug("Cleared app packages and .txt and .zip files from aosp source code.")


def clear_base_files(aosp_path, aosp_version):
    """
    Overwrites the base files from the aosp source code with the empty template.

    :param aosp_path: str - path to the root of the aosp source code.
    :param aosp_version: str - Android (AOSP) version

    """
    try:
        for base_filename in BASE_FILENAMES:
            logging.debug(f"Clearing base file: {base_filename} for version {aosp_version}")
            aosp_base_file_path = os.path.join(aosp_path, BASE_PATH, base_filename)
            if os.path.exists(aosp_base_file_path):
                template_folder_abs_path = get_template_folder_path()
                environment = Environment(loader=FileSystemLoader(str(template_folder_abs_path)))
                template = environment.get_template(base_filename)
                base_file_content = template.render(package_name_list=[])
                with open(aosp_base_file_path, 'w') as base_file:
                    base_file.write(base_file_content)
            else:
                logging.warning(f"Could not find base file in template folder: {aosp_base_file_path}")
    except Exception as err:
        logging.error(err)
        traceback.print_exc()
        pass


def clear_intermediate_files(aosp_path):
    out_dir = os.path.join(aosp_path, MODULE_BASE_INJECT_DIR)
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
        logging.debug(f"Removed {out_dir} from aosp source code.")
    else:
        RuntimeError(f"Could not find directory: {out_dir} in aosp source code.")


def clear_extracted_packages():
    """
    Reset the build environment by removing the extracted packages directory.
    """
    try:
        extracted_packages_path = os.path.join(BUILD_OUT_PATH, PACKAGE_EXTRACTION_DIR_NAME)
        if os.path.exists(extracted_packages_path):
            shutil.rmtree(extracted_packages_path)
            logging.debug(f"Removed {extracted_packages_path}")
    except Exception as err:
        logging.error(err)


def reset_post_injection_files(aosp_path):
    # TODO: Implement reset of post injection files
    build_image_file_path = os.path.join(aosp_path, "build/make/tools/releasetools/build_image.py")
    template_goldfish_mk_path = os.path.join(TEMPLATE_FOLDER, "goldfish_tools/Android.mk")
    logging.info(f"Resetting post injection files for {build_image_file_path} with {template_goldfish_mk_path}")
    try:
        shutil.copyfile(build_image_file_path, template_goldfish_mk_path)
    except Exception as err:
        logging.error(err)


def replace_build_image_file(aosp_path):
    build_image_file_path = os.path.join(aosp_path, "build/make/tools/releasetools/build_image.py")
    template_build_image_path = os.path.join(TEMPLATE_FOLDER, "build_image.py")
    logging.info(f"Restore build image file {build_image_file_path} with {template_build_image_path}")
    try:
        shutil.copyfile(template_build_image_path, build_image_file_path)
    except Exception as err:
        logging.error(err)

def clear_environment(aosp_path, aosp_packages_apps_path, aosp_version):
    """
    Reverts the build environment
    Returns:

    """
    logging.debug("Clearing injection environment...")
    clear_packages(aosp_packages_apps_path)
    clear_intermediate_files(aosp_path)
    clear_extracted_packages()
    clear_base_files(aosp_path, aosp_version)
    if aosp_version and int(aosp_version) == 12:
        replace_build_image_file(aosp_path)


def fetch_build_files(firmware_id, cookies, fmd_url, extract_destination_folder):
    """
    Main wrapper routine to download and extract firmware build files for aosp.
    Args:
        firmware_id: str - id of the firmware packages to fetch.
        cookies: cookie jar for requests.
        fmd_url: str - url to the main fmd backend
        extract_destination_folder: str - path to extract the app packages to.

    """
    logging.debug(f"Process firmware: {firmware_id}")
    is_successful = False
    max_attempts = 5
    while not is_successful and max_attempts > 0:
        try:
            max_attempts -= 1
            zip_file_path = download_firmware_build_files(fmd_url,
                                                          firmware_id,
                                                          cookies,
                                                          extract_destination_folder)
            tmp_path = os.path.join(BUILD_OUT_PATH, PACKAGE_EXTRACTION_DIR_NAME)
            os.makedirs(tmp_path, exist_ok=True)
            extract_zip(zip_file_path, tmp_path)
            os.remove(zip_file_path)
            is_successful = True
        except Exception as err:
            logging.error(f"Error fetching firmware build files: {err}")
            exit(-1)
    logging.debug(f"Completed firmware build file download to {extract_destination_folder}")


def parse_arguments():
    """
    Parse the command line arguments.
    """
    parser = argparse.ArgumentParser(prog='fmd_build_injector',
                                     description="A cli tool to download and store build files from FirmwareDroid.")
    parser.add_argument("-s", "--aosp-path", type=str, default="/home/ubuntu/aosp/aosp12/",
                        help="Specifies the path to the root of the aosp source code.")
    parser.add_argument("-f", "--fmd-url", type=str, default=None, required=True,
                        help="HTTP/HTTPS url to the FMD instance to grab the packages."
                             "Example: https://firmwaredroid.cloudlab.zhaw.ch")
    parser.add_argument("-u", "--fmd-username", type=str, default=None, required=True,
                        help="Username for the authentication to the fmd service.")
    parser.add_argument("-d", "--docker-repo-username", type=str, default=None, required=True,
                        help="Username for the authentication to the docker registry.")
    parser.add_argument("-r", "--docker-repo-url", type=str, default=None,
                        help="Specifies the url to a docker registry, where the emulator images will be pushed to.")
    parser.add_argument("-a", "--arch", type=str, default="x86_64",
                        help='Specifies the CPU architecture ("arm64" or "x86_64") to use for the build process.')
    parser.add_argument("-e", "--version", type=str, default="12",
                        help='Specifies Android version to build for. Example: "12"')
    parser.add_argument("-n", "--skip-filtering", action='store_true', default=False,
                        help='If set, the filtering of the packages will be skipped.')
    parser.add_argument("-z", "--reset-aosp", action='store_true', default=False,
                        help='If set, the aosp build environment will be reset.')
    parser.add_argument("-c", "--skip-clean", action='store_true', default=False,
                        help='If set, skips the cleanup of the aosp build environment.')
    parser.add_argument("-p", "--pk-filter", type=str, default=None, help='Set a specific aecs job id '
                                                                          'to process. Other jobs will be ignored '
                                                                          'when set.')
    parser.add_argument("-m", "--pre_injector_config",
                        type=str,
                        default="./device_configs/development/pre_injector_config_v1.json",)
    parser.add_argument("-i", "--post_injector_config",
                        type=str,
                        default="./device_configs/development/post_injector_config_v1.json",)
    args = parser.parse_args()

    if not (args.fmd_url.startswith("https://") or args.fmd_url.startswith("http://")):
        logging.error(f"Error: Incorrect FMD URL: {args.fmd_url}")
        exit(1)

    return args


def get_passwords(args):
    """
    Get the passwords for the FirmwareDroid and Docker registry.

    :param args:

    :returns: tuple - tuple of the FirmwareDroid and Docker registry passwords.

    """
    fmd_password = os.getenv('FMD_PASSWORD')
    if not fmd_password:
        fmd_password = getpass(f"Please enter your FirmwareDroid password ({args.fmd_username}): ")

    docker_repo_password = os.getenv('DOCKER_REPO_PASSWORD')
    if not docker_repo_password:
        docker_repo_password = getpass(f"Please enter your Docker registry password ({args.docker_repo_username}): ")

    return fmd_password, docker_repo_password


def fetch_firmware_ids(args, fmd_password, csrf_cookie):
    """
    Get the firmware ids from the FirmwareDroid service.

    args: dict - command line arguments.
    fmd_password: str - password for the FirmwareDroid service.
    csrf_cookie: cookie jar for requests.

    :returns: tuple - tuple of the firmware ids and cookies.

    """
    graphql_url = get_graphql_url(args.fmd_url)
    cookies = authenticate_fmd(graphql_url, args.fmd_username, fmd_password, csrf_cookie)
    firmware_id_list = get_firmware_ids(graphql_url, cookies, args.arch, args.pk_filter)
    logging.info(f"Got {len(firmware_id_list)} firmware ids to process...")
    return firmware_id_list, cookies


def upload_build_artefact(repo_url, username, password, artefact_path, filename):
    """
    Uploads the build artefact to the docker registry. Retries the upload process if it fails.

    :param repo_url: str - URL to the docker registry.
    :param username: str - username for the docker registry.
    :param password: str - password for the docker registry.
    :param artefact_path: str - path to the build artefact.
    :param filename: str - name of the build artefact.

    :returns: bool - True if the upload was successful.

    """
    is_upload_success = False
    max_attempts = 5
    while not is_upload_success and max_attempts > 0:
        logging.debug(f"Uploading image {filename} to repo {repo_url}.")
        try:
            is_upload_success, download_url = upload_image_as_raw(repo_url,
                                                    username,
                                                    password,
                                                    artefact_path,
                                                    filename)
        except Exception as err:
            logging.error(f"Error uploading image: {err}")
        max_attempts -= 1
        if not is_upload_success:
            logging.error(f"Failed to upload image {filename} to repo. Retrying...{max_attempts}")
    return is_upload_success, download_url

def setup_firmware_logger(firmware_id):
    """
    Sets up a new log file for the given firmware_id and redirects logging output to it.
    Prevents logs from showing in stdout.
    """
    uuid_filename = str(uuid.uuid4())
    log_file = os.path.join(BUILD_OUT_PATH, f"{uuid_filename}_{firmware_id}_process.log")
    logging.info(f"Logging redirected for id: {firmware_id} to file: {log_file}")
    logger = logging.getLogger()
    logger.handlers.clear()  # Remove all existing handlers, including stdout

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return file_handler


def write_json_output(result, output_file):
    """
    Writes the build result to a JSON file.

    :param result: dict - The result to write to the JSON file.
    :param output_file: str - Path to the JSON output file.
    """

    # Append the result to the JSON file
    try:
        with open(output_file, "r") as file:
            data = json.load(file)
    except FileNotFoundError:
        data = []
    except Exception as err:
        logging.error(f"Error writing to file: {err}")
        data = []

    data.append(result)
    try:
        with open(output_file, "w") as file:
            json.dump(data, file, indent=4)
            file.write("\n")  # Add a newline
    except Exception as err:
        logging.error(f"Error writing to file: {err}")


def process_firmware_ids(args, firmware_id_list, cookies, docker_repo_password):
    aosp_packages_abs_path = os.path.join(args.aosp_path, AOSP_PACKAGES_APPS_PATH)
    aosp_version = args.version
    if args.arch == SUPPORTED_ARCHITECTURES[0]:
        lunch_target = SUPPORTED_LUNCH_TARGETS[0]
    else:
        test = os.environ.get("FMD_PHONE64_TEST_BUILD") == "True"
        if aosp_version in ["11", "12"]:
            lunch_target = SUPPORTED_LUNCH_TARGETS[1]
            if test:
                lunch_target = SUPPORTED_LUNCH_TARGETS[2]
        elif aosp_version in ["13"]:
            lunch_target = SUPPORTED_LUNCH_TARGETS[2]
        elif aosp_version in ["14"]:
            lunch_target = SUPPORTED_LUNCH_TARGETS[3]
        else:
            raise RuntimeError(f"Unsupported Android version: {args.version}")
    logging.debug(f"Downloading and extracting app packages to: {aosp_packages_abs_path}")
    failed_firmware_ids = []
    succeed_firmware_ids = []
    download_url_list = []
    clear_environment(args.aosp_path, aosp_packages_abs_path, aosp_version)
    logging.info(f"Building for lunch target: {lunch_target} with aosp version: {aosp_version}")
    for firmware_id in tqdm(firmware_id_list):
        try:
            logging.info(f"Start fetching build files for firmware-id: {firmware_id}")
            fetch_build_files(firmware_id, cookies, args.fmd_url, BUILD_OUT_PATH)
            logging.debug(f"Start emulator image build process for firmware-id: {firmware_id}")

            file_handler = setup_firmware_logger(firmware_id)
            try:
                logging.getLogger().addHandler(file_handler)
                start_time = time.time()  # Record the start time
                is_build_success = start_aosp_build(args.aosp_path,
                                                    AOSP_PACKAGES_APPS_PATH,
                                                    firmware_id=firmware_id,
                                                    lunch_target=lunch_target,
                                                    aosp_version=args.version,
                                                    skip_filtering=args.skip_filtering,
                                                    cookies=cookies)
                end_time = time.time()
                duration = end_time - start_time

                status = "success" if is_build_success else "failure"
                result = {
                    "hostname": os.uname()[1],
                    "firmware_id": firmware_id,
                    "duration": round(duration, 2),
                    "status": status
                }
                write_json_output(result, PATH_BUILD_FILE_LOG)

                logging.info(f"Build process for firmware-id: {firmware_id} took {duration:.2f} seconds.")
            finally:
                logging.getLogger().removeHandler(file_handler)
                file_handler.close()
                if os.environ.get("FMD_DEBUG") == "True":
                    setup_logger(logging.DEBUG)
                else:
                    setup_logger()

            if is_build_success:
                logging.info(f"Build process for firmware-id: {firmware_id} was successful.")
                emulator_image_zip_path = get_emulator_image_path(args.aosp_path, lunch_target, args.version)
                filename = f"{firmware_id}_v{args.version}_{lunch_target}.zip".replace('-', '_')
                is_upload_success, download_url = upload_build_artefact(args.docker_repo_url,
                                                          args.docker_repo_username,
                                                          docker_repo_password,
                                                          emulator_image_zip_path,
                                                          filename)
                if is_upload_success:
                    logging.info(f"Upload of firmware-id: {firmware_id} was successful.")
                    with open("docker_images.txt", "a") as file:
                        file.write(f"{filename.replace('.zip', '')}\n")
                    succeed_firmware_ids.append(firmware_id)
                    download_url_list.append(download_url)
                else:
                    raise RuntimeError(f"Upload process for firmware-id: {firmware_id} failed.")
            else:
                raise RuntimeError(f"Build process for firmware-id: {firmware_id} failed.")
        except Exception as err:
            logging.error(f"Got an error processing firmware-id: {firmware_id}. Error: {err}")
            traceback.print_exc()
            traceback.print_stack()
            failed_firmware_ids.append(firmware_id)
        finally:
            if not args.skip_clean:
                clear_environment(args.aosp_path, aosp_packages_abs_path, aosp_version)

    if len(failed_firmware_ids) > 0:
        logging.error(f"Failed to build {len(failed_firmware_ids)} of the following firmware ids: {failed_firmware_ids} for arch: {args.arch}")
    logging.info(f"Successfully built {len(succeed_firmware_ids)} of the following firmware ids: {succeed_firmware_ids} for arch: {args.arch}")
    logging.info(f"Download URLs: {download_url_list}")



def set_skipped_module_names():
    global SKIPPED_MODULE_NAMES
    blocked_module_names = [EXTRACTION_ALL_FILES_DIR_NAME]
    blocked_module_names.extend(PRE_INJECTOR_CONFIG["AOSP_DEFAULT_PACKAGE_NAMES"])
    blocked_module_names.extend(PRE_INJECTOR_CONFIG["BLACKLISTED_ANDROID_12_EMULATOR_SHARED_LIBRARIES"])
    blocked_module_names.extend(PRE_INJECTOR_CONFIG["HOST_PACKAGES_LIST"])
    blocked_module_names.extend(PRE_INJECTOR_CONFIG["ANDROID_HARDWARE_MODULE_LIST"])
    blocked_module_names.extend(PRE_INJECTOR_CONFIG["DISALLOWED_APK_PACKAGES"])

    for libray in PRE_INJECTOR_CONFIG["SKIPPED_LIBRARIES"]:
        blocked_module_names.append(libray.replace(".so", ""))
    SKIPPED_MODULE_NAMES = blocked_module_names

def main():
    logging.info("=======================BUILD INJECTOR=======================")
    args = parse_arguments()
    if args.reset_aosp:
        aosp_packages_apps_abs_path = os.path.join(args.aosp_path, AOSP_PACKAGES_APPS_PATH)
        clear_environment(args.aosp_path, aosp_packages_apps_abs_path, args.version)
        logging.info("Reset aosp build environment.")
        exit(0)
    if args.arch not in SUPPORTED_ARCHITECTURES:
        raise RuntimeError(f"Unsupported architecture: {args.arch}. Supported architectures: {SUPPORTED_ARCHITECTURES}")

    if (not os.path.exists(args.aosp_path)
            or not os.path.exists(args.pre_injector_config)
            or not os.path.exists(args.post_injector_config)):
        raise RuntimeError(f"Files or directories do not exist")


    pre_injector_config, post_injector_config = load_configs(args.pre_injector_config, args.post_injector_config)
    global PRE_INJECTOR_CONFIG
    global POST_INJECTOR_CONFIG
    global PRE_INJECTOR_CONFIG_PATH
    global POST_INJECTOR_CONFIG_PATH
    PRE_INJECTOR_CONFIG = pre_injector_config
    POST_INJECTOR_CONFIG = post_injector_config
    PRE_INJECTOR_CONFIG_PATH = args.pre_injector_config
    POST_INJECTOR_CONFIG_PATH = args.post_injector_config
    PRE_INJECTOR_CONFIG["PRE_INJECTOR_CONFIG_PATH"] = args.pre_injector_config
    logging.info(f"Pre-injector config: {PRE_INJECTOR_CONFIG_PATH}, Post-injector config: {POST_INJECTOR_CONFIG_PATH}")
    set_skipped_module_names()
    fmd_password, docker_repo_password = get_passwords(args)
    csrf_cookie = get_csrf_token(args.fmd_url)
    firmware_id_list, cookies = fetch_firmware_ids(args, fmd_password, csrf_cookie)
    process_firmware_ids(args, firmware_id_list, cookies, docker_repo_password)
    logging.info("===============================================================")


if __name__ == "__main__":
    main()
