"""
A command-line tool that downloads files related to the build process of an Android firmware image and stores them
on disk. Directly extract the downloaded zip content.
"""
import argparse
import re
import uuid
import logging
import shutil
import subprocess
import glob
from tqdm import tqdm
from jinja2 import Environment, FileSystemLoader
from getpass import getpass

from aosp_apex_injector import repackage_apex_file
from aosp_post_build_injector import start_post_build_injector
from common import extract_zip
from config import *
from fmd_backend_requests import download_firmware_build_files, get_csrf_token, authenticate_fmd, \
    get_firmware_ids, get_graphql_url, upload_image_as_raw
from setup_logger import setup_logger
BLOCKED_MODULE_NAMES = get_blocked_module_names()


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


def start_aosp_build(aosp_path, aosp_packages_path, firmware_id, lunch_target, aosp_version, skip_filtering):
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
    is_successful = False
    logging.info(f"Start aosp {aosp_version} build injection with firmware: {firmware_id}")
    overwrite_partition_size(aosp_path, aosp_packages_path)

    aosp_packages_abs_path = str(os.path.join(aosp_path, aosp_packages_path))

    blueprint_build_command = f"bash -c 'source {aosp_path}/build/envsetup.sh && lunch {lunch_target} && m blueprint_tools && m otatools '"
    execute_build_command(aosp_path, firmware_id, blueprint_build_command, aosp_path)
    move_txt_files(EXTRACTED_PACKAGES_PATH, BUILD_OUT_PATH)
    move_packages_to_aosp(aosp_path, aosp_packages_abs_path, EXTRACTED_PACKAGES_PATH, lunch_target)
    inject_meta_files(aosp_path, aosp_packages_path, aosp_version, skip_filtering)

    retry_attempts = BUILD_RETRY_COUNT
    while not is_successful and retry_attempts > 0:
        try:
            main_build_command = get_aosp_build_command(lunch_target, aosp_version, aosp_path)
            execute_build_command(aosp_path, firmware_id, main_build_command, aosp_path)
            target_out_path = get_target_out_path(aosp_path, lunch_target)
            all_extracted_firmware_files_path = os.path.join(EXTRACTED_PACKAGES_PATH, EXTRACTION_ALL_FILES_DIR_NAME)
            start_post_build_injector(aosp_path, all_extracted_firmware_files_path, target_out_path, lunch_target)
            package_build_artefacts_command = get_aosp_repo_build_command(aosp_path, lunch_target)
            execute_build_command(aosp_path, firmware_id, package_build_artefacts_command, aosp_path)
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
    elif lunch_target == SUPPORTED_LUNCH_TARGETS[1] or lunch_target == SUPPORTED_LUNCH_TARGETS[2]:
        return os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_ARM64_PATH)
    else:
        raise RuntimeError(f"Unsupported build architecture: {lunch_target}")


def get_emulator_image_path(aosp_path, lunch_target):
    """
    Returns the path to the emulator image zip file based on the lunch target.

    :param aosp_path: str - path to the root of the aosp source code.
    :param lunch_target: str - aosp build argument to select the build arch.

    :returns: str - path to the emulator image zip file.

    """
    if lunch_target == SUPPORTED_LUNCH_TARGETS[0]:
        image_source_path = os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_x86_64_PATH, AOSP_EMU_ZIP_FILENAME)
    elif lunch_target == SUPPORTED_LUNCH_TARGETS[1] or lunch_target == SUPPORTED_LUNCH_TARGETS[2]:
        image_source_path = os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_ARM64_PATH, AOSP_EMU_ZIP_FILENAME)
    else:
        raise RuntimeError(f"Unsupported build architecture: {lunch_target}")
    return image_source_path


def extract_emulator_image(aosp_path, lunch_target):
    """
    Extracts the aosp emulator images to the image artefacts folder for further usage.
    """
    image_source_path = get_emulator_image_path(aosp_path, lunch_target)
    extract_dir = os.path.join(ROOT_PATH, IMAGE_ARTEFACTS_PATH)

    logging.debug(f"Extract image_source_path: {image_source_path} to {extract_dir}")
    if os.path.exists(image_source_path):
        if not os.path.exists(extract_dir):
            os.makedirs(extract_dir)
        extract_zip(image_source_path, extract_dir)
    else:
        raise RuntimeError(f"Could not find image zip file: {image_source_path}")


