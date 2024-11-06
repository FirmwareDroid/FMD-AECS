import os
from config_post_injector import *


def is_file_inject_allowed(file_name):
    """
    Determines if a file is allowed to be injected based on various criteria.

    :param file_name: str - The name of the file.

    :return: bool - True if the file is allowed to be injected, False otherwise.
    """
    if file_name in SKIPPED_BINARY_LIST:
        return False
    return True


def is_file_extension_allowed(file_extension):
    if file_extension in SKIPPED_FILE_EXTENSION_LIST:
        return False
    return True


def is_file_path_allowed(file_path):
    """
    Determines if a file path is allowed based on the keyword list.

    :param file_path: str - The path to the file.

    :return: bool - True if the file path is allowed, False otherwise.

    """
    if any(keyword in file_path for keyword in SKIPPED_KEYWORD_LIST):
        return False

    return True

def is_apex_file_path_allowed(file_path):
    """
    Determines if a file path is allowed based on the keyword list.
    :param file_path: str - The path to the file.
    :return: bool - True if the file path is allowed, False otherwise.
    """
    if any(keyword in file_path for keyword in SKIPPED_APEX_KEYWORD_LIST):
        return False
    return True


def get_module_type(source_file_path, is_apex=False):
    """
    Determines the module type of the source file.
    """
    source_file_path = source_file_path.strip()
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


    tmp_module_type = module_type
    if (not is_file_path_allowed(source_file_path)
            or not is_file_extension_allowed(file_extension)
            or not is_file_inject_allowed(file_name)):
        module_type = "SKIPPED"

    if is_apex and not is_apex_file_path_allowed(source_file_path):
        module_type = "SKIPPED"

    if file_name in ALLOW_FILE_INJECT_ALWAYS or any(keyword in source_file_path for keyword in ALLOW_FILE_INJECT_ALWAYS_KEYWORD_LIST):
        module_type = tmp_module_type

    if is_apex:
        if file_name in ALLOW_APEX_FILE_INJECT or any(keyword in source_file_path for keyword in ALLOW_APEX_FILE_INJECT_ALWAYS_KEYWORD_LIST):
            module_type = tmp_module_type

    return module_type
