"""
A command-line tool that downloads files related to the build process of an Android firmware image and stores them
on disk. Directly extract the downloaded zip content.
"""
import argparse
import json
import os.path
import re
import traceback
import uuid
import logging
import shutil
import subprocess
import signal
import glob
from pathlib import Path
import concurrent.futures

from tqdm import tqdm
from jinja2 import Environment, FileSystemLoader
from getpass import getpass
import time
from urllib.parse import urlparse
from aosp_apex_injector import repackage_apex_file
from aosp_post_build_injector import start_post_build_injector
from common import extract_zip, load_configs, get_aosp_build_out_dir, upload_build_artefact
from config import *
from fmd_backend_requests import download_firmware_build_files, get_csrf_token, authenticate_fmd, \
    get_firmware_ids, get_graphql_url
from json_writer import write_json_output, write_text_output
from setup_logger import setup_logger


SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
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







def fix_missing_file(aosp_path, aosp_version):
    """
    Creates the displayconfig file to fix an aosp error on Android 14
    """
    if aosp_version in ["14"]:
        # out / target / product / emulator64_arm64 / vendor / etc / displayconfig
        display_config_path = os.path.join(aosp_path, 'out/target/product/emulator64_arm64/vendor/etc/')
        os.makedirs(display_config_path, exist_ok=True)
        file_path = os.path.join(display_config_path, "displayconfig")
        try:
            with open(file_path) as f:
                f.seek(0)
        except Exception:
            logging.warning(f"Could not create displayconfig: {file_path}")


def copy_logs_by_prefix(source_dir: str, output_dir: str, prefix: str) -> None:
    """
    Copies all .log files starting with a specific prefix from source_dir
    (excluding subfolders) to output_dir.
    """
    src = Path(source_dir)
    dest = Path(output_dir)

    # Ensure the output directory exists, create it if it doesn't
    dest.mkdir(parents=True, exist_ok=True)

    # Construct the glob pattern: prefix followed by anything, ending in .log
    # Using .glob() instead of .rglob() ensures it stays in the top-level folder
    glob_pattern = f"{prefix}*.log"

    for file_path in src.glob(glob_pattern):
        if file_path.is_file():  # Safety check to ensure it's a file, not a directory
            shutil.copy(file_path, dest / file_path.name)
            logging.info(f"Copied: {file_path.name}")


def has_extracted_partitions(all_files_dir_path):
    """
    Returns True if all target directories exist in the root,
    otherwise returns False. Logs the existence of each directory.
    """
    # Removed leading slashes so os.path.join works as intended
    target_dirs = ['system']

    all_exist = True

    for d in target_dirs:
        full_path = os.path.join(all_files_dir_path, d)

        if os.path.isdir(full_path):
            logging.info(f"FOUND: Directory exists -> {full_path}")
        else:
            logging.warning(f"MISSING: Directory not found -> {full_path}")
            all_exist = False

    return all_exist