def get_base_filename(meta_build_filename):
    """
    Returns the base filename of the aosp build file based on the meta_build_filename.

    :param meta_build_filename:

    :returns: str - base filename of the aosp build file.
    """
    if "product" in meta_build_filename:
        return BASE_PRODUCT_FILE_NAME
    elif "vendor" in meta_build_filename:
        return BASE_VENDOR_FILE_NAME
    elif "system_ext" in meta_build_filename:
        return BASE_SYSTEM_EXT_FILE_NAME
    else:
        return BASE_SYSTEM_FILE_NAME


def read_and_render_template(meta_build_path, base_filename, aosp_version):
    """
    Reads the meta_build.txt file and renders the aosp build file template with the package names.

    :param meta_build_path: str - path to the meta_build.txt file.
    :param base_filename: str - base filename of the aosp build file to use as template.
    :param aosp_version: str - version of the aosp build.

    :returns: str - rendered aosp build file template.

    """
    with open(meta_build_path, 'r') as meta_build_file:
        package_name_list = meta_build_file.readlines()
        template_folder_abs_path = get_template_folder_path(aosp_version)
        logging.debug(f"Using template folder: {template_folder_abs_path} with base filename: {base_filename}")
        environment = Environment(loader=FileSystemLoader(str(template_folder_abs_path)))
        template = environment.get_template(base_filename)
        return template.render(package_name_list=package_name_list)


def get_template_folder_path(aosp_version):
    if aosp_version == "12":
        template_folder_abs_path = os.path.join(ROOT_PATH, TEMPLATE_FOLDER, "12/")
    elif aosp_version == "13":
        template_folder_abs_path = os.path.join(ROOT_PATH, TEMPLATE_FOLDER, "13/")
    else:
        raise RuntimeError(f"Unsupported aosp version: {aosp_version}")
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


def get_packages_to_filter(aosp_path, aosp_packages_path):
    """
    Filters the packages based on the filter list.

    :param aosp_path: str - path to the root of the aosp source code.
    :param aosp_packages_path: str - path to the prebuilt package folder of aosp.

    :returns: list - list of filtered packages.

    """
    if not os.path.isdir(aosp_path):
        raise ValueError(f"{aosp_path} is not a valid directory")

    aosp_packages_abs_path = str(os.path.join(aosp_path, aosp_packages_path))
    logging.debug(f"Search for packages to filter in {aosp_packages_abs_path}")
    dirnames_filtered = []
    try:
        for dirpath, dirnames, filenames in os.walk(aosp_packages_abs_path):
            for dirname in dirnames:
                if dirname.lower() in AOSP_DEFAULT_PACKAGE_NAMES or dirname in VENDOR_BLACKLISTED_PACKAGES:
                    dirnames_filtered.append(dirname)
            for file_name in filenames:
                logging.debug(f"Checking file: {file_name} in {dirpath}")
                if file_name.endswith(".apk"):
                    filename_without_apk_extension = file_name.replace(".apk", "")
                    if (filename_without_apk_extension in AOSP_DEFAULT_PACKAGE_NAMES
                        or filename_without_apk_extension in VENDOR_BLACKLISTED_PACKAGES) \
                            or any(keyword in filename_without_apk_extension for keyword in BLACKLISTED_KEYWORDS):
                        logging.debug(f"Found file: {file_name} in {dirpath} to exclude from the build process.")
                        dirnames_filtered.append(str(os.path.basename(dirpath)))
                    elif file_name.endswith(".apex") or file_name.endswith(".capex"):
                        filename_without_apex_extension = file_name.replace(".apex", "").replace(".capex", "")
                        logging.debug(f"Found file: {file_name} in {dirpath} to exclude from the build process.")
                        if any(keyword in filename_without_apex_extension for keyword in APEX_PRE_INJECT_DISALLOWED_KEYWORDS):
                            logging.debug(f"Found file: {file_name} in {dirpath} to exclude from the build process.")
                            dirnames_filtered.append(str(os.path.basename))
    except Exception as e:
        logging.error(f"An error occurred while filtering packages: {e}")
    return dirnames_filtered


