import logging
import os
import re
import zipfile
from ConfigManager import ConfigManager
from config import VENDOR_NAMES


def extract_zip(file_path, destination):
    print(f"Extracting {file_path} to {destination}")
    with zipfile.ZipFile(file_path, 'r') as zip_ref:
        zip_ref.extractall(destination)


def extract_vendor_name(filename, directory=None):
    """
    Extracts the vendor name from a filename. If no vendor name is found, attempts to infer it.

    :param filename: str - The name of the file.
    :param directory: str - Optional directory path to infer vendor name.
    :return: str - The extracted or inferred vendor name.
    """
    # Regex to match vendor names in filenames
    vendor_pattern = re.compile(r"com\.([a-z0-9]+)\.android\..*", re.IGNORECASE)
    match = vendor_pattern.match(filename)

    if match and match is not None or match == ".":
        return match.group(1)  # Return the vendor name from the filename

    # Fallback: Infer vendor name from directory structure
    if directory:
        for part in directory.split(os.sep):
            if part.lower() in VENDOR_NAMES:
                if part is not None and part != ".":
                    logging.info(f"Vendor name inferred from directory: {part}")
                else:
                    return part.lower()

    # Fallback: Use a default vendor name
    return ""

def extract_vendor_name_from_filename(filename):
    """
    Extracts the vendor name from a filename. If no vendor name is found, attempts to infer it.

    :param filename: str - The name of the file.
    :return: str - The extracted or inferred vendor name.
    """
    # Regex to match vendor names in filenames
    vendor_pattern = re.compile(r"com\.([a-z0-9]+)\.android\..*", re.IGNORECASE)
    match = vendor_pattern.match(filename)

    if match and match is not None or match == ".":
        return match.group(1)  # Return the vendor name from the filename

    # Fallback: Use a default vendor name
    return ""


def get_vendor_words(file_path=None, filename=None):
    """
    Get the vendor name from the file path.

    :param file_path: str - The path of the file.
    :return: str - The vendor name.
    """

    vendor_name_list = VENDOR_NAMES
    if file_path:
        directory_path = os.path.dirname(file_path)
        vendor_name = extract_vendor_name(file_path, directory_path)
    elif filename:
        vendor_name = extract_vendor_name_from_filename(filename)
    else:
        raise Exception("No file path provided")

    if vendor_name.startswith("."):
        vendor_name_list.append(f"{vendor_name}")
    else:
        vendor_name_list.append(f".{vendor_name}")

    vendor_name_list = list(set(vendor_name_list))


    words_to_replace = []
    for name in vendor_name_list:
        name = str(name)
        if name and name is not "" and name is not "." and name is not "..":
            name.replace("..", ".")
            words_to_replace.append(f".{name.lower()}")
            words_to_replace.append(f".{name.capitalize()}")
    logging.info(f"Vendor words to replace: {'|'.join(words_to_replace)}")
    return words_to_replace


def remove_vendor_name_from_path(file_path):
    logging.info(f"Filepath before removing vendor specific words: {file_path}")
    words_to_replace = get_vendor_words(file_path)
    file_path_vendor_replaced = file_path
    base_path = os.path.dirname(file_path)
    filename = os.path.basename(file_path)
    for word in words_to_replace:
        filename = filename.replace(word, "")
    file_path_vendor_replaced = os.path.join(base_path, filename)
    logging.info(f"Filename after path cleared from vendor words: {file_path_vendor_replaced}")
    return file_path_vendor_replaced


def remove_vendor_name_from_filename(filename):
    logging.info(f"Filename before removing vendor specific words {filename}")
    words_to_replace = get_vendor_words(filename=filename)
    filename_no_ext = filename
    for word in words_to_replace:
        filename_no_ext = filename_no_ext.replace(word, "")
    logging.info(f"Removed vendor name: {filename_no_ext}")
    return filename_no_ext


def load_configs(pre_injector_config_path, post_injector_config_path):
    # Usage
    ConfigManager.load_config("PRE_INJECTOR_CONFIG", pre_injector_config_path)
    ConfigManager.load_config("POST_INJECTOR_CONFIG", post_injector_config_path)

    pre_injector_config = ConfigManager.get_config("PRE_INJECTOR_CONFIG")
    post_injector_config = ConfigManager.get_config("POST_INJECTOR_CONFIG")
    logging.info(f"Loaded pre-injector config from {pre_injector_config_path}. Keys: {list(pre_injector_config.keys())}")
    logging.info(f"Loaded post injector config from {post_injector_config_path}. Keys: {list(post_injector_config.keys())}")
    if not pre_injector_config or not post_injector_config:
        logging.error("Pre-injector or post-injector config is empty. Please check the config files.")
        raise ValueError("Configuration files are empty or not loaded correctly.")
    return pre_injector_config, post_injector_config


