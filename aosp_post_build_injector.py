"""
This script includes methods to inject objects into the AOSP source code after the source code has been built and
before it is packaged into a firmware image. The script is used to inject blobs into the file system to enable
the replacement of the original blobs (from AOSP) with the vendor flavoured blobs.
"""
import argparse
import hashlib
import re
import shutil
import logging
import subprocess
import threading
import time
import json
import os
import stat
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor as Executor, as_completed
from http import cookies
from types import MappingProxyType

from filelock import FileLock

from aosp_apex_injector import handle_apex_modules, prepare_capex, rename_file, repackage_apex_file, \
    POST_INJECTOR_CONFIG, add_new_apex_file
from aosp_module_type import get_module_type
from aosp_post_build_app_injector import handle_apk_signing
from common import extract_vendor_name, remove_vendor_name_from_path, load_configs, is_elf_binary, \
    check_shared_object_architecture, get_path_up_to_first_term, get_md5_from_file
from config import AOSP_BUILD_OUT_SDK_ARM64_x64_PATH_EMU64A, AOSP_BUILD_OUT_SDK_ARM64_x64_PATH, \
    MEASURE_LOOKUP_PERFORMANCE, EXTRACTION_ALL_FILES_PATH
from config_post_injector import *
from fmd_backend_requests import get_csrf_token, authenticate_fmd
from json_writer import write_json_nd_output, write_json_output
from setup_logger import setup_logger
from tqdm import tqdm
from copy_helper import copy_fast
from fast_copy import schedule_copy, wait_for_all_copy_tasks


if os.environ.get("FMD_DEBUG") == "True":
    setup_logger(logging.DEBUG)
else:
    setup_logger()


processed_files_lock = threading.Lock()
# Immutable mapping of md5 -> tuple(file_paths). Built once before worker tasks start.
INTERMEDIATE_MD5_MAP = None
# Cache of file indexes: target_obj_path -> { basename -> [fullpaths] }
FILE_INDEX_CACHE = {}
FILE_INDEX_LOCK = threading.Lock()



def start_post_build_injector(aosp_path,
                              source_folder_path,
                              target_out_path,
                              lunch_target,
                              firmware_id=None,
                              pre_injector_package_list=None,
                              pre_injector_config_path=None,
                              post_injector_config_path=None,
                              cookies=None,
                              aosp_version=None):
    """
    Start the post build injector. Replaces the original objects in the AOSP source code with the vendor flavoured
    objects.

    :param aosp_path: str - path to the AOSP source code.
    :param source_folder_path: str - path to the source folder where the objects to inject reside.
    :param target_out_path: str - path to the AOSP target out folder.
    :param lunch_target: str - lunch target for the AOSP build.
    """
    if pre_injector_package_list is None:
        pre_injector_package_list = []
    logging.info(
        f"Starting post build injector with config:: {post_injector_config_path} | {pre_injector_config_path}")
    pre_injector_config, post_injector_config = load_configs(pre_injector_config_path, post_injector_config_path)
    global PRE_INJECTOR_CONFIG
    global POST_INJECTOR_CONFIG
    PRE_INJECTOR_CONFIG = pre_injector_config
    POST_INJECTOR_CONFIG = post_injector_config
    logging.debug(f"Starting post build injector with the following configuration: {POST_INJECTOR_CONFIG} | {PRE_INJECTOR_CONFIG} | {post_injector_config_path} | {pre_injector_config_path}")
    if not aosp_path.endswith("/"):
        aosp_path = f"{aosp_path}/"
        if not os.path.exists(aosp_path):
            logging.error(f"AOSP source folder does not exist: {aosp_path}")
            raise FileNotFoundError(f"AOSP path does not exist: {aosp_path}")

    logging.info(
        f"Starting post build injector: {aosp_path} | {source_folder_path} | {target_out_path} | {lunch_target}")

    # Improved checks with separate logs to help debugging the root cause when post-injection fails
    if not os.path.exists(source_folder_path):
        logging.error(f"Source folder does not exist: {source_folder_path}")
        raise FileNotFoundError(f"Post-Injection source folder does not exist: {source_folder_path}")

    if not os.path.isdir(source_folder_path):
        logging.error(f"Source path exists but is not a directory: {source_folder_path}")
        raise NotADirectoryError(f"Post-Injection source path is not a directory: {source_folder_path}")

    try:
        entries = os.listdir(source_folder_path)
    except Exception as e:
        logging.error(f"Could not list contents of source folder {source_folder_path}: {e}")
        raise

    if not entries:
        logging.error(f"Source folder is empty: {source_folder_path}")
        raise FileNotFoundError(f"Post-Injection source folder is empty: {source_folder_path}")

    if POST_INJECTOR_CONFIG["ENABLE_INJECTION"]:
        with Executor() as executor:
            inject(aosp_path, source_folder_path, target_out_path, executor, lunch_target, firmware_id, pre_injector_package_list, cookies, aosp_version)
    else:
        logging.info(f"Post-Injection is disabled by configuration: {POST_INJECTOR_CONFIG['ENABLE_INJECTION']}")
        logging.info(f"Skipping post build injection for {source_folder_path} into {target_out_path}")

    logging.debug(f"Finished post build injector")


def group_errors_by_prefix(error_list):
    """
    Groups errors by the first three words and counts occurrences.

    :param error_list: list - List of error messages.
    :return: dict - Grouped errors with counts.
    """
    error_groups = defaultdict(int)
    error_sample_list = {}
    for error in error_list:
        # Extract the first three words
        match = re.match(r"(\S+\s+\S+\s+\S+)", error)
        if match:
            prefix = match.group(1)
            error_groups[prefix] += 1
            if prefix not in error_sample_list:
                error_sample_list[prefix] = error
        else:
            # If no match, group under "Unknown Errors"
            error_groups["Unknown Errors"] += 1
    return error_groups, error_sample_list

def extract_file_type_frequencies(error_list):
    """
    Extracts and counts file types from error messages that end with a file path.

    :param error_list: list - List of error messages.
    :return: dict - Frequency of file types.
    """
    file_type_counts = defaultdict(int)
    for error in error_list:
        # Match file paths at the end of the error message and extract the file extension
        match = re.search(r".*\.(\w+)$", error)
        if match:
            file_extension = match.group(1).lower()  # Extract and normalize the file extension
            file_type_counts[file_extension] += 1
    return file_type_counts

def count_number_of_extracted_files(source_folder_path):
    """
    Counts the number of files in the source folder.

    :param source_folder_path: str - Path to the source folder.
    :return: dict - Number of files in the source folder per partition.
    """
    partition_names = ["system", "vendor", "product", "system_ext", "system_other", "userdata"]
    file_count_per_partition = defaultdict(int)
    for partition_name in partition_names:
        partition_path = os.path.join(source_folder_path, partition_name)
        if os.path.exists(partition_path):
            for root, _, files in os.walk(partition_path):
                file_count_per_partition[partition_name] += len(files)
    return file_count_per_partition


def build_intermediate_file_index(aosp_path, target_out_path):
    # Build the intermediate md5 map once before starting worker tasks. This map is immutable.
    global INTERMEDIATE_MD5_MAP
    try:
        INTERMEDIATE_MD5_MAP = build_intermediate_md5_map(aosp_path)
        logging.info(f"Initialized INTERMEDIATE_MD5_MAP with {len(INTERMEDIATE_MD5_MAP)} entries")
    except Exception as e:
        logging.warning(f"Failed to build INTERMEDIATE_MD5_MAP: {e}")


    # Build a file basename -> paths index for the target obj path to avoid repeated os.walk
    target_obj_path = None
    try:
        target_obj_path = os.path.join(target_out_path, FOLDER_NAME_OBJECTS)
        # Build index if not present (protect with lock to avoid races)
        with FILE_INDEX_LOCK:
            if target_obj_path not in FILE_INDEX_CACHE:
                idx = defaultdict(list)
                if os.path.exists(target_obj_path):
                    for root, dirs, files in scandir_walk(target_obj_path):
                        for f in files:
                            idx[f].append(os.path.join(root, f))
                FILE_INDEX_CACHE[target_obj_path] = idx
                logging.info(f"Built file index for {target_obj_path} with {sum(len(v) for v in idx.values())} entries")
    except Exception as e:
        logging.warning(f"Failed to build file index for target objects: {e}")
    return target_obj_path


