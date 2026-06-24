import logging
import os
from pathlib import Path

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


def add_oneshot_to_services(file_path: str) -> None:
    """
    Parses an AOSP .rc file and ensures that all services have the 'oneshot' flag
    so they do not restart if they fail or exit. Modifies the file in-place.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"The file {file_path} does not exist.")

    lines = path.read_text().splitlines()
    modified_lines = []

    inside_service = False
    has_oneshot = False
    service_indent = "    "  # Default fallback indent for the oneshot flag

    for line in lines:
        stripped = line.strip()

        # If we hit a new section (service, on, import), we are leaving the previous service block
        if stripped.startswith(('service ', 'on ', 'import ')):
            # If we were just inside a service block and it didn't have 'oneshot', add it before moving on
            if inside_service and not has_oneshot:
                modified_lines.append(f"{service_indent}oneshot  # Added to prevent restarts on failure")

            # Reset flags for the new block
            inside_service = stripped.startswith('service ')
            has_oneshot = False
            modified_lines.append(line)
            continue

        # Track if the service already has a oneshot flag
        # We split by '#' and strip to ignore any inline comments (e.g., 'oneshot  # comment')
        if inside_service and stripped:
            command_only = stripped.split('#')[0].strip()
            if command_only == 'oneshot':
                has_oneshot = True

            # Dynamically capture the indentation used inside this service block
            if not line.startswith((' ', '\t')):
                # This handles edge cases where indentation might be weird
                pass
            else:
                # Capture the leading whitespace of the current option line to match style
                service_indent = line[:len(line) - len(line.lstrip())]

        modified_lines.append(line)

    # Catch the very last service if the file ends while inside a service block
    if inside_service and not has_oneshot:
        modified_lines.append(f"{service_indent}oneshot  # Added to prevent restarts on failure")

    # Write the modified content back
    path.write_text('\n'.join(modified_lines) + '\n')
    logger.info(f"Success: Added oneshot to services in {file_path}")


def handle_init_rc(source_file_path):
    comment_out_boringssl_check(source_file_path)
    return source_file_path


def handle_vendor_init_rc(source_file_path):
    defuse_critical_services(source_file_path)
    return source_file_path


def run_rc_merger(source_file_path):
    filename = os.path.basename(source_file_path)
    if filename == "init.rc" and "/system/init/hw/" in source_file_path:
        target_file_path = handle_init_rc(source_file_path)
    else:
        logger.error(f"Modifying rc file inplace: {source_file_path}")
        add_oneshot_to_services(source_file_path)
        target_file_path = handle_vendor_init_rc(source_file_path)

    return target_file_path

#("/vendor/etc/init/" in source_file_path
#          or "/product/etc/init/" in source_file_path
#          or "/system_ext/etc/init/" in source_file_path):