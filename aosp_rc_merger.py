import logging
import os
from pathlib import Path
from ConfigManager import ConfigManager

POST_INJECTOR_CONFIG = {}
logger = logging.getLogger("semantic_injector")

# Essential AOSP / emulator services that should remain restartable if their .rc is touched
CORE_KEEPALIVE_SERVICES = {
    "servicemanager",
    "hwservicemanager",
    "vndservicemanager",
    "surfaceflinger",
    "zygote",
    "zygote_secondary",
    "audioserver",
    "mediaserver",
    "cameraserver",
    "drmserver",
    "keystore2",
    "netd",
    "vold",
    "lmkd",
    "logd",
    "ueventd",
    "adbd",
    "console",
    "statsd",
    "installd",
    "storaged",
    "wificond",
    "tombstoned",
    "gatekeeperd",
    "credstore",
}


def comment_out_boringssl_check(file_path):
    target_string = "reboot_on_failure"
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as file:
            lines = file.readlines()
        modified = False
        updated_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith('#') and stripped.startswith(target_string):
                indent = line[:len(line) - len(line.lstrip())]
                updated_lines.append(f"{indent}# {stripped}  # Disabled by FMD-AECS to prevent reboot on failure\n")
                modified = True
            else:
                updated_lines.append(line)
        if modified:
            with open(file_path, 'w', encoding='utf-8') as file:
                file.writelines(updated_lines)
            logger.info(f"Success: Commented out reboot_on_failure in {file_path}")
    except FileNotFoundError:
        logger.error(f"Error: The file at {file_path} was not found.")
    except Exception as e:
        logger.error(f"An error occurred in comment_out_boringssl_check: {e}")


def defuse_critical_services(file_path: str) -> None:
    """
    Parses an AOSP .rc file and comments out 'critical', 'reboot_on_failure',
    and blocking physical device 'mount_all'/'swapon_all'/'wait /dev/' commands
    to prevent system reboots or boot hangs on missing physical hardware.
    Modifies the file in-place.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"The file {file_path} does not exist.")

    content = path.read_text(encoding='utf-8', errors='ignore')
    lines = content.splitlines()
    modified_lines = []

    inside_service = False
    is_goldfish_rc = any(k in path.name.lower() for k in ("ranchu", "goldfish"))

    for line in lines:
        stripped = line.strip()
        command_only = stripped.split('#')[0].strip()

        # Detect the start of a new section
        if stripped.startswith(('service ', 'on ', 'import ')):
            inside_service = stripped.startswith('service ')

        if not stripped.startswith('#'):
            # Defuse 'critical' and 'reboot_on_failure' inside service blocks or action triggers
            if inside_service and (command_only == 'critical' or command_only.startswith('critical ')):
                indent = line[:len(line) - len(line.lstrip())]
                line = f"{indent}# {stripped}  # Disabled to prevent boot-loop/reboots"
            elif command_only.startswith('reboot_on_failure'):
                indent = line[:len(line) - len(line.lstrip())]
                line = f"{indent}# {stripped}  # Disabled to prevent reboot_on_failure panic"
            elif not is_goldfish_rc and not inside_service and command_only.startswith(('mount_all ', 'swapon_all ', 'wait /dev/block/', 'wait /dev/qseecom')):
                indent = line[:len(line) - len(line.lstrip())]
                line = f"{indent}# {stripped}  # Disabled physical block/device wait for emulator compatibility"

        modified_lines.append(line)

    path.write_text('\n'.join(modified_lines) + '\n', encoding='utf-8')
    logger.info(f"Success: Defused critical/reboot_on_failure directives in {file_path}")


def add_flag_to_services(file_path: str, target_flag: str, comment_suffix: str = "") -> None:
    """
    Generic function that parses an AOSP .rc file and ensures that non-core services
    have a specific flag (e.g., 'oneshot', 'disabled'). Modifies the file in-place.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"The file {file_path} does not exist.")

    lines = path.read_text(encoding='utf-8', errors='ignore').splitlines()
    modified_lines = []

    inside_service = False
    skip_current_service = False
    has_flag = False
    service_indent = "    "  # Default fallback indent

    appended_string = f"{target_flag}  {comment_suffix}".rstrip()

    for line in lines:
        stripped = line.strip()

        # If we hit a new section, we are leaving the previous block
        if stripped.startswith(('service ', 'on ', 'import ')):
            if inside_service and not skip_current_service and not has_flag:
                modified_lines.append(f"{service_indent}{appended_string}")

            inside_service = stripped.startswith('service ')
            skip_current_service = False
            if inside_service:
                parts = stripped.split()
                service_name = parts[1] if len(parts) > 1 else ""
                if service_name in CORE_KEEPALIVE_SERVICES:
                    skip_current_service = True
            has_flag = False
            modified_lines.append(line)
            continue

        if inside_service and stripped:
            command_only = stripped.split('#')[0].strip()
            if command_only == target_flag:
                has_flag = True

            if line.startswith((' ', '\t')):
                service_indent = line[:len(line) - len(line.lstrip())]

        modified_lines.append(line)

    if inside_service and not skip_current_service and not has_flag:
        modified_lines.append(f"{service_indent}{appended_string}")

    path.write_text('\n'.join(modified_lines) + '\n', encoding='utf-8')
    logger.info(f"Success: Added '{target_flag}' to services in {file_path}")


def add_oneshot_to_services(file_path: str) -> None:
    """
    Parses an AOSP .rc file and ensures that non-core services have the 'oneshot' flag
    so they do not restart infinitely if they fail or exit.
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
    defuse_critical_services(source_file_path)
    return source_file_path


def handle_vendor_init_rc(source_file_path):
    defuse_critical_services(source_file_path)
    return source_file_path


def run_rc_merger(source_file_path):
    global POST_INJECTOR_CONFIG
    POST_INJECTOR_CONFIG = ConfigManager.get_config("POST_INJECTOR_CONFIG")

    filename = os.path.basename(source_file_path)
    if filename == "init.rc" and ("/init/hw/" in source_file_path or "/etc/init/hw/" in source_file_path):
        target_file_path = handle_init_rc(source_file_path)
    else:
        logger.info(f"Modifying rc file inplace: {source_file_path}")
        add_oneshot_to_services(source_file_path)
        target_file_path = handle_vendor_init_rc(source_file_path)

    return target_file_path