def start_aosp_build(aosp_path, aosp_packages_path, firmware_id, lunch_target, aosp_version, skip_filtering, cookies, tag=None):
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
    overwrite_partition_size(aosp_path)
    if aosp_version in ["11", "12", "12.1"]:
        blueprint_build_command = f"bash -c 'cd {aosp_path} && source {aosp_path}/build/envsetup.sh && lunch {lunch_target} && m clean && m blueprint_tools otatools debugfs_static bssl debugfs'"
    else:
        blueprint_build_command = f"bash -c 'cd {aosp_path} && source {aosp_path}/build/envsetup.sh && lunch {lunch_target} && m clean && m blueprint_tools otatools debugfs_static apexer deapexer avbtool bssl debugfs'"
    try:
        execute_build_command(aosp_path, firmware_id, blueprint_build_command, aosp_path, log_name="host_tools_build_")
    except Exception as err:
        if aosp_version in ["14"]:
            lunch_target = SUPPORTED_LUNCH_TARGETS[2]
            logging.warning(f"Downgrading lunch target to {lunch_target}")
            blueprint_build_command = f"bash -c 'cd {aosp_path} && source {aosp_path}/build/envsetup.sh && lunch {lunch_target} && m clean && m blueprint_tools otatools debugfs_static apexer deapexer avbtool bssl debugfs'"
            execute_build_command(aosp_path, firmware_id, blueprint_build_command, aosp_path, log_name="host_tools_build_")
        else:
            raise err
    logging.debug(f"Environment setup for {lunch_target} completed. Moving packages to aosp source code next.")

    try:
        move_txt_files(EXTRACTED_PACKAGES_PATH, BUILD_OUT_PATH)

        if PRE_INJECTOR_CONFIG["ENABLE_INJECTION"]:
            included_package_statistics = move_packages_to_aosp(aosp_path, EXTRACTED_PACKAGES_PATH, lunch_target, aosp_version)
            if PRE_INJECTOR_CONFIG["ENABLE_TOOLBOX_INJECTION"]:
                toolbox_package = add_toolbox_packages_to_aosp(aosp_path)
                included_package_statistics["toolbox"] = toolbox_package
                logging.debug("Add toolbox modules")
        else:
            logging.debug("Skipping package injection as ENABLE_INJECTION is set to False.")
            included_package_statistics = {"apps": [], "libs": [], "apex": [], "toolbox": [], "count": 0}
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
            "pre_injector_duration": round(pre_injector_end_time - pre_injector_start_time, 2)
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
        package_name_list.extend(included_package_statistics["toolbox"])
        inject_meta_files(aosp_path, aosp_version, package_name_list)
        logging.debug(f"Injected meta files into aosp source code: {aosp_path}")
    except Exception as err:
        logging.error(f"Error injecting meta files: {err}")
        traceback.print_exc()
        exit(-1)

    retry_attempts = BUILD_RETRY_COUNT
    while not is_successful and retry_attempts > 0:
        try:
            path_post_builder_args = os.path.join(SCRIPT_DIR, "out/post_builder_args.json")
            if os.path.exists(path_post_builder_args):
                logging.info(f"Removing existing post_builder_args.json file: {path_post_builder_args}")
                os.remove(path_post_builder_args)

            target_out_path = get_target_out_path(aosp_path, lunch_target)
            all_extracted_firmware_files_path = os.path.join(EXTRACTED_PACKAGES_PATH, EXTRACTION_ALL_FILES_DIR_NAME)

            has_partitions = has_extracted_partitions(all_extracted_firmware_files_path)
            if not has_partitions:
                raise RuntimeError(f"Missing required partitions in extracted firmware files: {all_extracted_firmware_files_path}. Likely the firmware images was not correctly extracted or AECS file corrupt.")

            post_builder_args_dict = {"aosp_path": aosp_path,
                                      "source_folder_path": all_extracted_firmware_files_path,
                                      "target_out_path": target_out_path,
                                      "lunch_target": lunch_target,
                                      "firmware_id": firmware_id,
                                      "pre_injector_package_list": included_package_statistics["apps"],
                                      "pre_injector_config_path": PRE_INJECTOR_CONFIG_PATH,
                                      "post_injector_config_path": POST_INJECTOR_CONFIG_PATH,
                                      "aosp_version": aosp_version,
                                      "tag": tag
                                      }
            with open(path_post_builder_args, "w", encoding="utf-8") as f:
                json.dump(post_builder_args_dict, f, indent=4)
                logging.info(f"Post builder args written to out/post_builder_args.json")


            fix_missing_file(aosp_path, aosp_version)
            main_build_command = get_aosp_build_command(lunch_target, aosp_version, aosp_path)
            build_start_time = time.time()
            execute_build_command(aosp_path, firmware_id, main_build_command, aosp_path, log_name="main_build")
            build_end_time = time.time()
            included_package_statistics["main_build_duration"] = round(build_end_time - build_start_time, 2)
            logging.info(f"AOSP main build completed successfully. Continuing with post-build injection.")
            logging.info(f"Summary Pre-Injector: {included_package_statistics}")
            delete_image_files(aosp_path, lunch_target)
            package_build_artefacts_command = get_aosp_repo_build_command(aosp_path, lunch_target, aosp_version)
            package_start_time = time.time()
            execute_build_command(aosp_path, firmware_id, package_build_artefacts_command, aosp_path, log_name="packaging")
            package_end_time = time.time()
            included_package_statistics["package_build_artefacts_duration"] = round(package_end_time - package_start_time, 2)
            is_successful = True
        except Exception as err:
            logging.error(err)
            retry_attempts -= 1
    return is_successful