def create_error_statistics(error_list):
    """
    Analyze skipped errors and produce statistics about skipped file types.

    The function extracts file paths from error messages that indicate a skipped file
    (messages like: "Skipped File post-inject ...: /path/to/file | module_type: SKIPPED")
    and computes frequency counts of file extensions. Files without an extension are
    recorded under the key "<no_extension>" and — when the file exists on disk —
    additionally checked for ELF magic to estimate how many no-extension files are
    ELF binaries.

    The resulting statistics are logged and written to `SKIPPED_ERROR_LIST_FILE_PATH`.

    :param error_list: list of error message strings
    :return: dict with statistics
    """
    skipped_paths = []
    # Pattern to capture a filesystem path after the colon and before the pipe
    path_re = re.compile(r'Skipped File[^"]*:\s*(/[^|\n]+)\s*\|', re.IGNORECASE)

    for err in error_list:
        m = path_re.search(err)
        if m:
            path = m.group(1).strip()
            skipped_paths.append(path)

    total_skipped = len(skipped_paths)

    ext_counts = defaultdict(int)
    ext_samples = defaultdict(list)
    no_ext_elf_count = 0
    no_ext_paths = []

    for p in skipped_paths:
        _, ext = os.path.splitext(p)
        ext = ext.lower()
        if not ext:
            key = "<no_extension>"
            no_ext_paths.append(p)
            # If file exists try to detect ELF magic
            try:
                if os.path.exists(p) and os.path.isfile(p):
                    with open(p, 'rb') as fh:
                        hdr = fh.read(4)
                        if hdr == b'\x7fELF':
                            no_ext_elf_count += 1
            except Exception:
                # ignore any IO errors while probing files
                pass
        else:
            key = ext

        ext_counts[key] += 1
        if len(ext_samples[key]) < 5:
            ext_samples[key].append(p)

    # Prepare sorted list of extensions by frequency
    sorted_ext = sorted(ext_counts.items(), key=lambda kv: kv[1], reverse=True)

    stats = {
        "total_skipped": total_skipped,
        "extension_counts": dict(sorted_ext),
        "extension_samples": {k: v for k, v in ext_samples.items()},
        "no_extension_count": len(no_ext_paths),
        "no_extension_elf_count": no_ext_elf_count,
        "no_extension_examples": no_ext_paths[:10]
    }

    logging.info("Skipped files summary: total_skipped=%d", total_skipped)
    for ext, cnt in sorted_ext:
        logging.info("Skipped extension %s: %d occurrences", ext, cnt)

    if stats["no_extension_count"]:
        logging.info("Files without extension: %d (ELF detected: %d)", stats["no_extension_count"], stats["no_extension_elf_count"]) 

    # Persist statistics to the skipped error list path for further analysis
    try:
        write_json_output(stats, SKIPPED_ERROR_LIST_FILE_PATH)
    except Exception as e:
        logging.warning(f"Could not write skipped error statistics to {SKIPPED_ERROR_LIST_FILE_PATH}: {e}")

    # Additionally compute path-frequency statistics for files under the extracted ALL_FILES root
    try:
        all_files_prefix = None
        try:
            all_files_prefix = os.path.abspath(EXTRACTION_ALL_FILES_PATH)
        except Exception:
            logging.error(f"Could not resolve absolute path for ALL_FILES: {EXTRACTION_ALL_FILES_PATH}")
            return

        path_counts = defaultdict(int)
        path_samples = defaultdict(list)

        for p in skipped_paths:
            try:
                abs_p = os.path.abspath(p)
            except Exception:
                continue
            if not abs_p.startswith(all_files_prefix + os.sep) and abs_p != all_files_prefix:
                # skip files not under the ALL_FILES prefix
                continue

            # compute relative components under ALL_FILES
            rel = abs_p[len(all_files_prefix):].lstrip(os.sep)
            parts = rel.split(os.sep)
            # drop trailing filename if present
            if len(parts) == 0:
                continue
            # if last component contains a dot and looks like a filename, exclude it from directory components
            dir_parts = parts[:-1] if len(parts) > 1 else []

            # create subpath fragments of depth 1..4 starting at any position to capture patterns like /etc/security
            max_depth = 4
            for i in range(len(dir_parts)):
                for depth in range(1, min(max_depth, len(dir_parts) - i) + 1):
                    sub = "/" + "/".join(dir_parts[i:i+depth])
                    path_counts[sub] += 1
                    if len(path_samples[sub]) < 5:
                        path_samples[sub].append(abs_p)

        # Prepare sorted list of path frequencies (most common first)
        sorted_paths = sorted(path_counts.items(), key=lambda kv: kv[1], reverse=True)
        stats["all_files_prefix"] = all_files_prefix
        stats["all_files_path_counts"] = dict(sorted_paths[:200])
        stats["all_files_path_samples"] = {k: v for k, v in path_samples.items() if k in dict(sorted_paths[:200])}

        logging.info("Top skipped paths under ALL_FILES:")
        for path_key, count in sorted_paths[:20]:
            logging.info("%s: %d", path_key, count)

        # persist extended stats as well
        try:
            write_json_output(stats, SKIPPED_ERROR_LIST_FILE_PATH)
        except Exception:
            pass
    except Exception as e:
        logging.debug(f"Error while computing ALL_FILES path frequency stats: {e}")

    return stats

def inject(aosp_path, source_folder_path, target_out_path, executor, lunch_target, firmware_id, pre_injector_package_list, cookies, aosp_version):
    start_time = time.time()
    logging.info(f"Injection started at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time))}")

    target_obj_path = build_intermediate_file_index(aosp_path, target_out_path)

    error_list, inj_obj_list, inj_partition_list = process_partitions(aosp_path,
                                                                      source_folder_path,
                                                                      target_out_path,
                                                                      executor,
                                                                      lunch_target,
                                                                      pre_injector_package_list,
                                                                      firmware_id,
                                                                      cookies,
                                                                      aosp_version)
    # Verify Direct Injection targets exist on filesystem and log errors for missing targets
    try:
        direct_inject_errors = verify_direct_injection_targets(inj_partition_list)
        if direct_inject_errors:
            error_list.extend(direct_inject_errors)
    except Exception as e:
        logging.error(f"Error while verifying Direct Injection targets: {e}")
    end_time = time.time()

    # free caches
    INTERMEDIATE_MD5_MAP = None

    try:
        if 'target_obj_path' in locals():
            with FILE_INDEX_LOCK:
                if target_obj_path in FILE_INDEX_CACHE:
                    del FILE_INDEX_CACHE[target_obj_path]
    except Exception:
        pass

    logging.info(f"Injection ended at {end_time}")
    execution_time = end_time - start_time
    execution_time_minutes = execution_time / 60
    logging.info(f"Objects injected:")
    app_list = []
    apex_list = []
    libs_list = []
    skipped_app_list = []
    skipped_apex_list = []
    skipped_libs_list = []
    if PRINT_ALL_LOGS:
        for obj in inj_obj_list:
            logging.info(f"Indirect Inject via obj: {obj}")
            if isinstance(obj, tuple) and any(".apk" in str(element) for element in obj):
                file_name = os.path.basename(obj[0])
                app_list.append(file_name)
            elif isinstance(obj, tuple) and any(".apex" in str(element) for element in obj):
                file_name = os.path.basename(obj[0])
                apex_list.append(file_name)
            elif isinstance(obj, tuple) and any(".so" in str(element) for element in obj):
                file_name = os.path.basename(obj[0])
                libs_list.append(file_name)

        logging.info(f"Partition files injected:")
        for obj in inj_partition_list:
            logging.info(f"Direct Inject: {obj}")
            if isinstance(obj, tuple) and any(".apk" in str(element) for element in obj):
                file_name = os.path.basename(obj[0])
                app_list.append(file_name)
            elif isinstance(obj, tuple) and any(".apex" in str(element) for element in obj):
                file_name = os.path.basename(obj[0])
                apex_list.append(file_name)
            elif isinstance(obj, tuple) and any(".so" in str(element) for element in obj):
                file_name = os.path.basename(obj[0])
                libs_list.append(file_name)
    if PRINT_ERROR_LOGS:
        logging.info(f"Errors:")
        for obj in error_list:
            match = re.search(r":\s*(/[^|]+)\s*\|", obj)
            if match:
                file_path = match.group(1).strip()
                file_name = os.path.basename(file_path)
                if ".apk" in obj:
                    skipped_app_list.append(file_name)
                if ".apex" in obj:
                    skipped_apex_list.append(file_name)
                if ".so" in obj:
                    skipped_libs_list.append(file_name)

    logging.info(f"============================== ERRORS ==============================")
    for error in error_list:
        logging.info(f"{error}")
        if "_apex" in error or "_capex" in error:
            error_list.remove(error)
    logging.info(f"============================================================")

    logging.info(f"Execution time: {execution_time_minutes} minutes")
    number_of_files = count_number_of_extracted_files(source_folder_path)
    logging.info(f"Number of File in ALL_FILES: {number_of_files}")
    logging.info(f"Number of errors: {len(error_list)}")
    logging.info(f"Number of objects injected: {len(inj_obj_list)}")
    logging.info(f"Number of partition files injected: {len(inj_partition_list)}")
    logging.info(f"Number of files processed: {len(error_list) + len(inj_obj_list) + len(inj_partition_list)}")

    logging.info(f"\n\nInjected Apps/APEX/Libraries Summary:")
    logging.info(f"Post-Injection Apps injected: {app_list}\n\n")
    logging.info(f"Post-Injection APEX injected: {apex_list}\n\n")
    logging.info(f"Post-Injection Libraries injected: {libs_list}\n\n")
    logging.info(f"\nSkipped Apps/APEX/Libraries Summary:")
    logging.info(f"Post-Injection Libraries skipped: {skipped_libs_list}\n\n")
    logging.info(f"Post-Injection Apps skipped: {skipped_app_list}\n\n")
    logging.info(f"Post-Injection APEX skipped: {skipped_apex_list}\n\n")

    grouped_errors, error_sample_list = group_errors_by_prefix(error_list)

    logging.info(f"Grouped Errors:")
    for prefix, count in grouped_errors.items():
        logging.info(f"{prefix} {count} occurrences")

    for prefix, sample in error_sample_list.items():
        logging.info(f"Sample Error for {prefix}: {sample}")

    file_type_frequencies = extract_file_type_frequencies(error_list)

    logging.info(f"File Type Frequencies:")
    for file_type, count in file_type_frequencies.items():
        logging.info(f".{file_type}: {count} occurrences")

    result = {
        "hostname": os.uname()[1],
        "firmware_id": firmware_id,
        "method": "start_post_build_injector",
        "start_time": start_time,
        "end_time": end_time,
        "duration_minutes": execution_time_minutes,
        "duration_seconds": execution_time,
        "errors": len(error_list),
        "objects_injected": len(inj_obj_list),
        "partition_files_injected": len(inj_partition_list),
        "files_injected": len(inj_obj_list) + len(inj_partition_list),
        "errors_file_type_frequencies": file_type_frequencies,
        "errors_grouped": grouped_errors,
    }

    write_json_output(result, PATH_EXECUTION_TIME_LOG)
    stats = create_error_statistics(error_list)
    if stats:
        write_json_output(stats, SKIPPED_ERROR_LIST_FILE_PATH)



