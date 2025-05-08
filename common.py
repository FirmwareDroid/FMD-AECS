import os
import re
import zipfile

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

    if match:
        return match.group(1)  # Return the vendor name from the filename

    # Fallback: Infer vendor name from directory structure
    if directory:
        for part in directory.split(os.sep):
            if part.lower() in VENDOR_NAMES:
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

    if match:
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
    vendor_name_list.append(vendor_name)
    words_to_replace = []
    for name in vendor_name_list:
        words_to_replace.append(f".{name.lower()}")
        words_to_replace.append(name)

    return words_to_replace


def remove_vendor_name_from_path(file_path):
    words_to_replace = get_vendor_words(file_path)
    file_path_vendor_replaced = file_path
    for word in words_to_replace:
        file_path_vendor_replaced = file_path_vendor_replaced.replace(word, "")

    return file_path_vendor_replaced


def remove_vendor_name_from_filename(filename):
    words_to_replace = get_vendor_words(filename=filename)
    filename_no_ext = filename
    for word in words_to_replace:
        filename_no_ext = filename.replace(word, "")
    return filename_no_ext