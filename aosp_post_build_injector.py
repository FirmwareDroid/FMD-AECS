"""
This script includes methods to inject objects into the AOSP source code after the source code has been built and
before it is packaged into a firmware image. The script is used to inject blobs into the file system to enable
the replacement of the original blobs (from AOSP) with the vendor flavoured blobs.
"""
import argparse
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import os
import time
from setup_logger import setup_logger
from tqdm import tqdm

setup_logger()

FOLDER_NAME_OBJECTS = "obj"
FOLDER_NAME_EXECUTABLES = "EXECUTABLES"
FOLDER_NAME_JAVA_LIBRARIES = "JAVA_LIBRARIES"
FOLDER_NAME_ETC = "ETC"
PARTITION_NAME_LIST = ["super", "system", "vendor", "product", "odm", "oem", "data"]
MODULE_TYPE_LIST = ["EXECUTABLES", "JAVA_LIBRARIES", "SHARED_LIBRARIES", "ETC", "MISC", "APP"]


def start_post_build_injector(source_folder_path, target_out_path):
    start_time = time.time()
    error_list, inj_obj_list, inj_partition_list = process_partitions(source_folder_path, target_out_path)
    if len(error_list) > 0:
        for error in error_list:
            logging.error(error)
    end_time = time.time()
    execution_time = end_time - start_time
    execution_time_minutes = execution_time / 60
    logging.info(f"Execution time: {execution_time_minutes} minutes")
    logging.info(f"Number of errors: {len(error_list)}")
    logging.info(f"Number of objects injected: {len(inj_obj_list)}")
    logging.info(f"Number of partition files injected: {len(inj_partition_list)}")


def get_folders(directory_path):
    folders = []
    for entry in os.listdir(directory_path):
        full_path = os.path.join(directory_path, entry)
        if os.path.isdir(full_path):
            folders.append(full_path)
    return folders


def process_partitions(source_folder_path, target_out_path):
    folder_path_list = get_folders(source_folder_path)
    logging.debug(f"Folder path list: {folder_path_list}")

    combined_error_list = []
    combined_inj_obj_list = []
    combined_inj_partition_list = []

    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = {executor.submit(process_partition_files, folder_path, target_out_path): folder_path for folder_path
                   in tqdm(folder_path_list, desc="Processing partitions")}
        for future in concurrent.futures.as_completed(futures):
            error_list, inj_obj_list, inj_partition_list = future.result()
            combined_error_list.extend(error_list)
            combined_inj_obj_list.extend(inj_obj_list)
            combined_inj_partition_list.extend(inj_partition_list)

    return combined_error_list, combined_inj_obj_list, combined_inj_partition_list


def process_file_concurrently(file_path, partition_name, target_out_path):
    error = None
    inj_obj = None
    inj_partition = None
    module_type = get_module_type(file_path)
    if module_type in ["APP", "SHARED_LIBRARIES"]:
        return None, None, None  # Skipping file

    try:
        original_file_path = search_original_file(partition_name,
                                                  module_type,
                                                  os.path.basename(file_path),
                                                  target_out_path)
        if original_file_path is None:
            inject_file_into_partition(file_path, partition_name, target_out_path)
            inj_partition = file_path
        else:
            inject_file_into_obj(file_path, original_file_path)
            inj_obj = file_path
    except RuntimeError as e:
        error = str(e)

    return error, inj_obj, inj_partition


def process_partition_files(folder_path, target_out_path):
    error_list = []
    inj_obj_list = []
    inj_partition_list = []
    partition_name = os.path.basename(folder_path)
    file_paths = [os.path.join(root, file_name) for root, _, file_name_list in os.walk(folder_path) for file_name in file_name_list]

    # Initialize tqdm progress bar
    progress_bar = tqdm(total=len(file_paths), desc=f"Processing files in partition: {partition_name}")

    with ThreadPoolExecutor() as executor:
        future_to_file = {
            executor.submit(process_file_concurrently, file_path, partition_name, target_out_path): file_path for file_path in file_paths}

        for future in as_completed(future_to_file):
            file_path = future_to_file[future]
            try:
                result = future.result()
                if result[0]:  # If there's an error
                    error_list.append(result[0])
                if result[1]:  # If an object was injected
                    inj_obj_list.append(result[1])
                if result[2]:  # If a partition file was injected
                    inj_partition_list.append(result[2])
            except Exception as exc:
                logging.error(f"Error processing file {file_path}: {exc}")
                error_list.append(str(exc))
            finally:
                # Update progress bar after each task is completed
                progress_bar.update(1)

    # Close the progress bar after all tasks are completed
    progress_bar.close()

    return error_list, inj_obj_list, inj_partition_list


def get_module_type(source_file_path):
    """
    Determines the module type of the source file.
    """
    file_extension = os.path.splitext(source_file_path)[1]
    module_type = None
    if file_extension is None or file_extension == "" and "/bin/" in source_file_path:
        module_type = MODULE_TYPE_LIST[0]
    elif file_extension == ".jar":
        module_type = MODULE_TYPE_LIST[1]
    elif file_extension == ".so":
        module_type = MODULE_TYPE_LIST[2]
    elif file_extension == ".apk" or file_extension == ".odex" or file_extension == ".vdex":
        module_type = MODULE_TYPE_LIST[5]

    if "/etc/" in source_file_path:
        module_type = MODULE_TYPE_LIST[3]
    elif module_type is None:
        module_type = MODULE_TYPE_LIST[4]
    return module_type


