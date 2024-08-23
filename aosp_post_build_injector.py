"""
This script includes methods to inject objects into the AOSP source code after the source code has been built and
before it is packaged into a firmware image. The script is used to inject blobs into the file system to enable
the replacement of the original blobs (from AOSP) with the vendor flavoured blobs.
"""
import argparse
import shutil
import logging
import subprocess
import time
import os
import stat
import traceback
from concurrent.futures import ProcessPoolExecutor as Executor, as_completed
from config import AOSP_DEFAULT_PACKAGE_NAMES, VENDOR_BLACKLISTED_PACKAGES, EXTRACTED_PACKAGES_PATH
from setup_logger import setup_logger
from tqdm import tqdm

if os.environ.get("FMD_DEBUG") == "True":
    setup_logger(logging.DEBUG)
else:
    setup_logger()

FOLDER_NAME_OBJECTS = "obj"
FOLDER_NAME_EXECUTABLES = "EXECUTABLES"
FOLDER_NAME_JAVA_LIBRARIES = "JAVA_LIBRARIES"
FOLDER_NAME_ETC = "ETC"
PARTITION_NAME_LIST = ["super", "system", "vendor", "product", "odm", "oem", "data"]

SKIPPED_APP_LIST = ["GooglePermissionController.apk", "GooglePackageInstaller.apk"]
SKIPPED_APP_LIST.extend(VENDOR_BLACKLISTED_PACKAGES)
SKIPPED_FILE_EXTENSION_LIST = [".bprof", ".policy", ".rc", ".apex", ".ko", ".prop", ".xml", ".capex",
                               ".odex",
                               ".vdex",
                               ".prof",
                               ".idsig"     # File left over from the file apk signing process
                               ]
SKIPPED_BINARY_LIST = ["vold",
                       "keystore2",
                       "vdc",
                       "vndservicemanager",
                       "servicemanager",
                       "hwservicemanager",
                       "console",
                       "zygote",
                       "tee",
                       "qemu-props",
                       "boringssl_self_test32",
                       "boringssl_self_test64",
                       "ueventd",
                       "wait_for_keymaster",
                       "linkerconfig",
                       "bootstat",
                       "wpa_supplicant",
                       "apexd-bootstrap",
                       "bootstrap",
                       "fsverity_init",
                       "init",
                       "apexd",
                       "atrace",
                       "setprop",
                       "getprop",
                       "std.build.prop",
                       "pro.build.prop",
                       "default.prop",
                       "lmkd",
                       "build.prop",
                       "raw.image"      # Leftover from the file extraction process
                       ]
SKIPPED_KEYWORD_LIST = ["selinux",
                        "keystore",
                        "keymaster",
                        "android.hardware",
                        "vold",
                        "recovery-refresh",
                        "vendor.sensors",
                        "atrace",
                        "qseecom",
                        "exfat",
                        "vendor.qti.hardware",
                        "hardware",
                        "zygote",
                        "android.hidl",
                        "qti",
                        "hwservicemanager",
                        "secureboot"]
ALLOWED_OVERWRITE_FILE_EXTENSION_LIST = [".ogg", ".otf", ".ttf"]
ALLOW_FILE_OVERWRITE = ["framework-res.apk", "framework-ext-res.apk",
                        "passwd", "group"]
ALLOW_FILE_OVERWRITE.extend(AOSP_DEFAULT_PACKAGE_NAMES)
ALLOWED_KEYWORD = ["overlay"]


def start_post_build_injector(aosp_path, source_folder_path, target_out_path):
    """
    Start the post build injector. Replaces the original objects in the AOSP source code with the vendor flavoured
    objects.

    :param aosp_path: str - path to the AOSP source code.
    :param source_folder_path: str - path to the source folder where the objects to inject reside.
    :param target_out_path: str - path to the AOSP target out folder.

    """
    with Executor() as executor:
        inject(aosp_path, source_folder_path, target_out_path, executor)


