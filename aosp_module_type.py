import logging
import os
from common import is_elf_binary
POST_INJECTOR_CONFIG = None


def is_file_inject_allowed(file_name):
    """
    Determines if a file is allowed to be injected based on various criteria.

    :param file_name: str - The name of the file.

    :return: bool - True if the file is allowed to be injected, False otherwise.
    """
    if file_name in POST_INJECTOR_CONFIG["SKIPPED_BINARY_LIST"]:
        return False
    for file_ending in POST_INJECTOR_CONFIG["SKIPPED_FILE_ENDING_LIST"]:
        if file_name.endswith(file_ending):
            return False
    return True


def is_file_extension_allowed(file_extension):
    if (len(POST_INJECTOR_CONFIG["ALLOW_ONLY_EXTENSION_LIST"]) > 0
            and file_extension not in POST_INJECTOR_CONFIG["ALLOW_ONLY_EXTENSION_LIST"]):
        return False
    if file_extension in POST_INJECTOR_CONFIG["SKIPPED_FILE_EXTENSION_LIST_GENERAL"]:
        return False
    return True


def is_file_path_allowed(file_path):
    """
    Determines if a file path is allowed based on the keyword list.

    :param file_path: str - The path to the file.

    :return: bool - True if the file path is allowed, False otherwise.

    """
    if any(keyword in file_path for keyword in POST_INJECTOR_CONFIG["SKIPPED_KEYWORD_LIST"]):
        return False

    return True

def is_apex_file_path_allowed(file_path):
    """
    Determines if a file path is allowed based on the keyword list.
    :param file_path: str - The path to the file.
    :return: bool - True if the file path is allowed, False otherwise.
    """
    if any(keyword in file_path for keyword in POST_INJECTOR_CONFIG["SKIPPED_APEX_KEYWORD_LIST"]):
        return False
    return True


def is_app_already_injected(file_name, pre_injector_package_list):
    if file_name in pre_injector_package_list:
        return True
    return False