def get_folders(directory_path):
    folders = []
    for entry in os.listdir(directory_path):
        full_path = os.path.join(directory_path, entry)
        if os.path.isdir(full_path):
            folders.append(full_path)
    return folders


def process_partitions(aosp_path, source_folder_path, target_out_path, executor, lunch_target, pre_injector_package_list, firmware_id, cookies, aosp_version):
    folder_path_list = get_folders(source_folder_path)
    logging.info(f"Folder path list: {folder_path_list}")
    combined_error_list = []
    combined_inj_obj_list = []
    combined_inj_partition_list = []

    for folder_path in tqdm(folder_path_list, desc="Processing partitions"):
        error_list, inj_obj_list, inj_partition_list = process_partition_files(aosp_path,
                                                                               folder_path,
                                                                               target_out_path,
                                                                               executor,
                                                                               lunch_target,
                                                                               pre_injector_package_list,
                                                                               firmware_id,
                                                                               cookies,
                                                                               aosp_version)
        combined_error_list.extend(error_list)
        combined_inj_obj_list.extend(inj_obj_list)
        combined_inj_partition_list.extend(inj_partition_list)

    return combined_error_list, combined_inj_obj_list, combined_inj_partition_list


def process_file_concurrently(aosp_path, file_path, partition_name, target_out_path, lunch_target, pre_injector_package_list, firmware_id, cookies, aosp_version):
    inj_obj = None
    inj_partition = None
    error_message = None
    apex_merge_file_path_list = None
    lock_path = f"{file_path}.fmd-aecs-lock"
    processed_marker = f"{file_path}.fmd-aecs-processed"
    lock = FileLock(lock_path)

    if os.path.exists(processed_marker):
        return f"File already processed: {file_path}", None, None

    try:
        with ((((lock)))):
            if os.path.exists(processed_marker):
                return f"File already processed: {file_path}", None, None
            module_type, tmp_module_type = get_module_type(file_path,
                                          pre_injector_package_list=pre_injector_package_list,
                                          post_injector_config=POST_INJECTOR_CONFIG)

            #with processed_files_lock:
                #if file_path in processed_files:
                #    return f"File already processed: {file_path}", None, None
                #processed_files.add(file_path)

            if module_type in ["SKIPPED"]:
                error_message = f"Skipped File post-inject (Keyword/Extension/Filename): {file_path} | module_type: {module_type}"
                logging.info(error_message)
            else:
                logging.debug(f"Processing file {file_path}")
                filename = os.path.basename(file_path)
                file_extension = os.path.splitext(file_path)[1]
                if module_type == "APPS" and file_extension.lower() == ".apk":
                    error_message = handle_app_modules(file_path, aosp_path, firmware_id, cookies)
                elif file_extension.lower() == ".apex" or file_extension.lower() == ".capex":
                    if file_path.endswith(".capex"):
                        file_path = replace_capex_with_apex(file_path)
                    if "tzdata" in file_path or "tzdata" in filename:
                        new_name = re.sub(r'tzdata\d+', 'tzdata', filename)
                        file_path = rename_file(file_path, new_name)
                    if aosp_version and float(aosp_version) > 12:
                        if "bluetooth" in filename:
                            new_name = filename.replace("bluetooth", "btservices")
                            file_path = rename_file(file_path, new_name)

                    allow_merge = True
                    if "vndk" in filename and not "current" in filename:
                        allow_merge = False

                    if POST_INJECTOR_CONFIG["ALLOW_APEX_INJECTION_MERGE"] \
                        and any(keyword in filename for keyword in POST_INJECTOR_CONFIG["ALLOW_APEX_MERGE_KEYWORD_LIST"]) \
                        and "ALL_FILES/system/" in file_path \
                        and allow_merge:
                        logging.info(f"Handle APEX file: {file_path} with module type: {module_type}")
                        try:
                            is_merge_success, log_message, apex_merge_file_path_list = handle_apex_modules(file_path, aosp_path, lunch_target, target_out_path, aosp_version)
                        except Exception as e:
                            is_merge_success = False
                            log_message = f"Exception occurred: {e}:{traceback.format_exc()}\n{traceback.print_stack()}"

                        if not is_merge_success:
                            error_message = f"Error handling merge APEX file: {file_path}|{log_message}"
                            raise Exception(error_message)
                        else:
                            error_message = None
                    else:
                        try:
                            is_repack_success, log_message = repackage_apex_file(aosp_path, file_path, lunch_target, aosp_version)
                            if not is_repack_success:
                                error_message = f"Error handling repack APEX file: {file_path}|{log_message}"
                            else:
                                error_message = None
                        except Exception as e:
                            error_message = f"Exception occurred: {e}:{traceback.format_exc()}\n{traceback.print_stack()}"
                            is_repack_success = False
                elif module_type == "EXECUTABLES" and is_elf_binary(file_path):
                    if filename in POST_INJECTOR_CONFIG["APEX_BINARY_ISOLATED_NAMESPACE_LIST"]:
                        try:
                            is_apex_add_success, log_message = add_new_apex_file(aosp_path,
                                                                                 file_path,
                                                                                 lunch_target,
                                                                                 partition_name,
                                                                                 aosp_version)
                        except Exception as e:
                            is_apex_add_success = False
                            log_message = f"Exception occurred: {e}:{traceback.format_exc()}"
                        if not is_apex_add_success:
                            error_message = f"Error adding APEX file: {file_path}|{log_message}"
                        else:
                            error_message = None

                if not error_message:
                    inj_obj, inj_partition = search_and_inject(partition_name, module_type, file_path, target_out_path, aosp_path, lunch_target, aosp_version, apex_merge_file_path_list)
                else:
                    logging.info(f"File not further processed: {file_path} | {error_message}")
    except Exception as e:
        error_message = f"{e}:{traceback.format_exc()}"
    finally:
        with open(processed_marker, 'w') as marker:
            marker.write("")

    result = error_message, inj_obj, inj_partition
    return result

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
        logging.info(f"File renamed to: {new_file_path}")
        return new_file_path
    except Exception as e:
        logging.error(f"Error renaming file {file_path} to {new_name}: {e}")
        raise



def handle_app_modules(file_path, aosp_path, firmware_id, cookies):
    error_message = None
    signing_success, output, subprocess_error_message = handle_apk_signing(file_path, aosp_path, firmware_id, cookies)
    if not signing_success:
        error_message = f"Error signing APK file: {file_path}|{subprocess_error_message}"
    return error_message


def replace_capex_with_apex(file_path):
    logging.info(f"APEX file is capex: {file_path}")
    input_folder_path = os.path.dirname(file_path)
    capex_filename = os.path.basename(file_path)
    extracted_apex_file_path = prepare_capex(file_path, input_folder_path,
                                             capex_filename.replace(".capex", ".apex"))
    if extracted_apex_file_path:
        logging.info(f"APEX file extracted from CAPEX: {extracted_apex_file_path}")
        new_filename = f"{capex_filename}.original_capex"
        rename_file(file_path, new_filename)
        file_path = extracted_apex_file_path
    return file_path

def delete_intermediate_cached_files(target_file_injection_path, aosp_version, aosp_path):
    try:
        os.remove(target_file_injection_path)
        logging.info(f"Removed file before indirect injection: {target_file_injection_path}")
    except Exception as e:
        logging.warning(f"Could not remove file before indirect injection: {target_file_injection_path} | {e}")


def find_intermediate_file(aosp_path, md5_original_file):
    """
    Find intermediate files matching the provided MD5.

    If an immutable precomputed mapping (INTERMEDIATE_MD5_MAP) exists it will be used
    for fast lookups. Otherwise, the function will walk the intermediates folder and
    compute MD5s on the fly (legacy fallback).
    """
    global INTERMEDIATE_MD5_MAP

    # Normalize md5 to lowercase for comparison
    target_md5 = (md5_original_file or '').strip().lower()

    # Start timing measurement for this lookup
    start_time = time.time()

    # If a precomputed mapping exists, use it for O(1) lookup
    if INTERMEDIATE_MD5_MAP:
        try:
            matched = INTERMEDIATE_MD5_MAP.get(target_md5, ())
            logging.info(f"Found intermediate candidates with same md5 hash: {matched}")
            # record timing for the map lookup
            if MEASURE_LOOKUP_PERFORMANCE and ENABLE_INJECTION_PERFORMANCE_LOG:
                try:
                    write_json_nd_output({
                        "function": "find_intermediate_file",
                        "method": "map_lookup",
                        "target_md5": target_md5,
                        "matches": len(matched) if matched is not None else 0,
                        "duration_seconds": time.time() - start_time,
                        "timestamp": time.time()
                    }, PATH_MAPPING_EXECUTION_TIME_LOG)
                except Exception:
                    logging.warning("Could not write find_intermediate_file timing to log")

            # Return a mutable list for compatibility with callers
            return list(matched)
        except Exception:
            logging.warning("Error while using INTERMEDIATE_MD5_MAP, falling back to on-the-fly search")

    logging.info("Using fallback intermediate file overwrite")
    # Fallback: walk and compute md5s as before
    intermediates_path = str(os.path.join(aosp_path, "out/soong/.intermediates/"))
    matching_intermediate_file_list = []
    if not os.path.exists(intermediates_path):
        logging.debug(f"Intermediates path does not exist: {intermediates_path}")
        # record timing (no files)
        if MEASURE_LOOKUP_PERFORMANCE:
            try:
                write_json_nd_output({
                    "function": "find_intermediate_file",
                    "method": "walk",
                    "target_md5": target_md5,
                    "matches": 0,
                    "duration_seconds": time.time() - start_time,
                    "timestamp": time.time()
                }, PATH_EXECUTION_TIME_LOG)
            except Exception:
                logging.warning("Could not write find_intermediate_file timing to log")
        return matching_intermediate_file_list


    for root, dirs, files in os.walk(intermediates_path):
        for fname in files:
            try:
                file_path = os.path.join(root, fname)
                logging.debug(f"Found intermediate APEX candidate: {file_path}")
                md5sum = get_md5_from_file(file_path)
                if not md5sum:
                    continue
                if md5sum.strip().lower() == target_md5:
                    logging.info(f"Matched intermediate APEX by md5: {file_path}")
                    matching_intermediate_file_list.append(file_path)
            except Exception as e:
                logging.warning(f"Error while checking intermediate file {fname} in {root}: {e}")
                continue
    # record timing for the walk-based search
    if MEASURE_LOOKUP_PERFORMANCE and ENABLE_INJECTION_PERFORMANCE_LOG:
        try:
            write_json_nd_output({
                "function": "find_intermediate_file",
                "method": "walk",
                "target_md5": target_md5,
                "matches": len(matching_intermediate_file_list),
                "duration_seconds": time.time() - start_time,
                "timestamp": time.time()
            }, PATH_MAPPING_EXECUTION_TIME_LOG)
        except Exception:
            logging.warning("Could not write find_intermediate_file timing to log")

    return matching_intermediate_file_list


