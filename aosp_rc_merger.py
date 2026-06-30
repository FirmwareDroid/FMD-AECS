import logging
import os
from pathlib import Path
from ConfigManager import ConfigManager

POST_INJECTOR_CONFIG = {}
logger = logging.getLogger("semantic_injector")

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
            logger.info(f"Success: Updated {file_path}")
        elif replacement_string in file_contents:
            logger.info(f"Notice: The line is already commented out in {file_path}")
        else:
            logger.warning(f"Warning: Target string not found in {file_path}")
    except FileNotFoundError:
        logger.error(f"Error: The file at {file_path} was not found.")
    except Exception as e:
        logger.error(f"An error occurred: {e}")


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
    logger.info(f"Success: Updated {file_path}: Lines modified: {modified_lines}")



def add_flag_to_services(file_path: str, target_flag: str, comment_suffix: str = "") -> None:
    """
    Generic function that parses an AOSP .rc file and ensures that all services
    have a specific flag (e.g., 'oneshot', 'disabled'). Modifies the file in-place.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"The file {file_path} does not exist.")

    lines = path.read_text().splitlines()
    modified_lines = []

    inside_service = False
    has_flag = False
    service_indent = "    "  # Default fallback indent

    # Format the appended comment string if one is provided
    appended_string = f"{target_flag}  {comment_suffix}".rstrip()

    for line in lines:
        stripped = line.strip()

        # If we hit a new section, we are leaving the previous block
        if stripped.startswith(('service ', 'on ', 'import ')):
            # If we were just inside a service block and it didn't have the flag, add it
            if inside_service and not has_flag:
                modified_lines.append(f"{service_indent}{appended_string}")

            # Reset state for the new block
            inside_service = stripped.startswith('service ')
            has_flag = False
            modified_lines.append(line)
            continue

        # Track if the service already has the target flag
        # Split by '#' and strip to ignore inline comments
        if inside_service and stripped:
            command_only = stripped.split('#')[0].strip()
            if command_only == target_flag:
                has_flag = True

            # Dynamically capture the indentation used inside this service block
            if not line.startswith((' ', '\t')):
                pass
            else:
                service_indent = line[:len(line) - len(line.lstrip())]

        modified_lines.append(line)

    # Catch the very last service if the file ends while inside a service block
    if inside_service and not has_flag:
        modified_lines.append(f"{service_indent}{appended_string}")

    # Write the modified content back
    path.write_text('\n'.join(modified_lines) + '\n')
    logger.info(f"Success: Added '{target_flag}' to services in {file_path}")


def add_oneshot_to_services(file_path: str) -> None:
    """
    Parses an AOSP .rc file and ensures that all services have the 'oneshot' flag
    so they do not restart if they fail or exit.
    """
    add_flag_to_services(file_path, "oneshot", "# Added to prevent restarts on failure")


def add_disabled_to_services(file_path: str) -> None:
    """
    Add the "disabled" flag to a service, which prevents the service from starting
    at boot time, making it effectively a lazy service.
    """
    add_flag_to_services(file_path, "disabled", "# Added to prevent starting at boot (lazy service)")


def handle_init_rc(source_file_path):
    comment_out_boringssl_check(source_file_path)
    return source_file_path


def handle_vendor_init_rc(source_file_path):
    defuse_critical_services(source_file_path)
    return source_file_path


def run_rc_merger(source_file_path):
    global POST_INJECTOR_CONFIG
    POST_INJECTOR_CONFIG = ConfigManager.get_config("POST_INJECTOR_CONFIG")

    filename = os.path.basename(source_file_path)
    if filename == "init.rc" and "/system/init/hw/" in source_file_path:
        target_file_path = handle_init_rc(source_file_path)
    else:
        logger.info(f"Modifying rc file inplace: {source_file_path}")
        if "/system_ext/" in source_file_path:
            add_oneshot_to_services(source_file_path)
        # add_disabled_to_services(source_file_path)  # Uncomment to apply to all vendor init scripts
        target_file_path = handle_vendor_init_rc(source_file_path)

    return target_file_path
