import logging
import os
from common import is_elf_binary, check_binary_architecture

POST_INJECTOR_CONFIG = None


def _find_matching_keyword(s, keywords):
    """Return the first keyword from keywords that appears in s, or None."""
    if not keywords:
        return None
    for kw in keywords:
        try:
            if kw and kw in s:
                return kw
        except Exception:
            continue
    return None


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

    #elif file_extension in [".xml"]:
    #    module_type = "STATIC_CONFIG"

    if file_extension in ["", None] and (is_elf_binary(source_file_path) or "bin" in parent_dir or "xbin" in parent_dir):
        module_type = "EXECUTABLES"
    elif file_extension in [".jar"]:
        module_type = "JAVA_LIBRARIES"
    elif file_extension in [".so"]:
        module_type = "SHARED_LIBRARIES"
    elif file_extension in [".apk"]:
        module_type = "APPS"
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
        kw = _find_matching_keyword(file_name, POST_INJECTOR_CONFIG["SKIPPED_APP_KEYWORDLIST"])
        logging.info(f"Skipping {source_file_path}: matched SKIPPED_APP_KEYWORDLIST keyword: {kw}")
        module_type = "SKIPPED"
    if module_type == "APPS" and (file_name_no_ext in POST_INJECTOR_CONFIG["SKIPPED_APP_LIST"]
                                  or file_name in POST_INJECTOR_CONFIG["SKIPPED_APP_LIST"]):
        logging.info(f"Skipping {source_file_path}: filename found in SKIPPED_APP_LIST")
        module_type = "SKIPPED"
    if module_type == "APPS" and any(keyword in file_name for keyword in POST_INJECTOR_CONFIG["ALLOWED_APP_INJECTION_KEYWORD"]):
        module_type = "APPS"

    if module_type in ["EXECUTABLES", "ETC"] and POST_INJECTOR_CONFIG["DISABLE_BINARY_INJECTION"]:
        module_type = "SKIPPED"

    if (not is_file_path_allowed(source_file_path)
            or (file_extension not in ["", None] and not is_file_extension_allowed(file_extension))
            or not is_file_inject_allowed(file_name)):
        # Determine which sub-condition triggered the skip and log details
        if not is_file_path_allowed(source_file_path):
            kw = _find_matching_keyword(source_file_path, POST_INJECTOR_CONFIG.get("SKIPPED_KEYWORD_LIST", []))
            logging.info(f"Skipping {source_file_path}: path contains skipped keyword: {kw}")
        if file_extension not in ["", None] and not is_file_extension_allowed(file_extension):
            # Distinguish between allow-list and general-skip-list
            allow_only = POST_INJECTOR_CONFIG.get("ALLOW_ONLY_EXTENSION_LIST", [])
            skipped_general = POST_INJECTOR_CONFIG.get("SKIPPED_FILE_EXTENSION_LIST_GENERAL", [])
            if allow_only and file_extension not in allow_only:
                logging.info(f"Skipping {source_file_path}: extension '{file_extension}' not in ALLOW_ONLY_EXTENSION_LIST")
            elif file_extension in skipped_general:
                logging.info(f"Skipping {source_file_path}: extension '{file_extension}' in SKIPPED_FILE_EXTENSION_LIST_GENERAL")
            else:
                logging.info(f"Skipping {source_file_path}: extension '{file_extension}' disallowed")
        if not is_file_inject_allowed(file_name):
            if file_name in POST_INJECTOR_CONFIG.get("SKIPPED_BINARY_LIST", []):
                logging.info(f"Skipping {source_file_path}: filename in SKIPPED_BINARY_LIST")
            else:
                # which ending matched?
                for ending in POST_INJECTOR_CONFIG.get("SKIPPED_FILE_ENDING_LIST", []):
                    if file_name.endswith(ending):
                        logging.info(f"Skipping {source_file_path}: filename endswith skipped ending '{ending}'")
                        break
        module_type = "SKIPPED"

    if module_type in ["SHARED_LIBRARIES", "ETC", "APPS"]:
        if pre_injector_package_list:
            for package_name in pre_injector_package_list:
                stripped_package_name = package_name.replace("FMD_APEX", "").replace("fmd", "").strip()
                if file_name == stripped_package_name or file_name_no_ext == stripped_package_name:
                    logging.info(f"Skipping {source_file_path} as it was already injected via pre-injector.")
                    module_type = "SKIPPED"
                    break

    if module_type in ["SHARED_LIBRARIES"] and file_extension in [".so"]:
        if POST_INJECTOR_CONFIG["DISABLE_SHARED_LIBRARY_INJECTION"]:
            module_type = "SKIPPED"

    if POST_INJECTOR_CONFIG["ENABLE_SHARED_LIBRARIES_INJECTION_IF_NOT_EXISTS"] and file_extension in [".so"]:
        if (file_name in POST_INJECTOR_CONFIG["SKIPPED_SHARED_LIBRARIES_EVEN_IF_NOT_EXISTS_LIST"]
                or any(keyword in file_name for keyword in POST_INJECTOR_CONFIG["SKIPPED_KEYWORD_SHARED_LIBRARIES_EVEN_IF_NOT_EXISTS_LIST"])):
            logging.info(f"File {source_file_path} is skipped from injection even if it does not exist in the system, as"
                         f" it is listed in SKIPPED_SHARED_LIBRARIES_EVEN_IF_NOT_EXISTS_LIST or contains keywords in"
                         f" SKIPPED_KEYWORD_SHARED_LIBRARIES_EVEN_IF_NOT_EXISTS_LIST. Module type set to SKIPPED.")
            module_type = "SKIPPED"
        else:
            logging.info(f"File {source_file_path} does not exist in the system. Enabling injection as "
                         f"ENABLE_SHARED_LIBRARIES_INJECTION_IF_NOT_EXISTS is set. Module type remains {tmp_module_type}.")
            module_type = tmp_module_type

    if is_apex and any(keyword in file_name for keyword in POST_INJECTOR_CONFIG["SKIPPED_APEX_KEYWORD_LIST"]):
        keyword_match = next(keyword for keyword in POST_INJECTOR_CONFIG["SKIPPED_APEX_KEYWORD_LIST"] if keyword in file_name)
        logging.info(f"Skipping {source_file_path} as it is an APEX file and contains a keyword from SKIPPED_APEX_KEYWORD_LIST: {keyword_match}")
        module_type = "SKIPPED"

    if POST_INJECTOR_CONFIG["ENABLE_ALLOW_APEX_INJECT_ALWAYS_KEYWORD_NOT_IN_LIST"]:
        if is_apex and all(keyword not in file_name for keyword in
                           POST_INJECTOR_CONFIG["ALLOW_APEX_INJECT_ALWAYS_KEYWORD_NOT_IN_LIST"]):
            module_type = "ETC"

    if is_apex and any(keyword in file_name
                       for keyword in POST_INJECTOR_CONFIG["ALLOW_APEX_INJECT_ALWAYS_KEYWORD_LIST"]):
        module_type = "ETC"

    if (module_type == "APPS" or file_extension in [".apk"]) and POST_INJECTOR_CONFIG["DISALLOW_APP_INJECTION"]:
        logging.info(f"Post-Build App injection is disallowed by configuration: {source_file_path}")
        module_type = "SKIPPED"

    if module_type == "JAVA_LIBRARIES" and POST_INJECTOR_CONFIG["DISABLE_JAVA_LIBRARIES_INJECTION"]:
        logging.error(f"Java library injection is disallowed by configuration: {source_file_path}")
        module_type = "SKIPPED"
    elif module_type == "JAVA_LIBRARIES" and POST_INJECTOR_CONFIG["ALLOW_ALL_JAVA_LIBRARIES_INJECTION"]:
        module_type = tmp_module_type

    if module_type == "MISC" and (POST_INJECTOR_CONFIG["DISABLE_MISC_INJECTION"]
                            or any(keyword in file_name for keyword in POST_INJECTOR_CONFIG["SKIPPED_MISC_KEYWORD_LIST"])
                            or file_extension in POST_INJECTOR_CONFIG["SKIPPED_MISC_EXTENSION_LIST"]
    ):
        if POST_INJECTOR_CONFIG.get("DISABLE_MISC_INJECTION"):
            logging.info(f"Skipping {source_file_path}: DISABLE_MISC_INJECTION is enabled")

        if file_name in POST_INJECTOR_CONFIG["ALLOW_MISC_INJECT_ALWAYS"]:
            logging.info(f"File {source_file_path}|{tmp_module_type} is allowed to be injected regardless of its type. ALLOW_MISC_INJECT_ALWAYS")
            module_type = tmp_module_type
        elif any(keyword in source_file_path for keyword in POST_INJECTOR_CONFIG["ALLOW_MISC_INJECT_ALWAYS_KEYWORD_LIST"]):
            kw = _find_matching_keyword(file_name, POST_INJECTOR_CONFIG.get("ALLOW_MISC_INJECT_ALWAYS_KEYWORD_LIST", []))
            logging.info(f"ALLOW_MISC_INJECT_ALWAYS_KEYWORD_LIST matching keyword : {kw}")
            module_type = tmp_module_type
        else:
            kw = _find_matching_keyword(file_name, POST_INJECTOR_CONFIG.get("SKIPPED_MISC_KEYWORD_LIST", []))
            if kw:
                logging.info(f"Skipping {source_file_path}: matched SKIPPED_MISC_KEYWORD_LIST keyword: {kw}")
            if file_extension in POST_INJECTOR_CONFIG.get("SKIPPED_MISC_EXTENSION_LIST", []):
                logging.info(f"Skipping {source_file_path}: extension '{file_extension}' in SKIPPED_MISC_EXTENSION_LIST")
            module_type = "SKIPPED"

    if module_type == "APPS" and (file_name in POST_INJECTOR_CONFIG["ALLOW_APP_INJECT_ALWAYS"] or any(keyword in file_name for keyword in POST_INJECTOR_CONFIG["ALLOW_APP_INJECT_ALWAYS_KEYWORD_LIST"])):
        logging.info(
            f"File {source_file_path}|{tmp_module_type} is allowed to be injected regardless of its type. ALLOW_FILE_INJECT_ALWAYS / ALLOW_FILE_INJECT_ALWAYS_KEYWORD_LIST")
        module_type = tmp_module_type

    # Override the module type if the file name or path contains specific keywords
    if (file_name in POST_INJECTOR_CONFIG["ALLOW_FILE_INJECT_ALWAYS"]
            or any(keyword in source_file_path for keyword in POST_INJECTOR_CONFIG["ALLOW_FILE_INJECT_ALWAYS_KEYWORD_LIST"])):
        logging.info(f"File {source_file_path}|{tmp_module_type} is allowed to be injected regardless of its type. ALLOW_FILE_INJECT_ALWAYS / ALLOW_FILE_INJECT_ALWAYS_KEYWORD_LIST")
        module_type = tmp_module_type

    if file_name in POST_INJECTOR_CONFIG["NEVERALLOW_FILE_INJECT"]:
        logging.info(f"File {source_file_path} is in NEVERALLOW_FILE_INJECT list, marking it as SKIPPED regardless of other settings.")
        module_type = "SKIPPED"
    elif any(keyword in source_file_path for keyword in POST_INJECTOR_CONFIG["NEVERALLOW_FILE_INJECT_KEYWORD_LIST"]):
        logging.info(f"File {source_file_path} contains a keyword from NEVERALLOW_FILE_INJECT list, marking it as SKIPPED regardless of other settings.")
        module_type = "SKIPPED"

    if module_type in ["EXECUTABLE"]:
        if not check_binary_architecture(source_file_path) == "64-bit":
            logging.info(f"Skipping incompatible Binary (likely 32-bit Architecture): {source_file_path}")
            module_type = "SKIPPED"

    if "_apex" in source_file_path:
        module_type = "SKIPPED"
        logging.warning(f"FMD extraction leftover file {source_file_path} contains '_apex' in its path, marking it as SKIPPED to avoid potential issues with APEX files.")

    logging.debug(f"File Extension: {file_extension} for {source_file_path} is module type {module_type}")

    return module_type, tmp_module_type