def build_intermediate_md5_map(aosp_path):
    """
    Build a mapping of md5 -> tuple(paths) for all files under out/soong/.intermediates/.
    The returned mapping is a read-only MappingProxyType where values are tuples to ensure immutability.
    """
    intermediates_path = str(os.path.join(aosp_path, "out/soong/.intermediates/"))
    md5_map = defaultdict(list)
    start_time = time.time()
    total_bytes_hashed = 0
    files_hashed = 0
    method = "parallel"
    if not os.path.exists(intermediates_path):
        logging.debug(f"Intermediates path does not exist (build map): {intermediates_path}")
        return MappingProxyType({})

    logging.info(f"Building intermediate md5 map from: {intermediates_path}")
    # Try to parallelize hashing to utilize multiple cores and overlap I/O
    # Determine worker count conservatively
    try:
        cpu = os.cpu_count() or 1
        workers = min(32, max(2, cpu * 2))
    except Exception:
        workers = 4

    # We will submit hashing tasks while scanning to avoid building a huge list first
    futures = {}
    try:
        with Executor(max_workers=workers) as ex:
            for root, dirs, files in os.walk(intermediates_path):
                for fname in files:
                    file_path = os.path.join(root, fname)
                    try:
                        futures[ex.submit(get_md5_from_file, file_path)] = file_path
                    except Exception as e:
                        logging.warning(f"Failed to submit hashing job for {file_path}: {e}")

            # Collect results as they complete
            for fut in as_completed(futures):
                src_path = futures.get(fut)
                try:
                    md5sum = fut.result()
                    if not md5sum:
                        continue
                    md5_key = md5sum.strip().lower()
                    md5_map[md5_key].append(src_path)
                    # update counters
                    try:
                        sz = os.path.getsize(src_path)
                        total_bytes_hashed += sz
                    except Exception:
                        sz = 0
                    files_hashed += 1
                except Exception as e:
                    logging.warning(f"Error while hashing intermediate file {src_path}: {e}")
    except Exception:
        # Fallback to serial processing if parallel execution fails for any reason
        logging.warning("Parallel hashing failed, falling back to serial md5 computation")
        method = "serial"
        for root, dirs, files in os.walk(intermediates_path):
            for fname in files:
                try:
                    file_path = os.path.join(root, fname)
                    md5sum = get_md5_from_file(file_path)
                    if not md5sum:
                        continue
                    md5_key = md5sum.strip().lower()
                    md5_map[md5_key].append(file_path)
                    try:
                        sz = os.path.getsize(file_path)
                        total_bytes_hashed += sz
                    except Exception:
                        pass
                    files_hashed += 1
                except Exception as e:
                    logging.warning(f"Error while hashing intermediate file {fname} in {root}: {e}")
                    continue

    # Convert lists to tuples for immutability and wrap mapping in MappingProxyType
    immutable_map = {k: tuple(v) for k, v in md5_map.items()}
    # record timing and throughput metrics for building the md5 map
    if ENABLE_INJECTION_PERFORMANCE_LOG:
        try:
            duration = time.time() - start_time
            mb_hashed = total_bytes_hashed / (1024.0 * 1024.0)
            files_per_sec = files_hashed / duration if duration > 0 else None
            mb_per_sec = mb_hashed / duration if duration > 0 else None
            write_json_nd_output({
                "function": "build_intermediate_md5_map",
                "aosp_path": aosp_path,
                "intermediates_path": intermediates_path,
                "method": method,
                "workers": workers,
                "files_hashed": files_hashed,
                "total_bytes_hashed": total_bytes_hashed,
                "duration_seconds": duration,
                "throughput_files_per_second": files_per_sec,
                "throughput_mb_per_second": mb_per_sec,
                "timestamp": time.time()
            }, PATH_MAPPING_EXECUTION_TIME_LOG)
        except Exception:
            logging.debug('Could not write build_intermediate_md5_map timing to log')
    return MappingProxyType(immutable_map)

def indirect_injection(target_file_injection_path,
                       file_name,
                       target_out_path,
                       partition_name,
                       module_type,
                       file_path,
                       inj_partition,
                       aosp_path,
                       lunch_target,
                       aosp_version):
    start_time = time.time()

    file_ext = os.path.splitext(file_name)[1]
    if file_ext in POST_INJECTOR_CONFIG["SKIPPED_FILE_EXTENSION_LIST_INDIRECT_INJECTION"]:
        logging.info(f"Skipped indirect injection for file: {file_path} with extension: {file_ext}")
        return None, inj_partition, False

    if not (file_name in POST_INJECTOR_CONFIG["ALLOW_FILE_INJECT_ALWAYS"]
            or any(keyword in file_path for keyword in POST_INJECTOR_CONFIG["ALLOW_FILE_INJECT_ALWAYS_KEYWORD_LIST"])):

        if POST_INJECTOR_CONFIG["ENABLE_SHARED_LIBRARIES_INJECTION_IF_NOT_EXISTS"] and file_ext == ".so":
            if os.path.exists(target_file_injection_path):
                logging.info(f"Skipped indirect injection for shared library file: {file_path} as "
                             f"ENABLE_SHARED_LIBRARIES_INJECTION_IF_NOT_EXISTS is set.")
                return None, inj_partition, False
            else:
                logging.info(f"Allow indirect injection for shared library file: {file_path}")
        else:
            logging.info(f"Indirect injection for shared library file: {file_path}")


    delete_intermediate_cached_files(target_file_injection_path, aosp_version, aosp_path)

    logging.info(f"File exists in target path: {target_file_injection_path}. Continue with indirect injection of {file_path}.")
    inj_obj = None
    original_file_path = None
    # Indirect Injection
    if file_name in POST_INJECTOR_CONFIG["INDIRECT_INJECTION_FILE_MAPPING"].keys():
        logging.info(f"Indirect injection for file with manual mapping: {file_path}")
        if not check_file_compatibility(file_path, target_file_injection_path, module_type):
            logging.debug(f"Incompatible file for indirect injection: {file_path} | {file_name} | {module_type} | {target_file_injection_path}")
        else:
            file_map_entry = POST_INJECTOR_CONFIG["INDIRECT_INJECTION_FILE_MAPPING"][file_name]
            logging.info(f"Found mapping entry for file: {file_path} | {file_name} | {module_type} | {target_file_injection_path} | mapping: {file_map_entry}")
            if file_map_entry["injection_path"] in file_path:
                original_file_path = os.path.join(target_out_path, file_map_entry["original_path"])
    else:
        original_file_path = search_original_file_in_obj(partition_name,
                                                         module_type,
                                                         file_path,
                                                         file_name,
                                                         target_out_path)

    if original_file_path is None:
        file_path_vendor_replaced = remove_vendor_name_from_path(file_path)
        file_name_vendor_replaced = os.path.basename(file_path_vendor_replaced)
        original_file_path = search_original_file_in_obj(partition_name,
                                                         module_type,
                                                         file_path_vendor_replaced,
                                                         file_name_vendor_replaced,
                                                         target_out_path)

    is_injected = False
    if original_file_path:
        try:
            logging.info(f"Original file path type: {type(original_file_path).__name__}: {original_file_path}")
        except Exception:
            logging.debug("Could not determine original_file_path type")
        if isinstance(original_file_path, list):
            original_file_path_list = original_file_path
            logging.info(f"Indirect injection with multiples files: {original_file_path_list} | {file_path} | {file_name}")
            for overwrite_path in original_file_path_list:
                is_injected = inject_file_into_obj(file_path, overwrite_path, module_type, aosp_path, partition_name, lunch_target, aosp_version)
                inj_obj = (file_path, overwrite_path, module_type)
        else:
            logging.info(f"Indirect injection with singles file: {original_file_path} | {file_path} | {file_name} ")
            is_injected = inject_file_into_obj(file_path, original_file_path, module_type, aosp_path, partition_name, lunch_target, aosp_version)
            inj_obj = (file_path, original_file_path, module_type)
    else:
        is_injected = False
        error_message = f"Original file not found for indirect injection: {file_path} | {file_name}"
        logging.error(error_message)
        inj_obj = (error_message, None, module_type)

    # record timing
    if ENABLE_INJECTION_PERFORMANCE_LOG:
        duration = time.time() - start_time
        try:
            write_json_nd_output({
                'function': 'indirect_injection',
                'file': file_path,
                'target_file': target_file_injection_path,
                'method': 'indirect',
                'duration_seconds': duration,
                'timestamp': time.time(),
                'injected': bool(is_injected)
            }, PATH_MAPPING_EXECUTION_TIME_LOG)
        except Exception:
            logging.debug('Could not write indirect_injection timing to log')

    logging.info(f"Indirect injection result for file: {file_path} | injected: {is_injected} | target_file: {target_file_injection_path}")
    return inj_obj, inj_partition, is_injected