def delete_image_files(aosp_path, lunch_target):
    """
    Delete all *.img files from the build out folder and its PACKAGING subfolder.
    """
    cmd = (
        f"cd {aosp_path} && "
        f"source build/envsetup.sh && "
        f"lunch {lunch_target} && "
        f"cd ${{ANDROID_PRODUCT_OUT:?}} && "
        f"rm -f *.img && "
        f"shopt -s globstar && rm -f obj/PACKAGING/**/*.img emulator/arm64-v8a/*.img"
    )
    logging.info(f"Deleting all *.img files from the build out folder with command: {cmd}")

    process = subprocess.call(cmd, shell=True, executable='/bin/bash')

    if process == 0:
        logging.info("Deleted all *.img files from the build out folder and packaging subdirectories.")
    else:
        logging.error("Failed to delete *.img files. Check if lunch target is correct.")


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
    elif lunch_target == SUPPORTED_LUNCH_TARGETS[3] or lunch_target == SUPPORTED_LUNCH_TARGETS[4]:
        return os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_ARM64_x64_PATH_EMU64A)
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
    if aosp_version in ["11", "12", "12.1"]:
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
    elif aosp_version in ["14"]:
        image_source_path = os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_ARM64_x64_PATH_EMU64A, AOSP_EMU_ZIP_FILENAME)
        if not os.path.exists(image_source_path):
            image_source_path = os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_ARM64_x64_PATH, AOSP_EMU_ZIP_FILENAME_A12_A13)
    elif aosp_version in ["14", "15", "16"]:
        image_source_path = os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_ARM64_x64_PATH_EMU64A, AOSP_EMU_ZIP_FILENAME)

    if not image_source_path or not os.path.exists(image_source_path):
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
                logging.info(f"(Pre-Injector) Removing blacklisted module from meta file {meta_build_path}: {line}")
            else:
                logging.info(f"Allowing module meta in build: {line}")
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
    Calculate the size of directories in bytes.

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


def get_minimal_partition_size():
    """
    Calculates the minimal partition size based on the size of the packages to inject.

    :param aosp_path: str - path to the root of the aosp source code.
    :param aosp_packages_path: str - path to the prebuilt package folder of aosp.

    :returns: int - minimal partition size in bytes.

    """
    #packages_abs_path = os.path.join(aosp_path, aosp_packages_path)
    #approximate_size_packages = get_directory_size(packages_abs_path)
    extracted_files_abs_path = str(os.path.abspath(EXTRACTED_PACKAGES_PATH))
    approximate_size = get_directory_size(extracted_files_abs_path)
    logging.info(f"Approximate_size size all extracted files: {approximate_size}")
    default_size = 4294967296  # 4GB
    overhead_gb = 1073741824 * 3  # 3GB
    while default_size < (approximate_size + overhead_gb):
        default_size += overhead_gb
        logging.info(f"Increasing partition size to: {default_size} Approximate bytes of packages "
                      f"to inject is: {approximate_size}")
    logging.info(f"Final partition size set to: {default_size} bytes for approximate package size of: {approximate_size} bytes")
    return default_size


def overwrite_partition_size(aosp_path):
    """
    Overwrites the partition size in the aosp source code.

    :param aosp_path: str - path to the root of the aosp source code.
    :param aosp_packages_path: str - path to the prebuilt package folder of aosp.

    """
    minimal_partition_size = get_minimal_partition_size()
    super_partition_size = minimal_partition_size + 8388608  # 8MB
    dynamic_partition_size = minimal_partition_size

    board_config_file_path_list = [
        os.path.join(aosp_path, "build/make/target/board/BoardConfigEmuCommon.mk"),
        os.path.join(aosp_path, "build/make/target/board/BoardConfigGsiCommon.mk"),
        os.path.join(aosp_path, "device/generic/goldfish/board/BoardConfigCommon.mk"),
        os.path.join(aosp_path, "device/generic/goldfish/emulator64_arm64/BoardConfig.mk"),
        os.path.join(aosp_path, "device/generic/goldfish/emu64a/BoardConfig.mk"),
    ]
    for board_config_file_path in board_config_file_path_list:
        if not os.path.exists(board_config_file_path):
            logging.info(f"Board config file not found, skipping partition size overwrite for: {board_config_file_path}")
            continue
        with open(board_config_file_path, 'r') as base_file:
            lines = base_file.readlines()
        for i, line in enumerate(lines):
            if "BOARD_SUPER_PARTITION_SIZE" in line:
                lines[i] = f"  BOARD_SUPER_PARTITION_SIZE := {super_partition_size}\n"
                logging.info(f"Overwriting BOARD_SUPER_PARTITION_SIZE size: {super_partition_size} in {board_config_file_path}")
            if "BOARD_GSI_DYNAMIC_PARTITIONS_SIZE" in line:
                lines[i] = f"  BOARD_GSI_DYNAMIC_PARTITIONS_SIZE := {dynamic_partition_size}\n"
                logging.info(f"Overwriting BOARD_GSI_DYNAMIC_PARTITIONS_SIZE size: {super_partition_size} in {board_config_file_path}")
            if "BOARD_EMULATOR_DYNAMIC_PARTITIONS_SIZE" in line:
                lines[i] = f"  BOARD_EMULATOR_DYNAMIC_PARTITIONS_SIZE := {dynamic_partition_size}\n"
                logging.info(f"Overwriting BOARD_EMULATOR_DYNAMIC_PARTITIONS_SIZE size to:  {minimal_partition_size} in {board_config_file_path} for emulator")
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
    included_package_statistics = {"apps": [], "libs": [], "apex": [], "count": 0, "skipped_apps": [], "skipped_libs": [], "skipped_apex": [], "toolbox": []}
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