def search_original_file(partition_name, module_type, file_name, target_out_path):
    """
    Searches for the original file in the AOSP source code.
    """
    if module_type == "MISC":
        search_folder_path = target_out_path
    else:
        search_folder_path = target_out_path + module_type

    if partition_name == "super" or partition_name == "system":
        partition_name = ""

    # Check if folder has partition and filename in it
    candidate_directory_list = []
    for root, dir_name_list, files in os.walk(search_folder_path):
        for dir_name in dir_name_list:
            if partition_name in dir_name and dir_name.startswith(file_name):
                candidate_directory_list.append(str(os.path.join(root, dir_name)))

    second_round_candidate_list = []
    source_file_extension = os.path.splitext(file_name)[1]
    for candidate_directory in candidate_directory_list:
        for root, dir_name_list, files in os.walk(candidate_directory):
            for file in files:
                file_extension = os.path.splitext(file)[1]
                if file_extension == source_file_extension:
                    second_round_candidate_list.append(str(os.path.join(root, file)))

    result_list = []
    if len(second_round_candidate_list) > 1:
        for result in second_round_candidate_list:
            candidate_file_name = os.path.basename(result)
            if candidate_file_name == file_name:
                result_list.append(result)

    result_file_path = None
    if len(result_list) > 1:
        logging.debug(f"Multiple files found for {file_name}: {result_list}")
        raise RuntimeError(f"Error: Multiple files found: "
                           f"{partition_name}, "
                           f"{module_type}, "
                           f"{file_name}, "
                           f"{target_out_path}\n"
                           f"{second_round_candidate_list}\n"
                           f"{result_list}\n")
    elif len(result_list) == 1:
        result_file_path = result_list[0]

    return result_file_path


def inject_file_into_obj(source_file_path, original_file_path):
    """
    Injects a file into the AOSP source code.
    """
    logging.debug(f"Injecting obj file: {source_file_path} into {original_file_path}")
    #shutil.copyfile(source_file_path, original_file_path)


def is_top_folder(library_path, folder_name):
    """
    Check if the library path is the top folder.

    :param library_path: str - path to the library.
    :param folder_name: str - name of the top folder.

    :return: bool - True if the library path is the top folder, False otherwise.

    """
    path_list = library_path.split(os.sep)
    return path_list[0] == folder_name


def get_subfolders(file_path, top_folder_name):
    """
    Get the subfolders after a specific top folder.

    :param file_path: str - path to the file.
    :param top_folder_name: str - name of the top folder.

    :return: list(str) - list of subfolders after the folder in case there are any subfolders.

    """
    subfolders = []
    if top_folder_name in file_path and not is_top_folder(file_path, top_folder_name):
        path_list = file_path.split(os.sep)
        top_folder_index = path_list.index(top_folder_name.replace("/", ""))
        subfolders = path_list[top_folder_index + 1:]
        subfolders = subfolders[:-1]
    return subfolders


def inject_file_into_partition(source_file_path, partition_name, target_out_path):
    target_partition_path = target_out_path + partition_name
    if not target_partition_path.endswith("/"):
        target_partition_path += "/"
    subfolder_list = get_subfolders(source_file_path, partition_name)
    if len(subfolder_list) == 0:
        target_dir_injection_path = target_partition_path
    else:
        target_dir_injection_path = target_partition_path + str(os.path.join(*subfolder_list))
    if not os.path.exists(target_dir_injection_path):
        logging.debug(f"Creating directory: {target_dir_injection_path}")
        os.makedirs(target_dir_injection_path)
    target_file_injection_path = target_dir_injection_path + os.path.basename(source_file_path)
    logging.debug(f"Injecting file: {source_file_path} into {target_file_injection_path}\n")
    #shutil.copyfile(source_file_path, target_file_injection_path)


def parse_arguments():
    """
    Parse the command line arguments.
    """
    parser = argparse.ArgumentParser(prog='aosp_post_build_injector',
                                     description="A cli tool to inject files into AOSP after the build.")
    parser.add_argument("-s",
                        "--source-path",
                        default=None,
                        required=True,
                        type=str,
                        help='Path to the source folder where the objects to inject reside.')
    parser.add_argument("-t",
                        "--target-out-path",
                        default=None,
                        required=True,
                        type=str,
                        help='Path to the AOSP target out folder.')
    args = parser.parse_args()

    return args


def main():
    logging.info("=======================AOSP POST BUILD INJECTOR=======================")
    args = parse_arguments()
    # source_folder_path = "/home/ubuntu/tmp/6682a66664ee52bdcbb49bd9/"
    source_folder_path = args.source_path
    if not source_folder_path.endswith("/"):
        source_folder_path += "/"
    # target_out_path = "/home/ubuntu/aosp_12/out/target/product/emulator_arm64/"
    target_out_path = args.target_out_path
    if not target_out_path.endswith("/"):
        target_out_path += "/"
    logging.info(f"Source folder path: {source_folder_path}")
    logging.info(f"Target out path: {target_out_path}")
    start_post_build_injector(source_folder_path, target_out_path)
    logging.info("=======================AOSP POST BUILD INJECTOR EXIT=======================")


if __name__ == "__main__":
    main()
