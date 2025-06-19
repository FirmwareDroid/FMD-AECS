import logging
import os

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


def get_module_type(source_file_path, pre_injector_package_list=None, post_injector_config=None):
    """
    Determines the module type of the source file.
    """
    global POST_INJECTOR_CONFIG
    POST_INJECTOR_CONFIG = post_injector_config
    source_file_path = source_file_path.strip()
    file_extension = os.path.splitext(source_file_path)[1]
    file_name = os.path.basename(source_file_path)
    file_name_no_ext = os.path.splitext(file_name)[0]

    is_apex = file_extension in [".apex", ".capex"]

    if file_extension in ["", None] and "/bin/" in source_file_path:
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
    else:
        module_type = "MISC"


    if module_type in ["EXECUTABLES", "ETC"] and POST_INJECTOR_CONFIG["DISABLE_BINARY_INJECTION"]:
        module_type = "SKIPPED"

    tmp_module_type = module_type
    if (not is_file_path_allowed(source_file_path)
            or (file_extension not in ["", None] and not is_file_extension_allowed(file_extension))
            or not is_file_inject_allowed(file_name)):
        module_type = "SKIPPED"

    if pre_injector_package_list and file_name_no_ext in pre_injector_package_list:
        module_type = "SKIPPED"

    if is_apex and any(keyword in file_name for keyword in POST_INJECTOR_CONFIG["SKIPPED_APEX_KEYWORD_LIST"]):
        module_type = "SKIPPED"

    # Override the module type if the file name or path contains specific keywords
    if (file_name in POST_INJECTOR_CONFIG["ALLOW_FILE_INJECT_ALWAYS"]
            or any(keyword in source_file_path for keyword in POST_INJECTOR_CONFIG["ALLOW_FILE_INJECT_ALWAYS_KEYWORD_LIST"])):
        module_type = tmp_module_type

    logging.info(f"File Extension: {file_extension} for {source_file_path} is module type {module_type}")

    return module_type