def inject_ca_certificate(aosp_path):
    """
    Injects a custom CA certificate into the AOSP source code.

    :param aosp_path: str - path to AOSP root folder.
    """
    certificate_path_list = PRE_INJECTOR_CONFIG.get("CA_CERTIFICATE_PATH_LIST", [])
    for certificate_path in certificate_path_list:
        if not os.path.isfile(certificate_path):
            logging.error(f"CA certificate file not found: {certificate_path}. Skipping injection.")
            return

        certs_dir = os.path.join(aosp_path, "system/ca-certificates/files")
        if not os.path.exists(certs_dir):
            raise Exception(f"CA certificate directory not found: {certs_dir}")

        shutil.copy2(certificate_path, certs_dir)
        logging.info(f"Injected CA certificate into AOSP: {certificate_path} to {certs_dir}")


def inject_toolbox_packages_to_aosp(aosp_path):
    """
    Moves custom AOSP modules from the "toolbox" folder to the AOSP source code. These modules are not
    filtered and always injected when activated. The toolbox modules contain custom tools for debugging,
    analysis, or additional functionalities that enhance the AOSP build.
    """
    toolbox_packages_list = PRE_INJECTOR_CONFIG.get("TOOLBOX_PACKAGE_LIST", [])
    toolbox_injected_list = []
    for module_dict in toolbox_packages_list:
        is_enabled = module_dict["enable"]
        if not is_enabled:
            continue
        toolbox_dir = module_dict["path"]
        module_name = module_dict["name"]
        if not os.path.exists(toolbox_dir):
            logging.debug(f"No toolbox directory found at {toolbox_dir}. Skipping toolbox injection.")
            return []
        dir_name = os.path.basename(toolbox_dir)
        dst_dir = str(os.path.join(aosp_path, MODULE_BASE_INJECT_DIR, dir_name))
        shutil.copytree(toolbox_dir, dst_dir, dirs_exist_ok=True)
        if module_name == "lldb-server_toolbox":
            lldb_path = os.path.join(aosp_path, PRE_INJECTOR_CONFIG["LLDB_BINARY_PATH"])
            lldb_file_name = str(os.path.basename(lldb_path))
            dst_file_path = os.path.join(dst_dir, lldb_file_name)
            shutil.copyfile(lldb_path, dst_file_path)
            logging.info(f"Copied LLDB binary from {lldb_path} to {dst_file_path}")
        logging.info(f"Injected toolbox packages from {toolbox_dir} into AOSP source code at {dst_dir}.")
        toolbox_injected_list.append(module_name)
    return toolbox_injected_list


def add_toolbox_packages_to_meta_file(toolbox_list):
    """
    Add the toolbox packages to the system meta build file to ensure they are included in the build.
    """
    system_meta_build_path = os.path.join(BUILD_OUT_PATH, META_BUILD_SYSTEM_FILENAME)
    if not os.path.exists(system_meta_build_path):
        logging.error(f"Could not inject Toolbox packages. System meta build directory not found: {system_meta_build_path}")
    with open(system_meta_build_path, 'a') as system_meta_build_file:
        logging.info(f"Adding toolbox packages to system meta build: {system_meta_build_path}")
        for module_name in toolbox_list:
            system_meta_build_file.write(f'    {module_name} \\\n')