def search_and_inject(partition_name, module_type, file_path, target_out_path, aosp_path, lunch_target, aosp_version, apex_merge_file_path_list=None):
    inj_partition = None
    inj_obj = None
    target_path = None
    is_injected = False
    file_name = os.path.basename(file_path)
    file_extension = os.path.splitext(file_name)[1]

    target_file_injection_path, target_partition_path = get_target_injection_path(file_path, partition_name, target_out_path)
    logging.debug(f"Target file injection path: {target_file_injection_path} ")
    if file_extension == ".apex" or file_extension == ".capex":
        logging.debug(f"APEX Injection Strategy Selection for file: {file_path}")
        if (not os.path.exists(target_file_injection_path)
                and not any(keyword in os.path.basename(target_file_injection_path)
                            for keyword in POST_INJECTOR_CONFIG["ALLOW_APEX_MERGE_KEYWORD_LIST"])):
            # Direct Injection
            target_path, is_injected = inject_file_into_partition(file_path, target_file_injection_path, aosp_path, partition_name, lunch_target, aosp_version)
            inj_partition = (file_path, target_path, module_type)
            if is_injected:
                logging.info(f"Direct injection complete for: {file_path}")
            else:
                logging.info(f"Direct injection did not inject for: {file_path}")
        else:
            inj_obj, inj_partition, is_injected = indirect_injection(target_file_injection_path, file_name, target_out_path,
                                                        partition_name, module_type, file_path, inj_partition, aosp_path, lunch_target, aosp_version)
            if is_injected and apex_merge_file_path_list:
                inject_apex_intermediate_files(file_name, file_path, apex_merge_file_path_list, partition_name, target_out_path, aosp_path, lunch_target, aosp_version)


    elif not os.path.exists(target_file_injection_path):
        # Direct Injection
        target_path, is_injected = inject_file_into_partition(file_path, target_file_injection_path, aosp_path, partition_name, lunch_target, aosp_version)
        inj_partition = (file_path, target_path, module_type)
        if is_injected:
            logging.info(f"Direct injection complete for: {file_path}")
        else:
            logging.warning(f"Direct injection did not inject file, not fallback to indirect injection: {file_path}")
    else:
        inj_obj, inj_partition, is_injected = indirect_injection(target_file_injection_path, file_name, target_out_path,
                                                    partition_name, module_type, file_path, inj_partition, aosp_path, lunch_target, aosp_version)
        if is_injected:
            logging.info(f"Indirect injection complete for file: {file_path}")

        if not is_injected and is_injected is not None:
            logging.info(f"Fallback to Direct injection: {file_path}")
            # Fallback to Direct Injection
            target_path, is_injected = inject_file_into_partition(file_path, target_file_injection_path, aosp_path, partition_name, lunch_target, aosp_version)
            inj_partition = (file_path, target_path, module_type)
            if is_injected:
                logging.info(f"Direct injection complete for file: {file_path}")
            else:
                logging.warning(f"Direct+Indirect injection complete for file but file not injected: {file_path}")


    if file_name in POST_INJECTOR_CONFIG["COPY_TO_SPECIFIC_PATH"]:
        dst_path = os.path.join(target_partition_path, POST_INJECTOR_CONFIG["COPY_TO_SPECIFIC_PATH"][file_name])
        if not os.path.exists(dst_path):
            if file_name == "MitypeVF.ttf":
                test_data = "/system/fonts/Roboto-Regular.ttf"
                try:
                    with open(dst_path, "w") as f:
                        f.write(test_data)
                    logging.info(f"COPY_TO_SPECIFIC_PATH: {file_path} to {dst_path}")
                except Exception as e:
                    logging.error(f"Error copying file for COPY_TO_SPECIFIC_PATH: {file_path} to {dst_path} | {e}")
            else:
                copy_fast(file_path, dst_path)
        else:
            logging.info(f"SKIPPED COPY_TO_SPECIFIC_PATH: {file_path}|{dst_path} already exists")


    if target_path:
        try:
            md5sum = hashlib.md5(target_path).hexdigest()
            inj_partition = (inj_partition[0], inj_partition[1], inj_partition[2], md5sum)
        except Exception:
            pass

    return inj_obj, inj_partition


def inject_apex_intermediate_files(file_name, file_path, apex_merge_file_path_list, partition_name, target_out_path, aosp_path, lunch_target, aosp_version):
    for apex_sub_file_path in apex_merge_file_path_list:
        module_type, tmp_module_type = get_module_type(apex_sub_file_path, post_injector_config=POST_INJECTOR_CONFIG)
        sub_file_name = os.path.basename(apex_sub_file_path)
        apex_filename_no_ext = str(os.path.splitext(file_name)[0])
        original_file_path = search_original_file_in_obj(partition_name,
                                                         module_type,
                                                         apex_sub_file_path,
                                                         sub_file_name,
                                                         target_out_path,
                                                         replace_intermediate=f".{apex_filename_no_ext}_intermediates",
                                                         must_contain=apex_filename_no_ext)
        is_injected = inject_file_into_obj(file_path,
                                           original_file_path,
                                           module_type,
                                           aosp_path,
                                           partition_name,
                                           lunch_target,
                                           aosp_version)
        if is_injected:
            logging.info(f"APEX Intermediate: Indirect injection complete for file: {file_path}")
        else:
            logging.error(f"APEX INtermediate: Indirect injection failed for file: {file_path}")


def cleanup_files(directory):
    """
    Remove all .lock and .processed files in the given directory and its subdirectories.

    :param directory: str - path to the directory to clean up.
    """
    logging.info(f"Cleaning up all .lock files in {directory}")
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith('.fmd-aecs-lock') or file.endswith('.fmd-aecs-processed'):
                file_path = os.path.join(root, file)
                try:
                    os.remove(file_path)
                    #logging.info(f"Removed file: {file_path}")
                except Exception as e:
                    logging.error(f"Error removing file {file_path}: {e}")


def process_partition_files(aosp_path, folder_path, target_out_path, executor, lunch_target, pre_injector_package_list, firmware_id, cookies, aosp_version):
    logging.debug(f"Processing {folder_path} into {target_out_path}")
    logging.debug(f"AOSP Path: {aosp_path} "
                  f"| Target Out Path: {target_out_path} "
                  f"| Lunch Target: {lunch_target} "
                  f"| Folder Path: {folder_path} "
                  f"| Pre Injector Package List: {len(pre_injector_package_list)}")
    error_list = []
    inj_obj_list = []
    inj_partition_list = []
    processed_files = set()
    partition_name = os.path.basename(folder_path)

    file_paths = list(set(os.path.join(root, file_name.strip()) for root, _, file_name_list in scandir_walk(folder_path)
                          for file_name in file_name_list))
    logging.debug(f"Found {len(file_paths)} files in {folder_path} for post-injection...")

    # Initialize tqdm progress bar
    progress_bar = tqdm(total=len(file_paths), desc=f"Processing files in partition: {partition_name}")

    future_dict = {}
    skip_counter = 0
    for file_path in file_paths:
        with processed_files_lock:
            if file_path in processed_files:
                skip_counter += 1
                continue
            processed_files.add(file_path)

        logging.debug(f"Submitting file for injection: {file_path} | Partition: {partition_name} "
                     f"| Target Out Path: {target_out_path} | Lunch Target: {lunch_target} | Length of pre_injector_package_list: {len(pre_injector_package_list)}")
        future = executor.submit(process_file_concurrently, aosp_path, file_path, partition_name, target_out_path, lunch_target, pre_injector_package_list, firmware_id, cookies, aosp_version)
        future_dict[future] = file_path

    logging.debug(f"Finished processing {len(processed_files)}/{len(file_paths)} files in partition: {partition_name}. "
                 f"Skipped {skip_counter} files that were already processed.")

    for future in as_completed(future_dict):
        file_path = future_dict[future]
        try:
            result = future.result()
            if result[0]:  # If there's an error
                error_list.append(result[0])
            if result[1]:  # Indirect Injection
                inj_obj_list.append(result[1])
            if result[2]:  # Direct Injection
                inj_partition_list.append(result[2])
        except Exception as exc:
            logging.error(f"Error processing file {file_path}: {exc}")
            error_list.append(str(exc))
        finally:
            if future.exception():
                logging.error(f"Future for file {file_path} raised an exception: {future.exception()}")
            progress_bar.update(1)

    progress_bar.close()
    #handle_duplicated_permissions(target_out_path)
    cleanup_files(folder_path)
    # Wait for any scheduled non-blocking copy tasks to finish before moving on
    try:
        wait_for_all_copy_tasks()
    except Exception as e:
        logging.warning(f"Error waiting for copy tasks to finish: {e}")



    return error_list, inj_obj_list, inj_partition_list


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