def filter_packages_from_meta(meta_build_path, aosp_path, aosp_packages_path, skip_filtering=False):
    """
    Removes the packages based on the filter list from the meta file.

    :param meta_build_path: str - path to the meta_build.txt file.
    :param aosp_packages_path: str - path to the prebuilt package folder of aosp.
    :param aosp_path: str - path to the root of the aosp source code.
    :param skip_filtering: bool - skip the filtering process.

    :returns: list - list of filtered packages.

    """
    logging.info(f"Search for packages to exclude from build process: {meta_build_path}")
    with open(meta_build_path, 'r') as meta_build_file:
        lines = meta_build_file.readlines()

    if skip_filtering:
        package_dir_name_list = ["framework-res.apk"]
    else:
        package_dir_name_list = get_packages_to_filter(aosp_path, aosp_packages_path)

    logging.info(f"Found {len(package_dir_name_list)} modules to exclude from build.")
    if len(package_dir_name_list) == 0:
        logging.info("Did not find any package to filter. Likely something is wrong...")
    if package_dir_name_list and len(package_dir_name_list) > 0:
        logging.debug(f"Filtering packages: {package_dir_name_list} from {meta_build_path}")
        lines = [line for line in lines if not any(s in line for s in package_dir_name_list)]
        with open(meta_build_path, 'w') as file:
            file.writelines(lines)


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


def overwrite_partition_size(aosp_path, aosp_packages_path):
    """
    Overwrites the partition size in the aosp source code.

    :param aosp_path: str - path to the root of the aosp source code.
    :param aosp_packages_path: str - path to the prebuilt package folder of aosp.

    """
    minimal_partition_size = get_minimal_partition_size(aosp_path, aosp_packages_path)
    super_partition_size = minimal_partition_size + 8388608  # 8MB
    dynamic_partition_size = minimal_partition_size
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
        os.makedirs(destination_directory)

    for file_name in os.listdir(source_directory):
        if file_name.endswith('.txt') or file_name.endswith('.log'):
            source_file = os.path.join(source_directory, file_name)
            destination_file = os.path.join(destination_directory, file_name)
            shutil.move(source_file, destination_file)


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
        if filename.endswith(".apex") or filename.endswith(".capex"):
            return os.path.join(directory_path, filename)
    return None