def inject(aosp_path, source_folder_path, target_out_path, executor):
    start_time = time.time()
    error_list, inj_obj_list, inj_partition_list = process_partitions(aosp_path, source_folder_path,
                                                                      target_out_path, executor)
    end_time = time.time()
    execution_time = end_time - start_time
    execution_time_minutes = execution_time / 60
    logging.info(f"Errors:")
    for obj in error_list:
        logging.info(f"Error: {obj}")
    logging.info(f"Objects injected:")
    for obj in inj_obj_list:
        logging.info(f"Indirect Inject: {obj}")
    logging.info(f"Partition files injected:")
    for obj in inj_partition_list:
        logging.info(f"Direct Inject: {obj}")
    logging.info(f"Execution time: {execution_time_minutes} minutes")
    logging.info(f"Number of errors: {len(error_list)}")
    logging.info(f"Number of objects injected: {len(inj_obj_list)}")
    logging.info(f"Number of partition files injected: {len(inj_partition_list)}")


def make_file_executable(root_dir, filename):
    """
    Makes the file executable.

    :param root_dir: str - path to the root directory.
    :param filename: str - name of the file to make executable.

    """
    logging.info(f"Making file: {filename} executable.")
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for name in filenames:
            if name == filename:
                file_path = os.path.join(dirpath, name)
                os.chmod(file_path, os.stat(file_path).st_mode | stat.S_IEXEC)
                logging.info(f"Made file: {file_path} executable.")


def get_folders(directory_path):
    folders = []
    for entry in os.listdir(directory_path):
        full_path = os.path.join(directory_path, entry)
        if os.path.isdir(full_path):
            folders.append(full_path)
    return folders


def process_partitions(aosp_path, source_folder_path, target_out_path, executor):
    folder_path_list = get_folders(source_folder_path)
    logging.debug(f"Folder path list: {folder_path_list}")

    combined_error_list = []
    combined_inj_obj_list = []
    combined_inj_partition_list = []

    for folder_path in tqdm(folder_path_list, desc="Processing partitions"):
        error_list, inj_obj_list, inj_partition_list = process_partition_files(aosp_path, folder_path,
                                                                               target_out_path, executor)
        combined_error_list.extend(error_list)
        combined_inj_obj_list.extend(inj_obj_list)
        combined_inj_partition_list.extend(inj_partition_list)

    return combined_error_list, combined_inj_obj_list, combined_inj_partition_list


def process_file_concurrently(aosp_path, file_path, partition_name, target_out_path):
    error = None
    inj_obj = None
    inj_partition = None

    try:
        module_type = get_module_type(file_path)

        if module_type in ["SKIPPED"]:
            return f"Skipped File inject: {file_path}", None, None

        filename = os.path.basename(file_path)
        if filename and filename != "":
            allow_file_overwrite = (filename in ALLOW_FILE_OVERWRITE
                                    or any(keyword in filename for keyword in ALLOWED_KEYWORD))
        else:
            allow_file_overwrite = False

        if module_type == "APP":
            if filename.lower() in SKIPPED_APP_LIST:
                return f"Skipped Apk inject: {file_path}", None, None

            if allow_file_overwrite:
                signing_success, output, error_message = handle_apk_signing(file_path, aosp_path)
                if not signing_success:
                    return (error_message, file_path), file_path, inj_partition
            else:
                return f"Skipped Apk inject: {file_path}", None, None

        original_file_path = search_original_file_in_obj(partition_name,
                                                         module_type,
                                                         os.path.basename(file_path),
                                                         target_out_path)

        if original_file_path is None or module_type == "SHARED_LIBRARIES":
            inject_file_into_partition(file_path, partition_name, target_out_path, allow_file_overwrite)
            inj_partition = file_path
        else:
            inject_file_into_obj(file_path, original_file_path)
            inj_obj = (file_path, original_file_path)
    except Exception as e:
        error = f"{e}:{traceback.format_exc()}"

    return error, inj_obj, inj_partition