def add_toolbox_packages_to_aosp(aosp_path):
    """
    Moves custom AOSP modules from the "toolbox" folder to the AOSP source code. These modules are not
    filtered and always injected when activated. The toolbox modules contain custom tools for debugging,
    analysis, or additional functionalities that enhance the AOSP build.
    """
    inject_ca_certificate(aosp_path)
    toolbox_list = []
    aosp_packages = POST_INJECTOR_CONFIG.get("TOOLBOX_AOSP_PACKAGES", []) # Adding tools from AOSP
    toolbox_list.extend(aosp_packages)
    toolbox_list.extend(inject_toolbox_packages_to_aosp(aosp_path))
    add_toolbox_packages_to_meta_file(toolbox_list)
    return toolbox_list

def process_package(package_path, dir_name, aosp_path, out_dir, included_package_statistics, lunch_target, aosp_version):
    """
    Processes a single package directory and moves it to the appropriate location.

    :param package_path: str - path to the package directory.
    :param dir_name: str - name of the package directory.
    :param aosp_path: str - path to AOSP root folder.
    :param out_dir: str - output directory for injected packages.
    :param included_package_statistics: dict - statistics of included packages.
    :param lunch_target: str - AOSP build argument to select the build arch.
    :param aosp_version: str - AOSP version

    :returns: dict - updated statistics of included packages.
    """
    uuid_dir = str(uuid.uuid4())
    if is_package_skipped(dir_name, package_path):
        logging.info(f"(Pre-Injector) Skipping package: {dir_name}")
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
    if dir_name_cleaned in SKIPPED_MODULE_NAMES or any(
            (match := keyword) in dir_name_cleaned for keyword in PRE_INJECTOR_CONFIG["BLACKLISTED_KEYWORDS"]):
        keyword_match = match if "match" in locals() else "SKIPPED_MODULE_NAME"
        logging.info(
            f"Skipping package due to blacklisted keyword: {dir_name_cleaned} - matching keyword: {keyword_match}")
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
                logging.info(f"Skipping APEX package due to disallowed keyword (pre-injector): {dir_name_cleaned}")
                return True
            else:
                logging.info(f"Injecting APEX package (pre-injector): {dir_name}")
                return False
        else:
            logging.info(f"Skipping APEX package (pre-injector): {dir_name} due to disabled APEX injection.")
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
        logging.info(f"Copying library package: {package_path} to {framework_lib_path}")
        shutil.copytree(package_path, framework_lib_path, dirs_exist_ok=True)
        included_package_statistics["libs"].append(dir_name)
    else:
        logging.info(f"Native library injection disabled for package: {dir_name}")
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
    :param aosp_version: str - AOSP version

    :returns: dict - updated statistics of included packages.
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

    if aosp_version in ["11", "12", "12.1"]:
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
    elif aosp_version in ["12", "12.1"]:
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


def execute_build_command(firmware_id, lunch_target, command, aosp_root_path, log_name=""):
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
        log_name = log_name +"build_log_" + firmware_id + "_" + lunch_target + f"{str(unique_id)}.log"
        log_path = os.path.join(BUILD_OUT_PATH, log_name)
        logging.info(f"Executing command: {command}")
        logging.info(f"Build logs will be written to: {log_path}")
        env_variables = os.environ.copy()
        env_variables["ALLOW_NINJA_ENV"] = "true"
        with open(log_path, "w") as outfile:
            subprocess.run(command, shell=True, check=True, stdout=outfile, stderr=outfile, env=env_variables)
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
    template_build_image_path = os.path.join(ROOT_PATH, TEMPLATE_FOLDER, "build_image.py")
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
    #if aosp_version and float(aosp_version) == 12:
        #replace_build_image_file(aosp_path)


def fetch_build_files(firmware_id, cookies, fmd_url, extract_destination_folder, auth_username=None, auth_password=None):
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
                                                          extract_destination_folder,
                                                          auth_username=auth_username,
                                                          auth_password=auth_password)
            tmp_path = os.path.join(BUILD_OUT_PATH, PACKAGE_EXTRACTION_DIR_NAME)
            os.makedirs(tmp_path, exist_ok=True)
            extract_zip(zip_file_path, tmp_path)
            os.remove(zip_file_path)
            is_successful = True
        except Exception as err:
            logging.error(f"Error fetching firmware build files: {err}")
    logging.debug(f"Completed firmware build file download to {extract_destination_folder}")
    return is_successful


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
    parser.add_argument("-t", "--tag", type=str, default=None,
                        help='Optional tag to append to the uploaded artefact filename.')
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
    parser.add_argument("-1", "--build-only-first", type=int, default=None,
                        help='If set, only builds the first X firmware ids returned from the fmd service.')
    parser.add_argument("-i", "--post_injector_config",
                        type=str,
                        default="./device_configs/development/post_injector_config_v1.json",)
    parser.add_argument("-w", "--skip-counter", default=0, type=int, help="Number of firmware samples to skip.")
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




