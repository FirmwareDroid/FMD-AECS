from aosp_post_build_app_injector import (
    handle_apk_signing,
    handle_jar_module,
    ensure_archive_has_dex,
    build_aosp_apk_index,
    reconcile_and_deduplicate_vendor_apk,
    generate_privapp_permissions_allowlist,
)


def handle_app_modules(file_path, aosp_path, firmware_id, cookies):
    error_message = None
    signing_success, output, subprocess_error_message = handle_apk_signing(file_path, aosp_path, firmware_id, cookies)
    if not signing_success:
        error_message = f"Error signing APK file: {file_path}|{subprocess_error_message}"
    return error_message
