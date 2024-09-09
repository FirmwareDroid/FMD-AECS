"""
This script includes methods to inject objects into the AOSP source code after the source code has been built and
before it is packaged into a firmware image. The script is used to inject blobs into the file system to enable
the replacement of the original blobs (from AOSP) with the vendor flavoured blobs.
"""
import argparse
import shutil
import logging
import subprocess
import tempfile
import time
import os
import stat
import traceback
import zipfile
from concurrent.futures import ProcessPoolExecutor as Executor, as_completed
from config import AOSP_DEFAULT_PACKAGE_NAMES, VENDOR_BLACKLISTED_PACKAGES, EXTRACTED_PACKAGES_PATH, \
    BLACKLISTED_KEYWORDS, FILE_CONTEXT_TEMPLATE_PATH, APEX_PRIVATE_KEY_PATH, APEX_PUBKEY_PATH, \
    SHARED_USER_ID_MAPPING_DICT
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
MODULE_TYPE_ABI_COMPATIBLE = ["SHARED_LIBRARIES", "EXECUTABLES", "ETC"]

# Singleton Apps: StorageManagerGoogle.apk
SKIPPED_APP_LIST = ["GooglePermissionController.apk", "GooglePackageInstaller.apk"]
for blacklisted_module_name in VENDOR_BLACKLISTED_PACKAGES:
    SKIPPED_APP_LIST.append(f"{blacklisted_module_name}.apk")

SKIPPED_FILE_LIST = ["com.android.vndk.current.apex"]
SKIPPED_STATIC_FILE_KEYWORD_LIST = ["vintf", "vndk"]

