import logging
import os
from pathlib import Path


def comment_out_boringssl_check(file_path):
    target_string = "reboot_on_failure reboot,boringssl-self-check-failed"
    replacement_string = "#reboot_on_failure reboot,boringssl-self-check-failed"

    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            file_contents = file.read()
        if target_string in file_contents and replacement_string not in file_contents:
            updated_contents = file_contents.replace(target_string, replacement_string)
            with open(file_path, 'w', encoding='utf-8') as file:
                file.write(updated_contents)
            logging.info(f"Success: Updated {file_path}")
        elif replacement_string in file_contents:
            logging.info(f"Notice: The line is already commented out in {file_path}")
        else:
            logging.warning(f"Warning: Target string not found in {file_path}")
    except FileNotFoundError:
        logging.error(f"Error: The file at {file_path} was not found.")
    except Exception as e:
        logging.error(f"An error occurred: {e}")


def defuse_critical_services(file_path: str) -> None:
    """
    Parses an AOSP .rc file and comments out the 'critical' flag
    within service blocks to prevent system reboots on service failure.
    Modifies the file in-place.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"The file {file_path} does not exist.")

    # Read the original file content
    content = path.read_text()

    # Split the file into lines for processing
    lines = content.splitlines()
    modified_lines = []

    inside_service = False

    for line in lines:
        stripped = line.strip()

        # Detect the start of a new section
        if stripped.startswith(('service ', 'on ', 'import ')):
            # If it's a service block, turn on the flag. Otherwise, turn it off.
            inside_service = stripped.startswith('service ')

        # If we are inside a service block and find the critical flag
        if inside_service and stripped == 'critical':
            # Preserve the original indentation but comment out the flag
            indent = line[:line.find('critical')]
            line = f"{indent}# critical  # Disabled to prevent boot-loop/reboots"

        modified_lines.append(line)

    # Write the modified content back to the file
    path.write_text('\n'.join(modified_lines) + '\n')
    logging.info(f"Success: Updated {file_path}: Lines modified: {modified_lines}")


def handle_init_rc(source_file_path):
    comment_out_boringssl_check(source_file_path)
    return source_file_path


def handle_vendor_init_rc(source_file_path):
    defuse_critical_services(source_file_path)
    return source_file_path


def run_rc_merger(source_file_path):
    target_file_path = source_file_path
    filename = os.path.basename(source_file_path)
    if filename == "init.rc" and "/system/init/hw/" in source_file_path:
        target_file_path = handle_init_rc(source_file_path)
    elif ("/vendor/etc/init/" in source_file_path
          or "/product/etc/init/" in source_file_path
          or "/system_ext/etc/init/" in source_file_path):
        logging.error(f"Modifying rc file inplace: {source_file_path}")
        target_file_path = handle_vendor_init_rc(source_file_path)
    else:
        logging.info(f"Skipping the processing of RC file {source_file_path}. No handler defined.")
    return target_file_path