def handle_apk_signing(file_path, aosp_path):
    signing_key = get_signing_key_from_module(file_path)
    if not signing_key:
        return False, None, f"Signing key name not found for {file_path}"
    signing_key_path = get_signing_key_path(aosp_path, signing_key)
    if not os.path.exists(signing_key_path):
        return False, None, f"Signing key not found at {signing_key_path}"
    success, output, error_message = sign_apk_file(file_path, signing_key_path)
    if not success:
        return False, None, f"Error signing APK file: {signing_key}|{signing_key_path}|{error_message}"
    return success, output, (error_message, file_path, signing_key, signing_key_path)


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
        return "platform"


def get_signing_key_path(aosp_path, signing_key_name):
    key_file_path = f"{aosp_path}/build/target/product/security/{signing_key_name}.p12"
    return key_file_path


def execute_command(command):
    """
    Execute a command and checks if it has an exit code of 0.

    :param command: list - the command and its arguments to execute.
    :return: tuple(bool, str, str) - (True, stdout, None) if the command was successful, (False, None, stderr) otherwise.
    """
    result = subprocess.run(command, capture_output=True, text=False)
    if result.returncode == 0:
        return True, result.stdout.decode('utf-8', errors='ignore').strip(), None
    else:
        return False, None, (f"Error signing exit code: {result.returncode}|"
                             f"{result.stderr.decode('utf-8', errors='ignore').strip()}")


def sign_apk_file(apk_file_path, signing_key_path):
    """
    Signs the APK file with apksigner.

    :param apk_file_path: str - path to the APK file.
    :param signing_key_path: str - path to the signing key.
    """
    logging.info(f"Signing APK file: {apk_file_path}")
    sign_command = ['apksigner', 'sign',
                    '--ks', signing_key_path,
                    '--ks-pass', 'pass:',
                    '--in', apk_file_path,
                    '--out', apk_file_path]
    success, output, error_message = execute_command(sign_command)
    return success, output, error_message


def process_partition_files(aosp_path, folder_path, target_out_path, executor):
    error_list = []
    inj_obj_list = []
    inj_partition_list = []
    partition_name = os.path.basename(folder_path)
    file_paths = [os.path.join(root, file_name) for root, _, file_name_list in scandir_walk(folder_path) for
                  file_name in file_name_list]

    # Initialize tqdm progress bar
    progress_bar = tqdm(total=len(file_paths), desc=f"Processing files in partition: {partition_name}")

    future_dict = {
        executor.submit(process_file_concurrently, aosp_path, file_path, partition_name, target_out_path):
            file_path for file_path in file_paths}

    for future in as_completed(future_dict):
        file_path = future_dict[future]
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
    file_name = os.path.basename(source_file_path)
    if file_extension in ["", None] and "/bin/" in source_file_path:
        module_type = "EXECUTABLES"
    elif file_extension == ".jar":
        module_type = "JAVA_LIBRARIES"
    elif file_extension == ".so":
        module_type = "SHARED_LIBRARIES"
    elif file_extension in [".apk", ".odex", ".vdex", ".art", ".oat", ".dex", ".apex"]:
        module_type = "APP"
    elif file_extension in SKIPPED_FILE_EXTENSION_LIST:
        module_type = "SKIPPED"
    elif "/etc/" in source_file_path:
        module_type = "ETC"
    else:
        module_type = "MISC"

    if (file_name in SKIPPED_BINARY_LIST
            or any(keyword in source_file_path for keyword in SKIPPED_KEYWORD_LIST)):
        module_type = "SKIPPED"

    return module_type


def scandir_walk(dir_path):
    """
    A generator that yields a tuple (dirpath, dirnames, filenames) similar to os.walk,
    but uses os.scandir to improve performance.
    """
    dirnames = []
    filenames = []

    with os.scandir(dir_path) as scandir_it:
        for entry in scandir_it:
            if entry.is_dir(follow_symlinks=False):
                dirnames.append(entry.name)
            else:
                filenames.append(entry.name)

    yield dir_path, dirnames, filenames

    for dirname in dirnames:
        new_path = os.path.join(dir_path, dirname)
        yield from scandir_walk(new_path)