def is_parent_dir_arm_and_target_arm(file_path, candidate_path):
    """
    Prevent matching of arm to arm64 and vice versa.
    """
    parent_dir_file_path = os.path.basename(os.path.dirname(file_path))
    parent_dir_candidate = os.path.basename(os.path.dirname(candidate_path))

    is_match = False

    if parent_dir_file_path == "arm64":
        if "arm64" in parent_dir_candidate:
            is_match = True
    elif parent_dir_file_path == "arm":
        if not "arm64" in parent_dir_candidate and "arm" in parent_dir_candidate:
            is_match = True

    logging.debug(f"Checking parent dir: {parent_dir_file_path}|{parent_dir_candidate} for {file_path}|{candidate_path}: "
                 f"result: {is_match}")
    return is_match


def get_all_files(directory):
    all_files = []
    for root, _, files in os.walk(directory):
        for file in files:
            all_files.append(os.path.join(root, file))
    return all_files


def verify_direct_injection_targets(inj_partition_list):
    """
    Verify that all Direct Injection target paths exist on the filesystem.

    :param inj_partition_list: list of tuples (source_path, target_path, module_type)
    :return: list of error message strings for missing targets
    """
    errors = []
    if not inj_partition_list:
        return errors

    for obj in inj_partition_list:
        try:
            # Expect tuples like: (source, target, module_type[, ...])
            if not isinstance(obj, tuple) or len(obj) < 2:
                logging.debug(f"Skipping malformed Direct Inject entry (not a tuple or too short): {obj}")
                continue
            source_path = obj[0]
            target_path = obj[1]
            module_type = obj[2] if len(obj) > 2 else None

            if not target_path or not os.path.exists(target_path):
                msg = f"Direct Injection target missing: {target_path} for source {source_path} | module_type: {module_type}"
                logging.error(msg)
                errors.append(msg)
        except Exception as e:
            logging.error(f"Error while verifying Direct Inject entry {obj}: {e}")
            errors.append(f"Error while verifying Direct Inject entry {obj}: {e}")

    return errors


def search_original_file_in_obj(partition_name,
                                module_type,
                                file_path,
                                file_name,
                                target_out_path,
                                replace_intermediate="_intermediates",
                                exact_match_files=True,
                                must_contain=""):
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
    if not target_obj_path.endswith("/"):
        target_obj_path += "/"

    if module_type not in ["MISC", "STATIC_CONFIG"]:
        search_folder_path = str(target_obj_path) + module_type
    else:
        search_folder_path = str(target_obj_path)

    if partition_name in ["super", "system"]:
        partition_name = ""

    result_file_path_list = []
    # instrumentation
    start_time = time.time()
    candidates_examined = 0
    matches_found = 0
    lookup_method = "unknown"
    # Try to use pre-built basename -> paths index for the target_obj_path to avoid repeated os.walk
    idx = None
    try:
        # FILE_INDEX_CACHE stores indexes keyed by the base target_obj_path
        base_obj_path = os.path.join(target_out_path, FOLDER_NAME_OBJECTS)
        idx = FILE_INDEX_CACHE.get(base_obj_path)
    except Exception:
        idx = None

    if idx is not None:
        # Gather candidate file paths for this basename
        file_path_list = list(idx.get(file_name, []))
        lookup_method = "index"
        if exact_match_files and not file_path_list:
            # No exact basename matches: filter by extension across the whole index
            file_extension = os.path.splitext(file_name)[1]
            # flatten all paths with the extension
            file_path_list = [p for paths in idx.values() for p in paths if os.path.splitext(p)[1] == file_extension]
            logging.info("File Matcher: No exact matches found for %s. Filtered by extension (%s) using index", file_name, file_extension)
    else:
        # Fallback: expensive full walk
        file_path_list = get_all_files(search_folder_path)
        lookup_method = "walk"
        file_name_list = [os.path.basename(file) for file in file_path_list]
        if exact_match_files:
            matches = [file_path_list[i] for i, name in enumerate(file_name_list) if name == file_name]
            if matches:
                for matching_file in matches[:]:
                    if not check_file_compatibility(file_path, matching_file, module_type):
                        logging.debug("File Matcher: File not compatible: %s|%s", file_path, matching_file)
                        matches.remove(matching_file)
                logging.info("File Matcher: Found exact matches for %s: %s", file_name, matches)
                file_path_list = matches
            else:
                file_extension = os.path.splitext(file_name)[1]
                file_path_list = [file for file in file_path_list if os.path.splitext(file)[1] == file_extension]
                logging.debug("File Matcher: No exact matches found for %s. Filtered by extension (%s)", file_name, file_extension)
    duration_first_part = time.time() - start_time

    module_name = os.path.splitext(file_name)[0]
    logging.debug("File Matcher:%s Searching in %s for module_name: %s and file_name: %s", module_type, search_folder_path, module_name, file_name)

    contains_vndk = "vndk" in file_path
    contains_ndk = "ndk" in file_path
    root_cache = {}
    for file in file_path_list:
        candidates_examined += 1
        candidate_path = file
        candidate_file_name = os.path.basename(candidate_path)
        # Strip the root folder name to match the module name
        root = os.path.dirname(file)
        if root not in root_cache:
            # Perform the expensive stripping logic only once per unique directory
            stripped = root.replace(replace_intermediate, "")
            stripped = stripped.replace(f"_{partition_name}", "")
            stripped = stripped.replace("v1_prebuilt", "")
            root_cache[root] = os.path.basename(stripped)

        root_folder_name_stripped = root_cache[root]
        logging.debug("File Matcher: %s:%s - Root Folder Name stripped: %s", module_name, file_name, root_folder_name_stripped)

        if ("vndk" in candidate_path and not contains_vndk) or ("ndk" in candidate_path and not contains_ndk):
            logging.debug("File Matcher: VNDK/NDK Rule enforced -> Candidate: %s has vndk but target not: %s", candidate_path, file_path)
            continue

        if not check_file_compatibility(file_path, candidate_path, module_type):
            logging.debug("File Matcher: File not compatible: %s|%s", file_path, candidate_path)
            continue

        # Check if there is an exact match for the file name
        if candidate_file_name == file_name:
            logging.debug("File Matcher test exact match:%s Found %s in %s", file_name, candidate_path, root)

            # Verify if it matches the partition criteria
            if not partition_name or partition_name in root:
                logging.debug("File Matcher: Found file that matches partition: %s, candidate_path: %s", file_name, candidate_path)

                if "com.android" in candidate_path and "com.android" not in file_path:
                    logging.debug("File Matcher: com.android rule enforced -> Candidate: %s has com.android but target not: %s", candidate_path, file_path)
                    continue

                logging.debug("File Matcher: File found via direct match: %s|%s", file_path, candidate_path)
                result_file_path = candidate_path
                result_file_path_list.append(result_file_path)
                matches_found += 1
        # Check if the folder has the same name but the file within the folder is named differently
        elif module_name == root_folder_name_stripped and partition_name in root:
            logging.debug("File Matcher: Found module name: %s:%s:%s with partition %s", module_name, file_name, root, partition_name)
            file_extension_src = os.path.splitext(file_name)[1]
            file_extension_obj = os.path.splitext(file)[1]

            if file_extension_src.lower().strip() == file_extension_obj.lower().strip():
                logging.debug("File Matcher: Found file: %s, candidate_path: %s", file_name, candidate_path)
                logging.debug("File Matcher: File found via module name: %s|%s", file_path, candidate_path)
                result_file_path = candidate_path
                result_file_path_list.append(result_file_path)
            elif ((file_extension_src == ".apex" and file_extension_obj == ".capex")
                  or (file_extension_src == ".capex" and file_extension_obj == ".apex")):
                # Matching apex to capex files
                if POST_INJECTOR_CONFIG["ALLOW_APEX_INJECTION_MERGE"]:
                    result_file_path = candidate_path
                    logging.debug("File Matcher: Found APEX file2: %s, result_file_path: %s", file_name, result_file_path)
                    result_file_path_list.append(result_file_path)
                    matches_found += 1
        else:
            logging.debug("File Matcher: File not found: %s", file_name)

    duration_second_part = time.time() - start_time

    # record timing and counts
    if ENABLE_INJECTION_PERFORMANCE_LOG:
        try:
            duration = time.time() - start_time
            write_json_nd_output({
                "function": "search_original_file_in_obj",
                "target_out_path": target_out_path,
                "search_folder_path": search_folder_path,
                "partition_name": partition_name,
                "module_type": module_type,
                "file_name": file_name,
                "lookup_method": lookup_method,
                "candidates_examined": candidates_examined,
                "matches_found": matches_found,
                "duration_seconds": duration,
                "duration_seconds_first_part": duration_first_part,
                "duration_seconds_second_part": duration_second_part,
                "timestamp": time.time()
            }, PATH_MAPPING_EXECUTION_TIME_LOG)
        except Exception:
            logging.debug('Could not write search_original_file_in_obj timing to log')

    result_file_path_list = [path for path in result_file_path_list if must_contain in path]

    if len(result_file_path_list) > 0:
        logging.info("File Matcher: Found file for %s in %s with partition %s and results: %s", file_name, search_folder_path, partition_name, result_file_path_list)
        return result_file_path_list
    else:
        logging.info("File Matcher: No file found for %s in %s with partition %s", file_name, search_folder_path, partition_name)
        return None