def get_module_type(source_file_path, pre_injector_package_list=None, post_injector_config=None):
    """
    Determines the module type of the source file.
    """
    global POST_INJECTOR_CONFIG
    POST_INJECTOR_CONFIG = post_injector_config
    parent_dir = os.path.dirname(source_file_path)
    source_file_path = source_file_path.strip()
    file_extension = os.path.splitext(source_file_path)[1]
    file_name = os.path.basename(source_file_path)
    file_name_no_ext = os.path.splitext(file_name)[0]

    is_apex = file_extension in [".apex", ".capex"]

    if file_extension in ["", None] and (is_elf_binary(source_file_path) or "bin" in parent_dir or "xbin" in parent_dir):
        module_type = "EXECUTABLES"
    elif file_extension in [".jar"]:
        module_type = "JAVA_LIBRARIES"
    elif file_extension in [".so"]:
        module_type = "SHARED_LIBRARIES"
    elif file_extension in [".apk"]:
        module_type = "APPS"
    elif file_extension in [".xml"]:
        module_type = "STATIC_CONFIG"
    elif "/etc/" in source_file_path:
        module_type = "ETC"
    elif file_extension in [".apex", ".capex"]:
        module_type = "ETC"
        if "_compressed" in file_name:
            file_name = file_name.replace("_compressed", "")
        elif "_trimmed" in file_name:
            file_name = file_name.replace("_trimmed", "")
    else:
        module_type = "MISC"

    tmp_module_type = module_type

    if module_type == "APPS" and any(keyword in file_name for keyword in POST_INJECTOR_CONFIG["SKIPPED_APP_KEYWORDLIST"]):
        module_type = "SKIPPED"
    if module_type == "APPS" and (file_name_no_ext in POST_INJECTOR_CONFIG["SKIPPED_APP_LIST"]
                                  or file_name in POST_INJECTOR_CONFIG["SKIPPED_APP_LIST"]):
        module_type = "SKIPPED"
    if module_type == "APPS" and any(keyword in file_name for keyword in POST_INJECTOR_CONFIG["ALLOWED_APP_INJECTION_KEYWORD"]):
        module_type = "APPS"

    if module_type in ["EXECUTABLES", "ETC"] and POST_INJECTOR_CONFIG["DISABLE_BINARY_INJECTION"]:
        module_type = "SKIPPED"

    if (not is_file_path_allowed(source_file_path)
            or (file_extension not in ["", None] and not is_file_extension_allowed(file_extension))
            or not is_file_inject_allowed(file_name)):
        module_type = "SKIPPED"

    if module_type in ["SHARED_LIBRARIES", "ETC", "APPS"]:
        for package_name in pre_injector_package_list:
            stripped_package_name = package_name.replace("FMD_APEX", "").replace("fmd", "").strip()
            if file_name == stripped_package_name or file_name_no_ext == stripped_package_name:
                logging.info(f"Skipping {source_file_path} as it was already injected via pre-injector.")
                module_type = "SKIPPED"
                break

    if POST_INJECTOR_CONFIG["ENABLE_SHARED_LIBRARIES_INJECTION_IF_NOT_EXISTS"] and file_extension in [".so"]:
        logging.info(f"File {source_file_path} does not exist in the system. Enabling injection as "
                     f"ENABLE_SHARED_LIBRARIES_INJECTION_IF_NOT_EXISTS is set. Module type remains {tmp_module_type}.")
        if file_name in POST_INJECTOR_CONFIG["SKIPPED_SHARED_LIBRARIES_EVEN_IF_NOT_EXISTS_LIST"] or any(keyword in file_name for keyword in POST_INJECTOR_CONFIG["SKIPPED_KEYWORD_SHARED_LIBRARIES_EVEN_IF_NOT_EXISTS_LIST"]):
            module_type = "SKIPPED"
        else:
            module_type = tmp_module_type

    if is_apex and any(keyword in file_name for keyword in POST_INJECTOR_CONFIG["SKIPPED_APEX_KEYWORD_LIST"]):
        module_type = "SKIPPED"

    if POST_INJECTOR_CONFIG["ENABLE_ALLOW_APEX_INJECT_ALWAYS_KEYWORD_NOT_IN_LIST"]:
        if is_apex and all(keyword not in file_name for keyword in
                           POST_INJECTOR_CONFIG["ALLOW_APEX_INJECT_ALWAYS_KEYWORD_NOT_IN_LIST"]):
            module_type = "ETC"

    if is_apex and any(keyword in file_name
                       for keyword in POST_INJECTOR_CONFIG["ALLOW_APEX_INJECT_ALWAYS_KEYWORD_LIST"]):
        module_type = "ETC"

    if module_type == "APPS" and POST_INJECTOR_CONFIG["DISALLOW_APP_INJECTION"]:
        logging.error(f"App injection is disallowed by configuration: {source_file_path}")
        module_type = "SKIPPED"

    if module_type == "JAVA_LIBRARIES" and POST_INJECTOR_CONFIG["DISABLE_JAVA_LIBRARIES_INJECTION"]:
        logging.error(f"Java library injection is disallowed by configuration: {source_file_path}")
        module_type = "SKIPPED"
    elif module_type == "JAVA_LIBRARIES" and POST_INJECTOR_CONFIG["ALLOW_ALL_JAVA_LIBRARIES_INJECTION"]:
        module_type = tmp_module_type

    if module_type == "MISC" and POST_INJECTOR_CONFIG["DISABLE_MISC_INJECTION"]:
        module_type = "SKIPPED"

    if file_extension in [".apk"] and (file_name in POST_INJECTOR_CONFIG["ALLOW_APP_INJECT_ALWAYS"] or any(keyword in file_name for keyword in POST_INJECTOR_CONFIG["ALLOW_APP_INJECT_ALWAYS_KEYWORD_LIST"])):
        logging.info(
            f"File {source_file_path}|{tmp_module_type} is allowed to be injected regardless of its type. ALLOW_FILE_INJECT_ALWAYS / ALLOW_FILE_INJECT_ALWAYS_KEYWORD_LIST")
        module_type = tmp_module_type

    # Override the module type if the file name or path contains specific keywords
    if (file_name in POST_INJECTOR_CONFIG["ALLOW_FILE_INJECT_ALWAYS"]
            or any(keyword in source_file_path for keyword in POST_INJECTOR_CONFIG["ALLOW_FILE_INJECT_ALWAYS_KEYWORD_LIST"])):
        logging.info(f"File {source_file_path}|{tmp_module_type} is allowed to be injected regardless of its type. ALLOW_FILE_INJECT_ALWAYS / ALLOW_FILE_INJECT_ALWAYS_KEYWORD_LIST")
        module_type = tmp_module_type

    logging.debug(f"File Extension: {file_extension} for {source_file_path} is module type {module_type}")

    return module_type, tmp_module_type