def search_original_file_in_obj(partition_name, module_type, file_name, target_out_path):
    """
    Searches for the original file in the AOSP source code.
    """
    target_obj_path = os.path.join(target_out_path, FOLDER_NAME_OBJECTS)
    search_folder_path = str(os.path.join(target_obj_path, module_type if module_type != "MISC" else ""))
    if partition_name in ["super", "system"]:
        partition_name = ""

    result_file_path = None
    for root, dirs, files in os.walk(search_folder_path):
        # Filter directories if partition_name is specified
        if partition_name and not any(partition_name in d for d in dirs):
            continue

        # Check if file is in the current directory
        if file_name in files:
            candidate_path = os.path.join(root, file_name)
            # Verify if it matches the partition criteria
            if not partition_name or partition_name in root:
                result_file_path = candidate_path
                break  # Terminate search early

    if result_file_path:
        return result_file_path
    else:
        logging.debug(f"No file found for {file_name} in {search_folder_path} with partition {partition_name}")
        return None


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


def set_executable_permission(file_path):
    """
    Set the executable permission for a file.

    :param file_path: str - path to the file.
    :return: bool - True if the permission was set successfully, False otherwise.
    """
    try:
        os.chmod(file_path, os.stat(file_path).st_mode | stat.S_IEXEC)
        return True
    except PermissionError as e:
        logging.error(f"Permission denied: {e}")
        return False


def inject_file_into_partition(source_file_path, partition_name, target_out_path, overwrite=False):
    if partition_name == "super":
        partition_name = "system"

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
    target_file_injection_path = os.path.join(target_dir_injection_path, os.path.basename(source_file_path))
    target_file_injection_path = os.path.normpath(target_file_injection_path)

    if os.path.exists(target_file_injection_path):
        file_extension = os.path.splitext(target_file_injection_path)[1]
        if os.path.islink(target_file_injection_path) or file_extension in ALLOWED_OVERWRITE_FILE_EXTENSION_LIST:
            shutil.copyfile(source_file_path, target_file_injection_path)
        else:
            if overwrite:
                logging.debug(f"Overwriting file: {source_file_path} into {target_file_injection_path}")
                shutil.copyfile(source_file_path, target_file_injection_path)
                if not set_executable_permission(target_file_injection_path):
                    raise PermissionError(f"Permission denied for overwrite {target_file_injection_path}")
            else:
                if os.path.isfile(target_file_injection_path):
                    logging.debug(f"File {target_file_injection_path} already exists.")
                    #raise Warning(f"File {target_file_injection_path} already exists.")
    else:
        logging.debug(f"Injecting file: {source_file_path} into {target_file_injection_path}\n")
        shutil.copyfile(source_file_path, target_file_injection_path)
        if not set_executable_permission(target_file_injection_path):
            raise PermissionError(f"Permission denied for not existing file inject: {target_file_injection_path}")


def inject_file_into_obj(source_file_path, original_file_path):
    """
    Injects a file into the AOSP source code.
    """
    logging.debug(f"Overwriting Obj file: {source_file_path} into {original_file_path}")
    shutil.copyfile(source_file_path, original_file_path)
    os.chmod(original_file_path, os.stat(original_file_path).st_mode | stat.S_IEXEC)


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
    parser.add_argument("-a",
                        "--aosp-root-path",
                        default=None,
                        required=True,
                        type=str,
                        help='Path to the AOSP root folder.')
    args = parser.parse_args()

    return args


def main():
    logging.info("=======================AOSP POST BUILD INJECTOR=======================")
    args = parse_arguments()
    source_folder_path = args.source_path
    if not source_folder_path.endswith("/"):
        source_folder_path += "/"
    target_out_path = args.target_out_path
    if not target_out_path.endswith("/"):
        target_out_path += "/"
    aosp_path = args.aosp_root_path
    if not aosp_path.endswith("/"):
        aosp_path += "/"
    logging.info(f"Source folder path: {source_folder_path}")
    logging.info(f"Target out path: {target_out_path}")
    start_post_build_injector(aosp_path, source_folder_path, target_out_path)
    logging.info("=======================AOSP POST BUILD INJECTOR EXIT=======================")


if __name__ == "__main__":
    main()