def setup_firmware_logger(firmware_id):
    """
    Sets up a new log file for the given firmware_id and redirects logging output to it.
    Prevents logs from showing in stdout.
    """
    uuid_filename = str(uuid.uuid4())
    log_file = os.path.join(BUILD_OUT_PATH, f"pre_injector_{uuid_filename}_{firmware_id}.log")
    logging.info(f"Logging redirected for id: {firmware_id} to file: {log_file}")
    logger = logging.getLogger()
    logger.handlers.clear()  # Remove all existing handlers, including stdout

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return file_handler


def copy_result_files(results_dir, out_dir, skip_clean):
    if os.path.exists(results_dir):
        shutil.rmtree(results_dir, ignore_errors=True)
    os.makedirs(results_dir, exist_ok=True)
    folders_to_exclude = shutil.ignore_patterns('extracted_packages', 'temp', "acvtool_instrumentation")
    shutil.copytree(
        BUILD_OUT_PATH,
        results_dir,
        dirs_exist_ok=True,
        ignore=folders_to_exclude
    )
    for item in os.listdir(TMP_PATH):
        source_path = os.path.join(TMP_PATH, item)
        if os.path.isfile(source_path):
            shutil.copy(source_path, out_dir)
    extracted_packes_path = os.path.join(results_dir, "extracted_packages")
    if os.path.exists(extracted_packes_path) and not skip_clean:
        shutil.rmtree(extracted_packes_path, ignore_errors=True)
    if not skip_clean:
        shutil.rmtree(BUILD_OUT_PATH, ignore_errors=True)
        logging.info(f"Cleaned path: {BUILD_OUT_PATH}")
    os.makedirs(BUILD_OUT_PATH, exist_ok=True)


