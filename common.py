import json
import logging
import os
import re
import zipfile
from ConfigManager import ConfigManager
from config import VENDOR_NAMES
import hashlib
import subprocess
import time

from fmd_backend_requests import upload_image_as_raw


def extract_zip(file_path, destination):
    logging.info("Extracting %s to %s", file_path, destination)
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
        if name and name != "" and name != "." and name != "..":
            name.replace("..", ".")
            words_to_replace.append(f".{name.lower()}")
            words_to_replace.append(f".{name.capitalize()}")
    logging.debug(f"Vendor words to replace: {'|'.join(words_to_replace)}")
    return words_to_replace


def remove_vendor_name_from_path(file_path):
    logging.debug(f"Filepath before removing vendor specific words: {file_path}")
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
    logging.debug(f"Loaded pre-injector config from {pre_injector_config_path}. Keys: {list(pre_injector_config.keys())}")
    logging.debug(f"Loaded post injector config from {post_injector_config_path}. Keys: {list(post_injector_config.keys())}")
    if not pre_injector_config or not post_injector_config:
        logging.error("Pre-injector or post-injector config is empty. Please check the config files.")
        raise ValueError("Configuration files are empty or not loaded correctly.")
    return pre_injector_config, post_injector_config

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
        return False


def check_shared_object_architecture(file_path):
    """
    Check if a shared object (.so) file is compiled for 32-bit or 64-bit.

    :param file_path: str - Path to the .so file.
    :return: str - '32-bit', '64-bit', or 'Unknown architecture'.
    """
    try:
        with open(file_path, 'rb') as f:
            # Read the first 5 bytes of the file
            header = f.read(5)
            if len(header) < 5:
                return 'Unknown architecture'

            # Check the ELF magic number and class
            if header[:4] == b'\x7fELF':
                ei_class = header[4]
                if ei_class == 1:
                    return '32-bit'
                elif ei_class == 2:
                    return '64-bit'
            return 'Unknown architecture'
    except Exception as e:
        return f"Error determining architecture: {str(e)}"


def get_path_up_to_term(path, term):
    """
    Returns the subpath up to and including the last occurrence of `term` in the path.
    """
    parts = path.split(os.sep)
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == term:
            return os.sep.join(parts[:i+1]) + os.sep
    return None  # term not found

def get_path_up_to_first_term(path, term):
    """
    Returns the subpath up to and including the first occurrence of `term` in the path.
    """
    parts = path.split(os.sep)
    for i, part in enumerate(parts):
        if part == term:
            return os.sep.join(parts[:i+1]) + os.sep
    return None  # term not found


def get_md5_from_file(file_path):
    try:
        # Python 3.11
        with open(file_path, "rb") as f:
            digest = hashlib.file_digest(f, "md5")
            digest.hexdigest()
    except Exception as e:
        try:
            digest = hashlib.md5()
            # A 128KB buffer is usually the sweet spot for disk I/O
            buffer = bytearray(128 * 1024)
            mv = memoryview(buffer)
            with open(file_path, 'rb', buffering=0) as f:
                # We use a zero-buffer file object to manage our own buffering
                for n in iter(lambda: f.readinto(mv), 0):
                    digest.update(mv[:n])
        except Exception as e1:
            # Python 3.10
            digest = hashlib.md5()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    digest.update(chunk)
            return digest.hexdigest()
    return digest.hexdigest()


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


def get_aosp_build_out_dir(aosp_path, aosp_version):
    if aosp_version and float(aosp_version) in ["12", "12.1", "13"]:
        abs_source_path = os.path.join(aosp_path, "out/target/product/emulator64_arm64")
    elif aosp_version and float(aosp_version) >= 14:
        abs_source_path = os.path.join(aosp_path, "out/target/product/emu64a")
    else:
        abs_source_path = os.path.join(aosp_path, "out/target/product/emulator64_arm64")
    return abs_source_path


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
    max_attempts = 20
    download_url = None
    retry_delay = 10  # seconds (exponential backoff base)

    if not os.path.exists(artefact_path):
        raise FileNotFoundError(f"Artefact not found at {artefact_path}")
    if not username:
        raise Exception("Username not provided")
    if not password:
        raise Exception("Password not provided")
    if not repo_url:
        raise Exception("Repo url not provided")

    while not is_upload_success and max_attempts > 0:
        logging.debug(f"Uploading image {filename} to repo {repo_url}.")
        try:
            is_upload_success, download_url = upload_image_as_raw(repo_url,
                                                    username,
                                                    password,
                                                    artefact_path,
                                                    filename)
        except Exception as err:
            logging.error(f"Error uploading image: {err}")
        max_attempts -= 1
        if not is_upload_success:
            logging.error(f"Failed to upload image {filename} to repo. Retrying... attempts left={max_attempts}")
            if max_attempts > 0:
                logging.info(f"Waiting {retry_delay}s before next upload attempt")
                try:
                    time.sleep(retry_delay)
                except Exception:
                    pass
                # exponential backoff with cap
                retry_delay = min(retry_delay * 2, 60)
        else:
            logging.info(f"Successfully uploaded image {filename} to repo {repo_url}.")
    return is_upload_success, download_url