def check_file_compatibility(file_path, candidate_path, module_type):
    # Start high-resolution timer for compatibility check
    start_tc = time.perf_counter()
    is_match = True
    logging.debug(f"check_file_compatibility: {module_type}:{file_path}|{candidate_path}")

    if module_type in MODULE_TYPE_ABI_COMPATIBLE and is_elf_binary(file_path):
        logging.debug(f"File Matcher: Checking compatibility for {file_path}|{candidate_path}")
        candidate_arch = check_shared_object_architecture(candidate_path)
        file_arch = check_shared_object_architecture(file_path)
        logging.debug(f"File Matcher: ARCH {candidate_arch}|{file_arch} | {module_type}:{file_path}|{candidate_path}")
        if not is_abi_compatible(candidate_path, file_path):
            logging.debug(f"File Matcher: ABI not compatible: {file_path}|{candidate_path}")
            is_match = False

        if not check_shared_object_architecture(candidate_path) == check_shared_object_architecture(file_path):
            logging.debug(f"File Matcher: Shared object architecture not the same: {file_path}|{candidate_path}")
            is_match = False
        else:
            logging.debug(f"File Matcher: Shared object architecture is the same: {file_path}|{candidate_path}")

    if "arm" in file_path:
        if not is_parent_dir_arm_and_target_arm(file_path, candidate_path):
            logging.debug(f"File Matcher: Parent dir not arm: {file_path}|{candidate_path}")
            is_match = False

    if module_type == "JAVA_LIBRARIES":
        is_match = True


    if ENABLE_INJECTION_PERFORMANCE_LOG:
        # record timing for check_file_compatibility
        try:
            duration = time.perf_counter() - start_tc
            write_json_nd_output({
                "function": "check_file_compatibility",
                "module_type": module_type,
                "file_path": file_path,
                "candidate_path": candidate_path,
                "is_match": bool(is_match),
                "duration_seconds": duration,
                "timestamp": time.time()
            }, PATH_MAPPING_EXECUTION_TIME_LOG)
        except Exception:
            logging.debug('Could not write check_file_compatibility timing to log')

    return is_match


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
        file_extension = os.path.splitext(file_path)[1]
        if os.path.exists(file_path) \
            and not os.path.islink(file_path) \
            and os.path.isfile(file_path) \
            and (file_extension is None or file_extension == ".so"):
                os.chmod(file_path, os.stat(file_path).st_mode | stat.S_IEXEC)
        logging.debug(f"Set executable permission for file: {file_path}")
        return True
    except Exception as e:
        logging.warning(f"{e}")
        return False

def get_target_injection_path(source_file_path, partition_name, target_out_path):
    if partition_name == "super":
        partition_name = "system"

    filename = os.path.basename(source_file_path)
    file_extension = os.path.splitext(source_file_path)[1]

    target_partition_path = target_out_path + partition_name
    if not target_partition_path.endswith("/"):
        target_partition_path += "/"
    subfolder_list = get_subfolders(source_file_path, partition_name)
    if len(subfolder_list) == 0:
        target_dir_injection_path = target_partition_path
    else:
        # Adjust path for AOSP build directory structure
        if (file_extension in [".so", ".1", ".2", ".3", ".4", ".5", ".6", ".7", ".8", ".9"]
                and POST_INJECTOR_CONFIG["USE_ISOLATED_NAMESPACE"]
                and "/lib" in source_file_path
                and check_shared_object_architecture(source_file_path) == "64-bit"
                and filename in POST_INJECTOR_CONFIG["ISOLATED_NAMESPACE_NATIVE_LIBRARY_LIST"]):
            logging.info(f"File uses isolated namespace: {filename}|{source_file_path}")
            target_dir_injection_path = os.path.join(target_partition_path, "fmd")
        else:
            target_dir_injection_path = target_partition_path + str(os.path.join(*subfolder_list))

        target_dir_injection_path = target_dir_injection_path.replace("/system/system/", "/system/")
        target_dir_injection_path = target_dir_injection_path.replace("/system/system_ext/", "/system_ext/")
        target_dir_injection_path = target_dir_injection_path.replace("/system/vendor/", "/vendor/")
        target_dir_injection_path = target_dir_injection_path.replace("/system/product/", "/product/")

    if (not os.path.exists(target_dir_injection_path)
            and not os.path.islink(target_dir_injection_path)):
        logging.debug(f"Creating directory: {target_dir_injection_path}")
        os.makedirs(target_dir_injection_path, exist_ok=True)

    target_file_injection_path = os.path.join(target_dir_injection_path, os.path.basename(source_file_path))
    target_file_injection_path = os.path.normpath(target_file_injection_path)
    return target_file_injection_path, target_partition_path


# Direct Injection
def inject_file_into_partition(source_file_path, target_file_injection_path, aosp_path, partition_name, lunch_target, aosp_version):
    is_injected = False
    start_time = time.time()
    filename = os.path.basename(target_file_injection_path)
    if POST_INJECTOR_CONFIG["OVERWRITE_APP_PROCESS_32"]:
        # TODO : Remove this workaround in the future -> This does not work for all cases.
        source_file_path = handle_special_matching(source_file_path)

    # TODO : Remove this workaround in the future -> This does not work for all cases.
    #if aosp_version and int(aosp_version) == 12 and POST_INJECTOR_CONFIG["ALLOW_DIRECT_INJECTION_LIB_OVERWRITE"]:
    #    if "/lib/" in target_file_injection_path:
    #       target_file_injection_path = target_file_injection_path.replace("/lib/", "/lib64/hw/")
    #        logging.info(f"AOSP 12 lib adjustment for file: {filename} into {target_file_injection_path}")

    if filename in POST_INJECTOR_CONFIG["DIRECT_INJECTION_TARGET_PATH_OVERWRITE"]:
        target_file_injection_path = os.path.join(aosp_path,
                                                  "out/target/product/emulator64_arm64",
                                                  POST_INJECTOR_CONFIG["DIRECT_INJECTION_TARGET_PATH_OVERWRITE"][filename]
                                                  )
        logging.info(f"Direct Injection via specific target path overwrite "
                     f"for file: {filename} into {target_file_injection_path}")

    if (filename in POST_INJECTOR_CONFIG["APEX_BINARY_ISOLATED_NAMESPACE_LIST"] or source_file_path in
            POST_INJECTOR_CONFIG["APEX_BINARY_ISOLATED_NAMESPACE_LIST"]):
        logging.info(
            f"Direct Injection via APEX symlink file: {filename} with path {source_file_path} into {target_file_injection_path}")
        is_injected = inject_apex_symlink_file(filename=filename,
                                               source_file_path=source_file_path,
                                               original_file_path=target_file_injection_path,
                                               aosp_path=aosp_path,
                                               partition_name=partition_name,
                                               lunch_target=lunch_target,
                                               aosp_version=aosp_version)
        if not is_injected:
            logging.error(f"Error injecting APEX symlink file: {source_file_path} into {target_file_injection_path}")
            return target_file_injection_path
    elif os.path.exists(target_file_injection_path):
        if os.path.islink(target_file_injection_path):
            try:
                shutil.copy2(source_file_path, target_file_injection_path, follow_symlinks=False)
                is_injected = True
                logging.info(f"File link overwrite: {source_file_path} into {target_file_injection_path}")
            except Exception as e:
                logging.error(f"Error copying file link: {source_file_path} -> {target_file_injection_path} | {e}")
        else:
            # Fîle should not already exist, but if it does, we overwrite it, but it is not recommended.
            is_injected = False
            logging.error(f"File overwrite: {source_file_path} into {target_file_injection_path}. "
                          f"This overwrite is likely to be reverted by the AOSP build system. Thus, nothing is injected. "
                          f"Adjust your injection policy to handle this case.")
            # try:
            #     if os.path.isfile(source_file_path):
            #         inj_md5 = get_md5_from_file(source_file_path)
            #         org_md5 = get_md5_from_file(target_file_injection_path)
            #         shutil.copy2(source_file_path, target_file_injection_path, follow_symlinks=False)
            #         logging.error(f"File overwrite: {source_file_path}:{inj_md5} into {target_file_injection_path}:{org_md5}. This overwrite is likely to be reverted by the AOSP build system.")
            #         if not set_executable_permission(target_file_injection_path):
            #             raise PermissionError(f"Permission denied for overwrite {target_file_injection_path}")
            # except Exception as e:
            #     logging.error(f"Error copying file: {source_file_path} -> {target_file_injection_path} | {e}")
    else:
        logging.debug(f"Injecting file: {source_file_path} into {target_file_injection_path}\n")
        if not os.path.exists(source_file_path):
            logging.error(f"Injecting file: Source file does not exist anymore: {source_file_path}")
        else:
            try:
                os.makedirs(os.path.dirname(target_file_injection_path), exist_ok=True)
            except Exception as e:
                logging.error(f"Error creating directory: {target_file_injection_path} -> {e}")
            try:
                if os.path.isfile(source_file_path) and not os.path.islink(source_file_path):
                    shutil.copy2(source_file_path, target_file_injection_path, follow_symlinks=False)
                    is_injected = True
                elif os.path.islink(source_file_path):
                    command = f'sudo cp -a {source_file_path} {target_file_injection_path} '
                    result = subprocess.run(command, shell=True, capture_output=True, text=True)
                    if result.returncode != 0:
                        logging.error(
                            f"Inject File Error copying symlink: {source_file_path} with {target_file_injection_path} | {result.stderr}")
                    else:
                        is_injected = True
            except Exception as e:
                logging.error(f"Inject File Error copying file: {source_file_path} -> {target_file_injection_path} | {e}")

        if not set_executable_permission(target_file_injection_path):
            raise PermissionError(f"Permission denied for not existing file inject: {target_file_injection_path}")

    if ENABLE_INJECTION_PERFORMANCE_LOG:
        try:
            write_json_nd_output({
                'function': 'inject_file_into_partition',
                'source_file': source_file_path,
                'target_file': target_file_injection_path,
                'method': 'direct',
                'duration_seconds': time.time() - start_time,
                'timestamp': time.time(),
                'injected': bool(is_injected)
            }, PATH_MAPPING_EXECUTION_TIME_LOG)
        except Exception:
            logging.debug('Could not write inject_file_into_partition timing to log')

    return target_file_injection_path, is_injected