def process_firmware_ids(args, firmware_id_list, cookies, docker_repo_password, fmd_password):
    aosp_packages_abs_path = os.path.join(args.aosp_path, AOSP_PACKAGES_APPS_PATH)
    aosp_version = args.version
    if args.arch == SUPPORTED_ARCHITECTURES[0]:
        lunch_target = SUPPORTED_LUNCH_TARGETS[0]
    else:
        if aosp_version in ["11", "12", "12.1"]:
            lunch_target = SUPPORTED_LUNCH_TARGETS[2]
        elif aosp_version in ["13"]:
            lunch_target = SUPPORTED_LUNCH_TARGETS[2]
        elif aosp_version in ["14"]:
            lunch_target = SUPPORTED_LUNCH_TARGETS[3]
        elif aosp_version in ["15", "16"]:
            lunch_target = SUPPORTED_LUNCH_TARGETS[4]
        else:
            raise RuntimeError(f"Unsupported Android version: {args.version}")
    logging.debug(f"Downloading and extracting app packages to: {aosp_packages_abs_path}")
    failed_firmware_ids = []
    succeed_firmware_ids = []
    download_url_list = []

    clear_environment(args.aosp_path, aosp_packages_abs_path, args.version)
    logging.info(f"Building for lunch target: {lunch_target} with aosp version: {aosp_version}")
    try:
        os.remove(PATH_BUILD_FILE_ARTEFACT_LOG)
    except FileNotFoundError:
        pass
    counter = 0

    if args.skip_counter:
        skip_counter = args.skip_counter
    else:
        skip_counter = 0

    for firmware_id in tqdm(firmware_id_list):
        out_dir = os.path.join(ROOT_PATH, "out")
        results_dir = os.path.join(ROOT_PATH, out_dir, f"fmd_build_injector_{firmware_id}")
        if skip_counter > 0:
            skip_counter -= 1
            logging.info(f"Skipping firmware id: {firmware_id}. Remaining skip count: {skip_counter}")
            continue
        os.makedirs(BUILD_OUT_PATH, exist_ok=True)

        # Copy template for later access in post injector
        shutil.copytree(TEMPLATE_PATH, TMP_TEMPLATE_PATH, dirs_exist_ok=True)

        logging.info(f"Number of firmware ids left to process: {len(firmware_id_list) - firmware_id_list.index(firmware_id)}")
        try:
            logging.info(f"Start fetching build files for firmware-id: {firmware_id}")
            is_download_success = fetch_build_files(firmware_id, cookies, args.fmd_url, BUILD_OUT_PATH,
                                                   auth_username=args.fmd_username, auth_password=fmd_password)
            if not is_download_success:
                failed_firmware_ids.append(firmware_id)
                continue
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
                                                    cookies=cookies,
                                                    tag=getattr(args, 'tag', None))
                end_time = time.time()
                duration = end_time - start_time

                status = "success" if is_build_success else "failure"
                result = {
                    "hostname": os.uname()[1],
                    "firmware_id": firmware_id,
                    "duration": round(duration, 2),
                    "android_version": aosp_version,
                    "lunch_target": lunch_target,
                    "pre_inj_config": PRE_INJECTOR_CONFIG_PATH,
                    "post_inj_config": POST_INJECTOR_CONFIG_PATH,
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
                # If a tag is provided, sanitize it and append to the filename before the extension
                if getattr(args, 'tag', None):
                    sanitized_tag = re.sub(r"\W+", "_", args.tag)
                    tag_part = f"_{sanitized_tag}"
                else:
                    tag_part = ""
                filename = f"{firmware_id}_v{args.version}_{lunch_target}{tag_part}.zip".replace('-', '_')

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
                    write_text_output(filename, PATH_BUILD_FILE_ARTEFACT_LOG)
                    logging.info(f"Number of Success builds so far: {len(succeed_firmware_ids)}. Number of failed builds so far: {len(failed_firmware_ids)}.")
                else:
                    raise RuntimeError(f"Upload process for firmware-id: {firmware_id} failed.")
            else:
                raise RuntimeError(f"Build process for firmware-id: {firmware_id} failed.")
        except Exception as err:
            logging.error(f"Got an error processing firmware-id: {firmware_id}. Error: {err}")
            failed_firmware_ids.append(firmware_id)
        finally:
            try:
                copy_result_files(results_dir, out_dir, args.skip_clean)
            except Exception as err:
                logging.error(f"Got an error copying build results: {err} | {results_dir}")
                traceback.print_exc()
            if not args.skip_clean:
                clear_environment(args.aosp_path, aosp_packages_abs_path, aosp_version)

        counter += 1
        if args.build_only_first and counter >= args.build_only_first:
            logging.info("Build only first flag is set. Stopping after first firmware id.")
            break

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

    if args.arch not in SUPPORTED_ARCHITECTURES:
        raise RuntimeError(f"Unsupported architecture: {args.arch}. Supported architectures: {SUPPORTED_ARCHITECTURES}")

    if (not os.path.exists(args.aosp_path)
            or not os.path.exists(args.pre_injector_config)
            or not os.path.exists(args.post_injector_config)):
        raise RuntimeError(f"Files or directories do not exist for Pre- or Post-Injector Configuration or AOSP source code.")


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

    if args.reset_aosp:
        aosp_packages_apps_abs_path = os.path.join(args.aosp_path, AOSP_PACKAGES_APPS_PATH)
        clear_environment(args.aosp_path, aosp_packages_apps_abs_path, args.version)
        logging.info("Reset aosp build environment.")
        exit(0)

    set_skipped_module_names()
    fmd_password, docker_repo_password = get_passwords(args)

    os.makedirs(BUILD_OUT_PATH, exist_ok=True)
    # Expose docker/nexus repo credentials as globals so helper functions can upload artefacts
    globals()['DOCKER_REPO_URL_GLOBAL'] = args.docker_repo_url
    globals()['DOCKER_REPO_USERNAME_GLOBAL'] = args.docker_repo_username
    globals()['DOCKER_REPO_PASSWORD_GLOBAL'] = docker_repo_password
    csrf_cookie = get_csrf_token(args.fmd_url)
    firmware_id_list, cookies = fetch_firmware_ids(args, fmd_password, csrf_cookie)
    process_firmware_ids(args, firmware_id_list, cookies, docker_repo_password, fmd_password)
    logging.info("===============================================================")


if __name__ == "__main__":
    main()