SKIPPED_FILE_EXTENSION_LIST = [".bprof",
                               ".policy",
                               ".rc",
                               ".ko",
                               ".prop",
                               ".capex",
                               #".prof",
                               #".odex",
                               #".vdex",
                               ".art",
                               ".oat",
                               ".apex"
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
                       "otacerts.zip",  # Allow to overwrite with own certificates
                       "raw.image",  # Leftover from the file extraction process
                       "com.google.android.adbd.apex",  # Blocks ADB access
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

ALLOWED_OVERWRITE_FILE_EXTENSION_LIST = [".ogg",
                                         ".otf",
                                         ".ttf"]

ALLOW_FILE_OVERWRITE = ["framework-res.apk",
                        "framework-ext-res.apk",
                        "passwd",
                        "group",
                        "com.google.android.hardwareinfo.xml",
                        ]
for default_module_name in AOSP_DEFAULT_PACKAGE_NAMES:
    ALLOW_FILE_OVERWRITE.append(f"{default_module_name}.apk")
ALLOWED_KEYWORD = ["Overlay",
                   "Connectivity",
                   "Wifi",
                   "Telephony",
                   "Telecom",
                   "TeleService",
                   "TelephonyProvider",
                   "NetworkStackGoogle",
                   "SystemUI",  # Breaks SystemUI
                   "libadb_protos"
                   ]
#for blacklisted_keyword in BLACKLISTED_KEYWORDS:
#    ALLOWED_KEYWORD.append(blacklisted_keyword)
ALLOW_FILE_INJECT = ["installd.rc",
                     "com.google.android.extservices.apex",
                     "com.google.android.permission.apex",
                     "com.google.android.media.apex",
                     "com.google.android.art.apex",
                     "com.google.android.media.swcodec.apex",
                     "com.google.android.telephony.apex",
                     #"com.google.android.tzdata3.apex", --> No exact file match com.android.tzdata.apex is used
                     "com.google.android.os.statsd.apex",
                     "com.google.android.resolv.apex",
                     "com.google.android.sdkext.apex",
                     "com.google.android.mediaprovider.apex",
                     "com.google.android.tethering.apex",
                     "com.google.android.conscrypt.apex",
                     "com.google.android.wifi.apex",
                     "com.google.android.cellbroadcast.apex",
                     ]


def start_post_build_injector(aosp_path, source_folder_path, target_out_path, lunch_target):
    """
    Start the post build injector. Replaces the original objects in the AOSP source code with the vendor flavoured
    objects.

    :param aosp_path: str - path to the AOSP source code.
    :param source_folder_path: str - path to the source folder where the objects to inject reside.
    :param target_out_path: str - path to the AOSP target out folder.
    :param lunch_target: str - lunch target for the AOSP build.

    """
    with Executor() as executor:
        inject(aosp_path, source_folder_path, target_out_path, executor, lunch_target)


def inject(aosp_path, source_folder_path, target_out_path, executor, lunch_target):
    start_time = time.time()
    error_list, inj_obj_list, inj_partition_list = process_partitions(aosp_path,
                                                                      source_folder_path,
                                                                      target_out_path,
                                                                      executor,
                                                                      lunch_target)
    end_time = time.time()
    execution_time = end_time - start_time
    execution_time_minutes = execution_time / 60
    logging.info(f"Objects injected:")
    for obj in inj_obj_list:
        logging.info(f"Indirect Inject via obj: {obj}")
    logging.info(f"Partition files injected:")
    for obj in inj_partition_list:
        logging.info(f"Direct Inject: {obj}")
    logging.info(f"Errors:")
    for obj in error_list:
        logging.info(f"Error: {obj}")
    logging.info(f"Execution time: {execution_time_minutes} minutes")
    logging.info(f"Number of errors: {len(error_list)}")
    logging.info(f"Number of objects injected: {len(inj_obj_list)}")
    logging.info(f"Number of partition files injected: {len(inj_partition_list)}")
    logging.info(f"Number of files processed: {len(error_list) + len(inj_obj_list) + len(inj_partition_list)}")


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


def process_partitions(aosp_path, source_folder_path, target_out_path, executor, lunch_target):
    folder_path_list = get_folders(source_folder_path)
    logging.debug(f"Folder path list: {folder_path_list}")

    combined_error_list = []
    combined_inj_obj_list = []
    combined_inj_partition_list = []

    for folder_path in tqdm(folder_path_list, desc="Processing partitions"):
        error_list, inj_obj_list, inj_partition_list = process_partition_files(aosp_path,
                                                                               folder_path,
                                                                               target_out_path,
                                                                               executor,
                                                                               lunch_target)
        combined_error_list.extend(error_list)
        combined_inj_obj_list.extend(inj_obj_list)
        combined_inj_partition_list.extend(inj_partition_list)

    return combined_error_list, combined_inj_obj_list, combined_inj_partition_list


def process_file_concurrently(aosp_path, file_path, partition_name, target_out_path, lunch_target):
    inj_obj = None
    inj_partition = None
    error_message = None
    try:
        module_type = get_module_type(file_path)
        if module_type in ["SKIPPED"]:
            error_message = f"Skipped File inject (Keyword/Extension/Filename): {file_path}"
        else:
            filename = os.path.basename(file_path)
            if filename and filename != "":
                allow_file_overwrite = (filename in ALLOW_FILE_OVERWRITE)
            else:
                allow_file_overwrite = False

            file_extension = os.path.splitext(file_path)[1]
            if module_type == "APPS" and file_extension.lower() == ".apk":
                error_message = handle_app_modules(file_path, aosp_path, filename, allow_file_overwrite)
            elif module_type == "STATIC_CONFIG":
                error_message = handle_static_config(file_path, filename)
            elif module_type == "ETC" and (file_extension.lower() == ".apex" or file_extension.lower() == ".capex"):
                error_message = handle_apex_modules(file_path, aosp_path, lunch_target)

            if not error_message:
                inj_obj, inj_partition = search_and_inject(partition_name, module_type, file_path, target_out_path,
                                                           allow_file_overwrite)
    except Exception as e:
        error_message = f"{e}:{traceback.format_exc()}"

    result = error_message, inj_obj, inj_partition
    return result


def handle_static_config(file_path, filename):
    error_message = None
    if filename.lower() in SKIPPED_FILE_LIST:
        error_message = f"Skipped known problematic filename: {file_path}"
    elif any(keyword in file_path for keyword in SKIPPED_STATIC_FILE_KEYWORD_LIST):
        error_message = f"Skipped file due known problematic keyword in path: {file_path}"

    if filename.lower in ALLOW_FILE_INJECT:
        logging.info(f"Allow file inclusion: {file_path}")

    return error_message


def get_apex_signing_key_from_filename(file_path):
    file_name = os.path.basename(file_path)
    if "media" in file_name:
        singing_key = "media"
    elif (("network" in file_name
          or "tethering" in file_name
          or "wifi" in file_name
          or "bluetooth" in file_name)
          or "cellbroadcast" in file_name):
        singing_key = "networkstack"
    else:
        singing_key = "platform"
    return singing_key


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
    """
    Get the signing key from the manifest file of the APK.
    Args:
        apk_file:

    Returns:

    """
    signing_key = "platform"
    for key, shared_uid_list in SHARED_USER_ID_MAPPING_DICT.items():
        for shared_uid in shared_uid_list:
            if search_string_in_apk(apk_file, shared_uid):
                signing_key = key
                break
    return signing_key


def handle_apex_modules(file_path, aosp_path, lunch_target):
    error_message = None

    is_repack_success, log_message = repackage_apex_file(aosp_path, file_path, file_path, lunch_target)
    if not is_repack_success:
        error_message = f"Error repackaging APEX file: {file_path}|{log_message}"
    else:
        signing_key = get_apex_signing_key_from_filename(file_path)
        if not signing_key:
            error_message = f"Signing key name not found for {file_path}"
        signing_key_path = get_signing_key_path(aosp_path, signing_key)
        if not error_message:
            is_success, log_message = sign_apk_file(file_path, signing_key_path)
            if not is_success:
                error_message = f"Error signing APEX file: {file_path}|{signing_key}|{signing_key_path}|{log_message}"
            else:
                logging.info(f"APEX file signed: {file_path} with key: {signing_key}")
    return error_message


def handle_app_modules(file_path, aosp_path, filename, allow_file_overwrite):
    error_message = None
    if filename.lower() in SKIPPED_APP_LIST:
        error_message = f"Skipped Apk known problematic app: {file_path}"
    if allow_file_overwrite or any(keyword in filename for keyword in ALLOWED_KEYWORD):
        signing_success, output, subprocess_error_message = handle_apk_signing(file_path, aosp_path)
        if not signing_success:
            error_message = f"Error signing APK file: {file_path}|{subprocess_error_message}"
    else:
        error_message = f"Skipped APP inject (should already be in the image): {file_path}"
    return error_message


def search_and_inject(partition_name, module_type, file_path, target_out_path, allow_file_overwrite):
    inj_partition = None
    inj_obj = None
    file_name = os.path.basename(file_path)
    original_file_path = search_original_file_in_obj(partition_name,
                                                     module_type,
                                                     file_path,
                                                     file_name,
                                                     target_out_path)
    if original_file_path is None:
        file_path_vendor_replaced = file_path.replace(".google", "").replace("Google", "")
        file_name_vendor_replaced = os.path.basename(file_path_vendor_replaced)
        original_file_path = search_original_file_in_obj(partition_name,
                                                         module_type,
                                                         file_path_vendor_replaced,
                                                         file_name_vendor_replaced,
                                                         target_out_path)

    if original_file_path is None or module_type == "SHARED_LIBRARIES":
        target_path = inject_file_into_partition(file_path, partition_name, target_out_path, allow_file_overwrite)
        inj_partition = (file_path, target_path)
    else:
        inject_file_into_obj(file_path, original_file_path)
        inj_obj = (file_path, original_file_path)

    return inj_obj, inj_partition


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


def execute_shell_command(command, aosp_root_path):
    current_directory = os.path.dirname(os.path.realpath(__file__))
    os.chdir(aosp_root_path)
    is_success = False
    result = subprocess.run(command, shell=True, capture_output=True, text=False)
    log_stderr = ""
    log_stdout = result.stdout.decode('utf-8', errors='ignore').strip()
    if result.returncode == 0:
        is_success = True
    else:
        log_stderr = result.stderr.decode('utf-8', errors='ignore').strip()
    os.chdir(current_directory)
    log = f"stdout: {log_stdout} | stderr: {log_stderr}"
    return is_success, log

def execute_command(command):
    """
    Execute a command and checks if it has an exit code of 0.

    :param command: list - the command and its arguments to execute.

    :return: tuple - (bool, str) - True if the command was successful, False otherwise.
    """
    is_success = False
    result = subprocess.run(command, capture_output=True, text=False)
    if result.returncode == 0:
        is_success = True
        log = result.stdout.decode('utf-8', errors='ignore').strip()
    else:
        log = result.stderr.decode('utf-8', errors='ignore').strip()

    return is_success, log


def align_apk_file(apk_file_path):
    logging.info(f"Align apk file: {apk_file_path}")
    out_file_path = f"{apk_file_path}.aligned"
    command = ['zipalign', '-P', '16', '-v', '4', apk_file_path, out_file_path]
    success, log_message = execute_command(command)
    if success:
        shutil.move(out_file_path, apk_file_path)
    return success, log_message


def generate_canned_fs_config(apex_extract_dir_path, output_file):
    """
    Generates a canned_fs_config file for the given directory. The config contains the file paths and their
    permissions. The method gives all the files and directories the default permissions.

    :param apex_extract_dir_path: str - path to the directory where the extracted apex files reside.
    :param output_file: str - path to the output file where the canned_fs_config will be saved.

    """
    with open(output_file, 'w') as out_file:
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
                relative_file_path = os.path.relpath(file_path, apex_extract_dir_path)
                user_id = 1000  # system
                group_id = 1000  # system
                mode = '0644'
                if os.access(file_path, os.X_OK):
                    mode = '0755'  # Executable files get 0755
                out_file.write(f"/{relative_file_path} {user_id} {group_id} {mode}\n")



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


def copy_apex_manifest_file(apex_extract_dir_path, output_dir_path):
    """
    Searches for the APEX manifest file in the APEX extract directory and copies it to the current directory.

    :param apex_extract_dir_path: str - path to the APEX extract directory.
    :param output_dir_path: str - path to the output directory where the APEX manifest file will be copied to.

    :return: bool - True if the APEX manifest file was found and copied, False otherwise.

    """
    logging.info(f"Copying APEX manifest file.")
    is_apex_manifest_file_found = False
    result_file_path = None
    for root, dirs, files in os.walk(apex_extract_dir_path):
        for file in files:
            if file == "apex_manifest.json" or file == "apex_manifest.pb":
                file_path = str(os.path.join(root, file))
                shutil.copyfile(file_path, os.path.join(output_dir_path, file))
                logging.info(f"Copied APEX manifest file: {file_path} to {output_dir_path}.")
                result_file_path = str(os.path.join(output_dir_path, file))
                if os.path.exists(result_file_path):
                    is_apex_manifest_file_found = True
                break
    return is_apex_manifest_file_found, result_file_path


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



def repackage_apex_file(aosp_path, apex_file_path, output_file_path, lunch_target):
    """
    Extracts the APEX file using deapexer, repackages it using apexer, and signs all the APK files in the APEX using apksigner.

    :param aosp_path: str - path to the AOSP source code.
    :param apex_file_path: str - path to the APEX file.
    :param output_file_path: str - path to the output file where the repackage APEX file will be saved.
    :param lunch_target: str - lunch target for the AOSP build.

    :return: tuple - (bool, str) - True if the repackage was successful, False otherwise. String containing the log.
    """
    logging.info(f"Repackaging APEX file: {apex_file_path}")
    success = False
    current_dir = os.getcwd()
    #with (tempfile.TemporaryDirectory() as apex_root_path):
    apex_root_path = tempfile.mkdtemp()
    try:
        os.chdir(apex_root_path)
        apex_extract_dir_path = os.path.join(apex_root_path, "extract")

        extract_success, log_message = extract_apex_file(aosp_path, apex_file_path, apex_extract_dir_path, lunch_target)
        if extract_success:
            logging.info(f"APEX extracted: {apex_file_path}")
            is_manifest_found, apex_manifest_path = copy_apex_manifest_file(apex_extract_dir_path, apex_root_path)
            if is_manifest_found and os.path.exists(apex_manifest_path):
                logging.info(f"APEX manifest file found: {apex_manifest_path}")
                with tempfile.NamedTemporaryFile(delete=False, dir=apex_root_path) as canned_fs_config:
                    generate_canned_fs_config(apex_extract_dir_path, canned_fs_config.name)
                logging.info(f"Canned FS config file: {canned_fs_config.name}")

                resign_apex_apk_files(apex_extract_dir_path)
                apexer_bin_path = os.path.join(aosp_path, "out/soong/host/linux-x86/bin/apexer")
                info = f"Apexer tool path: {apexer_bin_path}|{lunch_target}|{apex_manifest_path}|{apex_extract_dir_path}|{output_file_path}|{canned_fs_config.name}|{FILE_CONTEXT_TEMPLATE_PATH}"
                logging.info(info)
                command = f"{apexer_bin_path} --verbose " \
                                    f"--android_manifest " \
                                    f"--key={APEX_PRIVATE_KEY_PATH} " \
                                    f"--pubkey={APEX_PUBKEY_PATH} " \
                                    f"--apexer_tool_path={aosp_path}out/host/linux-x86/bin/" \
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
            else:
                log_message = f"APEX manifest file not found. {apex_file_path} | apex_manifest_path: {apex_manifest_path}"
        else:
            log_message = f"APEX extraction failed. {apex_file_path} | {log_message}"
    except Exception as e:
        log_message = f"Error repackaging APEX file: {apex_file_path} | {str(e)}"
    finally:
        os.chdir(current_dir)
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


def process_partition_files(aosp_path, folder_path, target_out_path, executor, lunch_target):
    error_list = []
    inj_obj_list = []
    inj_partition_list = []
    partition_name = os.path.basename(folder_path)
    file_paths = list(set(os.path.join(root, file_name.strip()) for root, _, file_name_list in scandir_walk(folder_path)
                          for file_name in file_name_list))

    # Initialize tqdm progress bar
    progress_bar = tqdm(total=len(file_paths), desc=f"Processing files in partition: {partition_name}")

    future_dict = {}
    for file_path in file_paths:
        future = executor.submit(process_file_concurrently, aosp_path, file_path, partition_name, target_out_path, lunch_target)
        future_dict[future] = file_path

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
    elif file_extension in [".apk"]:
        module_type = "APPS"
    elif file_extension in [".xml"]:
        module_type = "STATIC_CONFIG"
    elif "/etc/" in source_file_path:
        module_type = "ETC"
    elif file_extension in [".apex", ".capex"]:
        module_type = "ETC"
    else:
        module_type = "MISC"

    if file_name not in ALLOW_FILE_INJECT:

        if (file_name in SKIPPED_BINARY_LIST
                or any(keyword in source_file_path for keyword in SKIPPED_KEYWORD_LIST)
                or file_extension in SKIPPED_FILE_EXTENSION_LIST):

            if module_type == "APPS" and any(keyword in file_name for keyword in ALLOWED_KEYWORD):
                module_type = "APPS"
            else:
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


def check_binary_architecture(binary_path):
    """
    Check if a binary is compiled for 32-bit or 64-bit.

    :param binary_path: str - path to the binary file.
    :return: str - '32-bit' or '64-bit' based on the binary architecture.
    """
    try:
        with open(binary_path, 'rb') as f:
            # Read the first 5 bytes of the file
            header = f.read(5)
            if len(header) < 5:
                return 'Unknown architecture'

            # Check the magic number and class
            if header[:4] == b'\x7fELF':
                ei_class = header[4]
                if ei_class == 1:
                    return '32-bit'
                elif ei_class == 2:
                    return '64-bit'
            return 'Unknown architecture'
    except Exception as e:
        return f"Error determining architecture: {str(e)}"


def is_abi_compatible(candidate_path, file_path):
    candidate_arch = check_binary_architecture(candidate_path)
    src_arch = check_binary_architecture(file_path)
    logging.debug(f"Checking {candidate_path}|{candidate_arch}|{file_path}|{src_arch}")
    if candidate_arch == "Unknown architecture" or candidate_arch != src_arch:
        logging.debug(f"Skipping {candidate_path}|{candidate_arch}|{file_path} "
                      f"due to architecture mismatch")
        is_same_architecture = False
    else:
        is_same_architecture = True
    return is_same_architecture


def is_elf_binary(file_path):
    """
    Check if a file is an ELF binary.

    :param file_path: str - path to the file.
    :return: bool - True if the file is an ELF binary, False otherwise.
    """
    try:
        with open(file_path, 'rb') as f:
            magic = f.read(4)
            return magic == b'\x7fELF'
    except Exception as e:
        logging.error(f"Error checking ELF binary: {e}")
        return False


def search_original_file_in_obj(partition_name, module_type, file_path, file_name, target_out_path):
    """
    Searches for the original file in the AOSP source code.

    :param partition_name: str - name of the partition.
    :param module_type: str - type of the module.
    :param file_path: str - path to the file which needs to be injected.
    :param target_out_path: str - path to the AOSP target out folder.
    :param file_name: str - name of the file to search for.

    :return: str - path to the original file.


    """

    target_obj_path = os.path.join(target_out_path, FOLDER_NAME_OBJECTS)
    search_folder_path = str(os.path.join(target_obj_path,
                                          module_type if module_type not in ["MISC", "STATIC_CONFIG"] else ""))

    if partition_name in ["super", "system"]:
        partition_name = ""

    result_file_path = None
    for root, dirs, files in os.walk(search_folder_path):
        # Filter directories if partition_name is specified. For example, if partition_name is "vendor".
        if partition_name and not any(partition_name in d for d in dirs):
            continue

        module_name = os.path.splitext(file_name)[0]
        # Check if file is in the current directory is the same as the file we are looking for
        exact_match_files = [f for f in files if f == file_name]

        # Strip the root folder name to match the module name
        root_folder_name_stripped = os.path.basename(root).replace("_intermediates", "")
        root_folder_name_stripped = os.path.basename(root_folder_name_stripped).replace(f"_{partition_name}",
                                                                                        "")
        if exact_match_files:
            candidate_path = os.path.join(root, file_name)
            # Verify if it matches the partition criteria
            if not partition_name or partition_name in root:
                if is_elf_binary(file_path):
                    if module_type in MODULE_TYPE_ABI_COMPATIBLE and not is_abi_compatible(candidate_path,
                                                                                           file_path):
                        continue
                result_file_path = candidate_path
                break  # Terminate search early
        # Check if the folder has the same name but the file within the folder is named differently
        elif module_name == root_folder_name_stripped and partition_name in root:
            for file in files:
                file_extension_src = os.path.splitext(file_name)[1]
                file_extension_obj = os.path.splitext(file)[1]

                if file_extension_src.lower().strip() == file_extension_obj.lower().strip():
                    candidate_path = os.path.join(root, file)
                    if is_elf_binary(file_path):
                        if module_type in MODULE_TYPE_ABI_COMPATIBLE and not is_abi_compatible(candidate_path,
                                                                                               file_path):
                            continue

                    result_file_path = candidate_path
                    break
                elif ((file_extension_src == ".apex" and file_extension_obj == ".capex")
                      or (file_extension_src == ".capex" and file_extension_obj == ".apex")):
                    result_file_path = os.path.join(root, file)
                    logging.info(f"Found APEX file2: {file_name}, result_file_path: {result_file_path}")
                    break

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
        target_dir_injection_path = target_dir_injection_path.replace("/system/system/", "/system/")
    if (not os.path.exists(target_dir_injection_path)
            and not os.path.islink(target_dir_injection_path)):
        logging.debug(f"Creating directory: {target_dir_injection_path}")
        os.makedirs(target_dir_injection_path)

    target_file_injection_path = os.path.join(target_dir_injection_path, os.path.basename(source_file_path))
    target_file_injection_path = os.path.normpath(target_file_injection_path)

    source_file_path = handle_special_matching(source_file_path)

    if os.path.exists(target_file_injection_path):
        file_extension = os.path.splitext(target_file_injection_path)[1]
        if os.path.islink(target_file_injection_path) or file_extension in ALLOWED_OVERWRITE_FILE_EXTENSION_LIST:
            shutil.copyfile(source_file_path, target_file_injection_path)
        else:
            if overwrite:
                logging.info(f"Overwriting file: {source_file_path} into {target_file_injection_path}")
                shutil.copyfile(source_file_path, target_file_injection_path)
                if not set_executable_permission(target_file_injection_path):
                    raise PermissionError(f"Permission denied for overwrite {target_file_injection_path}")
            else:
                if os.path.isfile(target_file_injection_path):
                    logging.info(f"Skipped Inject File {target_file_injection_path} already exists.")
    else:
        logging.debug(f"Injecting file: {source_file_path} into {target_file_injection_path}\n")
        shutil.copyfile(source_file_path, target_file_injection_path)
        if not set_executable_permission(target_file_injection_path):
            raise PermissionError(f"Permission denied for not existing file inject: {target_file_injection_path}")
    return target_file_injection_path


def handle_special_matching(source_file_injection_path):
    if source_file_injection_path.endswith("app_process32"):
        source_file_injection_path = source_file_injection_path.replace("app_process32", "app_process64")
        logging.info(f"Special matching app_process32: {source_file_injection_path}")
    return source_file_injection_path


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
    lunch_target = "sdk_phone_arm64-userdebug"
    logging.info(f"Source folder path: {source_folder_path}")
    logging.info(f"Target out path: {target_out_path}")
    start_post_build_injector(aosp_path, source_folder_path, target_out_path, lunch_target)
    logging.info("=======================AOSP POST BUILD INJECTOR EXIT=======================")


if __name__ == "__main__":
    main()