def handle_special_matching(source_file_injection_path):
    if source_file_injection_path.endswith("app_process32"):
        source_file_injection_path = source_file_injection_path.replace("app_process32", "app_process64")
        logging.info(f"Special matching app_process32 replace with app_process64: {source_file_injection_path}")
    return source_file_injection_path

def inject_apex_symlink_file(filename, source_file_path, original_file_path, aosp_path, partition_name, lunch_target, aosp_version):
    # Special case for isolated namespace binaries - Replacing Binary with symlink to apex binary
    target_path = f"/apex/com.android.fmd.{filename}.apex/bin/{filename}"
    logging.info(f"Add new dangling symlink script: {original_file_path} -> {target_path}")
    root_path = get_path_up_to_first_term(source_file_path, partition_name)
    logging.info(f"{source_file_path} - Root path: {root_path}")
    relative_source_path = source_file_path.replace(root_path, "")
    if aosp_version and float(aosp_version) in ["12", "12.1", "13"]:
        abs_source_path = os.path.join(aosp_path, "out/target/product/emulator64_arm64", relative_source_path)
    elif aosp_version and float(aosp_version) >= 14:
        abs_source_path = os.path.join(aosp_path, "out/target/product/emu64a", relative_source_path)
    else:
        abs_source_path = os.path.join(aosp_path, "out/target/product/emulator64_arm64", relative_source_path)

    inject_commands = [f"os.remove('{abs_source_path}')",f"subprocess.call(['ln', '-s', '{target_path}', '{abs_source_path}'])"]
    injection_marker = "####### FMD INJECTION MARKER #######"
    build_image_file_path = os.path.join(aosp_path, "build/make/tools/releasetools/build_image.py")
    enable_isolated_namespace = POST_INJECTOR_CONFIG["USE_ISOLATED_NAMESPACE"]
    is_injected = True
    if enable_isolated_namespace:
        if os.path.exists(build_image_file_path):
            logging.info(f"Injecting file from Goldfish for {target_path}: {build_image_file_path}")
        try:
            with open(build_image_file_path, "r") as f:
                lines = f.readlines()

            with open(build_image_file_path, "w") as f:
                for line in lines:
                    logging.info(f"Processing line: {line.strip()}")
                    if injection_marker in line:
                        logging.debug(f"Injection marker found in {build_image_file_path}, injecting commands.")
                        f.write(f"{injection_marker}\n")
                        for command in inject_commands:
                            logging.debug(f"Injecting command to goldfish: {command}")
                            f.write(f"    {command}\n")
                        is_injected = True
                        logging.info(f"Injected file as simlink: {source_file_path} -> {target_path}")
                    else:
                        logging.info(f"Write line to goldfish: {line.strip()}")
                        f.write(line)
        except Exception as e:
            logging.error(f"Error injecting file into Goldfish: {build_image_file_path} | {e}")
            is_injected = False
    return is_injected


def inject_file_into_obj(source_file_path, original_file_path, module_type, aosp_path, partition_name, lunch_target, aosp_version):
    """
    Injects a file into the AOSP source code directly without matching to existing files.
    """
    filename = os.path.basename(source_file_path)
    inj_md5 = get_md5_from_file(source_file_path)
    org_md5 = get_md5_from_file(original_file_path)

    matching_intermediate_file_list = find_intermediate_file(aosp_path, org_md5)

    logging.info(f"Overwriting Obj file: {source_file_path}:{inj_md5} into {original_file_path}:{org_md5}")
    file_name = os.path.basename(original_file_path)
    start_time = time.time()
    is_injected = False
    try:
        # if "/apex/" in original_file_path:
        #     #TODO Remove this check
        #     if module_type == "JAVA_LIBRARIES":
        #         new_file_path = "/system/framework/" + file_name
        #         logging.info(f"Injecting file from apex: {source_file_path} into {new_file_path}")
        #     elif module_type == "BINARY":
        #         new_file_path = "/bin/" + file_name
        #         logging.info(f"Injecting binary from apex: {source_file_path} into {new_file_path}")
        #     else:
        #         new_file_path = "/etc/" + file_name
        #         logging.info(f"Injecting /etc/ file from apex: {source_file_path} into {new_file_path}")
        #     copy_fast(source_file_path, new_file_path)
        #     is_injected = True
        # elif filename in POST_INJECTOR_CONFIG["APEX_BINARY_ISOLATED_NAMESPACE_LIST"] or source_file_path in POST_INJECTOR_CONFIG["APEX_BINARY_ISOLATED_NAMESPACE_LIST"]:
        #     logging.info(f"Indirect Injection via APEX symlink file: {filename} with path {source_file_path} into {original_file_path}")
        #     is_injected = inject_apex_symlink_file(filename, source_file_path, original_file_path, aosp_path, partition_name, lunch_target, aosp_version)
        #else:
        copy_fast(source_file_path, original_file_path)
        is_injected = True
        set_executable_permission(original_file_path)
        for file_path in matching_intermediate_file_list:
            try:
                # Skip file, if path contains specific keyword
                if any(keyword in file_path for keyword in POST_INJECTOR_CONFIG["SKIPPED_INTERMEDIATE_FILE_OVERWRITE_KEYWORD_LIST"]):
                    logging.info(f"Skipping {file_path} because it was a keyword in SKIPPED_INTERMEDIATE_FILE_OVERWRITE_KEYWORD_LIST that matched the file_path")
                    continue
                schedule_copy(source_file_path, file_path)
                logging.debug(f"Scheduled indirect injection of .intermediate file: {file_path} with {source_file_path}")
            except Exception as e:
                logging.error(f"Error scheduling copy for intermediate file: {file_path} with {source_file_path} | {e}")
                #os.chmod(original_file_path, os.stat(original_file_path).st_mode | stat.S_IEXEC)
    except Exception as e:
        logging.error(f"Error injecting file: {source_file_path} into {original_file_path} | {e}")
        traceback.print_exc()
        is_injected = False
    finally:
        if ENABLE_INJECTION_PERFORMANCE_LOG:
            try:
                write_json_nd_output({
                    'function': 'inject_file_into_obj',
                    'source_file': source_file_path,
                    'target_file': original_file_path,
                    'method': 'indirect_obj',
                    'duration_seconds': time.time() - start_time,
                    'timestamp': time.time(),
                    'injected': bool(is_injected)
                }, PATH_MAPPING_EXECUTION_TIME_LOG)
            except Exception:
                logging.debug('Could not write inject_file_into_obj timing to log')

    return is_injected

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
    parser.add_argument("-m", "--pre_injector_config",
                        type=str,
                        default="./device_configs/development/pre_injector_config_v1.json", )
    parser.add_argument("-i", "--post_injector_config",
                        type=str,
                        default="./device_configs/development/post_injector_config_v1.json", )
    parser.add_argument("-e", "--aosp-version", type=str, default="12",)
    parser.add_argument("-u", "--fmd-username", type=str, default=None, required=True,
                        help="Username for the authentication to the fmd service.")
    parser.add_argument("-f", "--firmware-id", type=str, default=None, required=True,
                        help="ID of the firmware used in the pre-injector.")
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
    fmd_password = os.getenv('FMD_PASSWORD')
    if not fmd_password or not args.fmd_username:
        raise RuntimeError(f"Please enter your FMD username/password ({args.fmd_username}): ")

    aosp_version = args.aosp_version
    if aosp_version not in ["12", "12.1", "13", "14"]:
        raise RuntimeError("Please provide a valid AOSP version argument (12, 13, 14).")

    if aosp_version in ["12", "12.1"]:
        lunch_target = "sdk_phone64_arm64-userdebug"
    elif aosp_version == "13":
        lunch_target = "sdk_phone64_arm64-userdebug"
    elif aosp_version == "14":
        lunch_target = "sdk_phone64_arm64-ap2a-userdebug"
    elif aosp_version in ["15", "16"]:
        lunch_target = "sdk_phone64_arm64-trunk_staging-userdebug"
    else:
        raise RuntimeError("Please provide a valid lunch target argument.")

    logging.info(f"Source folder path: {source_folder_path}")
    logging.info(f"Target out path: {target_out_path}")
    logging.info(f"AOSP root path: {aosp_path}")
    logging.info(f"Lunch target: {lunch_target}")
    logging.info(f"Pre Injector Config: {args.pre_injector_config}")
    logging.info(f"Post Injector Config: {args.post_injector_config}")
    pre_injector_config, post_injector_config = load_configs(args.pre_injector_config, args.post_injector_config)

    fmd_username = args.fmd_username
    fmd_url = post_injector_config["FMD_URL"]
    graphql_url = post_injector_config["GRAPHQL_API_URL"]
    csrf_cookie = get_csrf_token(fmd_url)
    fmd_cookies = authenticate_fmd(graphql_url, fmd_username, fmd_password, csrf_cookie)
    if not args.firmware_id:
        raise RuntimeError("Please provide a firmware ID argument.")
    firmware_id = args.firmware_id

    start_post_build_injector(aosp_path=aosp_path,
                              source_folder_path=source_folder_path,
                              target_out_path=target_out_path,
                              lunch_target=lunch_target,
                              pre_injector_config_path=args.pre_injector_config,
                              post_injector_config_path=args.post_injector_config,
                              firmware_id=firmware_id,
                              cookies=fmd_cookies,
                              aosp_version=aosp_version)

    logging.info("=======================AOSP POST BUILD INJECTOR EXIT=======================")


if __name__ == "__main__":
    main()