def move_packages_to_aosp(aosp_path, aosp_packages_abs_path, extracted_packages_path, lunch_target):
    """
    Moves the prebuilt packages to the aosp source code.

    :param aosp_packages_abs_path: str - path to the prebuilt package folder of aosp.
    :param extracted_packages_path: str - path to the extracted packages.

    :returns: list(str) - list of included package names.
    """
    out_dir = os.path.join(aosp_path, MODULE_BASE_INJECT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    if not aosp_path.endswith("/"):
        aosp_path = aosp_path + "/"
    included_package_name_list = []
    for dir_name in os.listdir(extracted_packages_path):
        package_path = os.path.join(extracted_packages_path, dir_name)
        logging.debug(f"Moving {dir_name} from {extracted_packages_path} to {aosp_packages_abs_path}")
        if dir_name.strip() in BLOCKED_MODULE_NAMES:
            logging.info(f"Skipping package: {dir_name} as it is a default module.")
        elif any(keyword in dir_name.strip() for keyword in BLACKLISTED_KEYWORDS):
            logging.info(f"Skipping package by keyword: {dir_name} as it is likely a problematic module.")
        else:
            so_file_extension_list = [".so"]
            apex_file_extension_list = [".apex", ".capex"]
            uuid_dir = str(uuid.uuid4())
            if os.path.isdir(package_path) and check_file_extension(package_path, so_file_extension_list):
                framework_lib_path = os.path.join(aosp_path, f"{out_dir}libs/", uuid_dir)
                logging.info(f"Moved library package: {package_path} to {framework_lib_path}")
                shutil.copytree(package_path, framework_lib_path, dirs_exist_ok=True)
            elif os.path.isdir(package_path) and check_file_extension(package_path, apex_file_extension_list):
                package_dir_name = os.path.basename(package_path)
                apex_file_path = get_apex_file(package_path)
                apex_filename = os.path.basename(apex_file_path)
                if any(keyword in package_dir_name for keyword in APEX_PRE_INJECT_DISALLOWED_KEYWORDS):
                    logging.info(f"Skipping APEX package (KEYWORD) in pre-injector: {package_dir_name}")
                    continue
                modules_path = os.path.join(aosp_path, f"{out_dir}apex", package_dir_name)
                logging.info(f"Moving APEX package: {package_path} to {modules_path}")

                shutil.copytree(package_path, modules_path, dirs_exist_ok=True)
                apex_out_file = os.path.join(modules_path, apex_filename)
                if os.path.exists(apex_out_file):
                    os.remove(apex_out_file)
                if apex_file_path:
                    is_success, log_message = repackage_apex_file(aosp_path, apex_file_path, apex_out_file, lunch_target)
                    if is_success:
                        logging.info(f"Repackaged APEX package: {apex_file_path} to {modules_path}")
                    else:
                        logging.error(f"APEX repacking error: {log_message}")
                        exit(1)
                else:
                    logging.error(f"Could not find apex file in: {modules_path}")
            else:
                logging.info(f"Moving package: {dir_name} to {aosp_packages_abs_path}")
                shutil.copytree(package_path, out_dir, dirs_exist_ok=True)
            logging.info(f"Moved package: {dir_name} to {aosp_packages_abs_path}")
            included_package_name_list.append(dir_name)
    return included_package_name_list


def inject_meta_files(aosp_path, aosp_packages_path, aosp_version, skip_filtering):
    """
    Replaces the original base_system.mk of the AOSP source code with a modified version.
    The modified version includes all the packages to inject into the build process.

    :param aosp_packages_path: str - path to the prebuilt package folder of aosp.
    :param aosp_path: str -  path to aosp root folder.
    :param aosp_version: str - version of the aosp build.
    :param skip_filtering: bool - skip the filtering process.

    """
    for meta_build_filename in META_BUILD_FILENAMES:
        meta_build_path = os.path.join(BUILD_OUT_PATH, meta_build_filename)
        if not os.path.exists(meta_build_path):
            if meta_build_filename == META_BUILD_SYSTEM_FILENAME:
                raise RuntimeError(f"Could not find file: {meta_build_filename} from {meta_build_path}")
            else:
                with open(meta_build_path, 'w'):
                    pass
        base_filename = get_base_filename(meta_build_filename)
        filter_packages_from_meta(meta_build_path, aosp_path, aosp_packages_path, skip_filtering)
        content = read_and_render_template(meta_build_path, base_filename, aosp_version)
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

    if aosp_version not in ["12", "13"]:
        raise RuntimeError(f"Unsupported Android version: {aosp_version}")

    command = f"bash -c 'source {aosp_root}/build/envsetup.sh " \
              f"&& lunch {lunch_target} " \
              "&& m clean " \
              "&& m " \
              "&& m sdk'"
    return command


def get_aosp_repo_build_command(aosp_root, lunch_target):
    command = f"bash -c 'source {aosp_root}/build/envsetup.sh " \
              f"&& lunch {lunch_target} " \
              "&& m sdk_repo " \
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
            command = f"bash -c 'source {aosp_root}/build/envsetup.sh " \
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
        delete_unlisted_directories(aosp_packages_path, AOSP_DEFAULT_PACKAGE_NAMES)
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
            aosp_base_file_path = os.path.join(aosp_path, BASE_PATH, base_filename)
            if os.path.exists(aosp_base_file_path):
                template_folder_abs_path = get_template_folder_path(aosp_version)
                environment = Environment(loader=FileSystemLoader(str(template_folder_abs_path)))
                template = environment.get_template(base_filename)
                return template.render(package_name_list=[])
    except Exception as err:
        logging.error(err)
        pass


def clear_intermediate_files(aosp_path):
    out_dir = os.path.join(aosp_path, MODULE_BASE_INJECT_DIR)
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
        logging.debug(f"Removed {out_dir} from aosp source code.")


def clear_build_out(build_out_path):
    """
    Deletes the build out directory.

    :param build_out_path: str - path to the build out directory.
    """
    try:
        if os.path.exists(build_out_path):
            shutil.rmtree(build_out_path)
            logging.debug(f"Removed {build_out_path}")
    except Exception as err:
        logging.error(err)


def clear_environment(aosp_path, aosp_packages_path, aosp_version):
    """
    Reverts the build environment
    Returns:

    """
    clear_packages(aosp_packages_path)
    clear_intermediate_files(aosp_path)
    extracted_files_path = os.path.join(BUILD_OUT_PATH, PACKAGE_EXTRACTION_DIR_NAME)
    clear_build_out(extracted_files_path)
    clear_base_files(aosp_path, aosp_version)


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
            os.makedirs(tmp_path)
            extract_zip(zip_file_path, tmp_path)
            os.remove(zip_file_path)
            is_successful = True
        except Exception as err:
            logging.error(f"Error fetching firmware build files: {err}")
    logging.debug(f"Completed firmware build file download to {extract_destination_folder}")


def parse_arguments():
    """
    Parse the command line arguments.
    """
    parser = argparse.ArgumentParser(prog='fmd_build_injector',
                                     description="A cli tool to download and store build files from FirmwareDroid.")
    parser.add_argument("-s", "--aosp-path", type=str, default="/home/ubuntu/aosp_12/",
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
                                                                          'to process. Other jobs will be ingored when set.')
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
            is_upload_success = upload_image_as_raw(repo_url,
                                                    username,
                                                    password,
                                                    artefact_path,
                                                    filename)
        except Exception as err:
            logging.error(f"Error uploading image: {err}")
        max_attempts -= 1
        if not is_upload_success:
            logging.error(f"Failed to upload image {filename} to repo. Retrying...{max_attempts}")
    return is_upload_success


def process_firmware_ids(args, firmware_id_list, cookies, docker_repo_password):
    aosp_packages_abs_path = os.path.join(args.aosp_path, AOSP_PACKAGES_APPS_PATH)
    aosp_version = args.version
    if args.arch == SUPPORTED_ARCHITECTURES[0]:
        lunch_target = SUPPORTED_LUNCH_TARGETS[0]
    else:
        if aosp_version == "12":
            lunch_target = SUPPORTED_LUNCH_TARGETS[1]
        elif aosp_version == "13":
            lunch_target = SUPPORTED_LUNCH_TARGETS[2]
        else:
            raise RuntimeError(f"Unsupported Android version: {args.version}")
    logging.debug(f"Downloading and extracting app packages to: {aosp_packages_abs_path}")
    failed_firmware_ids = []
    succeed_firmware_ids = []
    clear_environment(args.aosp_path, aosp_packages_abs_path, aosp_version)
    for firmware_id in tqdm(firmware_id_list):
        try:
            logging.debug(f"Start fetching for build files for firmware-id: {firmware_id}")
            fetch_build_files(firmware_id, cookies, args.fmd_url, BUILD_OUT_PATH)
            logging.debug(f"Start emulator image build process for firmware-id: {firmware_id}")
            is_build_success = start_aosp_build(args.aosp_path,
                                                AOSP_PACKAGES_APPS_PATH,
                                                firmware_id=firmware_id,
                                                lunch_target=lunch_target,
                                                aosp_version=args.version,
                                                skip_filtering=args.skip_filtering)
            if is_build_success:
                logging.info(f"Build process for firmware-id: {firmware_id} was successful.")
                emulator_image_zip_path = get_emulator_image_path(args.aosp_path, lunch_target)
                filename = f"{firmware_id}_v{args.version}_{lunch_target}.zip".replace('-', '_')
                is_upload_success = upload_build_artefact(args.docker_repo_url,
                                                          args.docker_repo_username,
                                                          docker_repo_password,
                                                          emulator_image_zip_path,
                                                          filename)
                if is_upload_success:
                    logging.info(f"Upload of firmware-id: {firmware_id} was successful.")
                    with open("docker_images.txt", "a") as file:
                        file.write(f"{filename.replace('.zip', '')}\n")
                    succeed_firmware_ids.append(firmware_id)
                else:
                    raise RuntimeError(f"Upload process for firmware-id: {firmware_id} failed.")
            else:
                raise RuntimeError(f"Build process for firmware-id: {firmware_id} failed.")
        except Exception as err:
            logging.error(f"Got an error processing firmware-id: {firmware_id}. Error: {err}")
            failed_firmware_ids.append(firmware_id)
        finally:
            if not args.skip_clean:
                clear_environment(args.aosp_path, aosp_packages_abs_path, aosp_version)

    if len(failed_firmware_ids) > 0:
        logging.error(f"Failed to build the following firmware ids: {failed_firmware_ids} for arch: {args.arch}")
    logging.info(f"Successfully built the following firmware ids: {succeed_firmware_ids} for arch: {args.arch}")


def main():
    logging.info("=======================BUILD INJECTOR=======================")
    args = parse_arguments()
    if args.reset_aosp:
        aosp_packages_abs_path = os.path.join(args.aosp_path, AOSP_PACKAGES_APPS_PATH)
        clear_environment(args.aosp_path, aosp_packages_abs_path, args.version)
        logging.info("Reset aosp build environment.")
        exit(0)
    if args.arch not in SUPPORTED_ARCHITECTURES:
        raise RuntimeError(f"Unsupported architecture: {args.arch}. Supported architectures: {SUPPORTED_ARCHITECTURES}")
    fmd_password, docker_repo_password = get_passwords(args)
    csrf_cookie = get_csrf_token(args.fmd_url)
    firmware_id_list, cookies = fetch_firmware_ids(args, fmd_password, csrf_cookie)
    process_firmware_ids(args, firmware_id_list, cookies, docker_repo_password)
    logging.info("===============================================================")


if __name__ == "__main__":
    main()
