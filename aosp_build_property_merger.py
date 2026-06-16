#!/usr/bin/env python3
import logging
import sys
import os

# Define property keys or prefixes that should NEVER be injected into the AOSP build.
# These usually interfere with emulator boot, hardware detection, or virtualized storage.
TROUBLESOME_PROPERTIES = {
    # --- Core Hardware / SoC Platform Identity ---
    "ro.hardware",
    "ro.boot.hardware",
    "ro.chipname",
    "ro.board.platform",
    "ro.arch",
    "ro.product.board",
    "ro.product.cpu.abi",  # Let the emulator specify its own ABI target (x86_64 vs arm64)
    "ro.product.cpu.abilist",

    # --- Storage & Encryption Layouts ---
    "ro.crypto.state",
    "ro.crypto.type",
    "ro.crypto.volume.filenames_mode",
    "vold.post_fs_data_done",
    "ro.vold.unmount_fstab",

    # --- Verified Boot (AVB) & Bootloader Security ---
    "ro.build.fingerprint",
    "ro.build.description",
    "ro.boot.vbmeta.device_state",
    "ro.boot.verifiedbootstate",
    "ro.boot.flash.locked",
    "ro.boot.veritymode",
    "ro.boot.warranty_bit",
    "ro.secure",  # Forcing this from a real device can break adb root in userdebug emulator builds

    # --- Display, SurfaceFlinger & Graphics Drivers ---
    "ro.opengles.version",
    # Crucial: Let the emulator host handle its own GLES capabilities (e.g., SwiftShader or host passthrough)
    "ro.sf.lcd_density",  # Can distort virtual screen bounds or cause window manager crashes
    "ro.hardware.egl",  # Dictates specific physical rendering libs (e.g., "adreno" or "mali")
    "ro.hardware.gralloc",
    "ro.hardware.hwcomposer",
    "ro.surface_flinger.max_frame_buffer_acquired_buffers",

    # --- Radio, Cellular (RIL) & SIM Profile ---
    "ro.radio.noril",
    "gsm.version.ril-impl",
    "ro.telephony.default_network",
    "ril.subscription.types",

    # --- Biometrics, Security Chips & Camera Abstractions ---
    "ro.hardware.gatekeeper",
    "ro.hardware.fingerprint",
    "ro.hardware.keystore",  # Ties keystore to a physical hardware backed enclave (TEE/StrongBox)
    "sys.usb.config",
}

# Entire property prefixes to catch dynamic, runtime, or vendor-specific families
TROUBLESOME_PREFIXES = (
    "ro.boot.",  # Bootloader tags (command-line arguments passed from physical firmware)
    "sys.usb.",  # Physical USB peripheral configuration loops
    "init.svc.vendor.",  # Hardware service initializers tied to missing physical binaries
    "persist.vendor.radio",  # Cellular power controls and modem configurations
    "vendor.display.",  # Vendor-specific display engine configurations (e.g., QTI or Exynos engine tweaks)
    "ro.vendor.gfx.",  # Overrides for physical graphics pipelines
    "persist.bluetooth.",  # Hardware-bound Bluetooth controller adjustments
    "persist.vendor.fingerprint.",  # Proprietary biometric modules
)


def parse_properties(prop_file_path):
    """Parses a build.prop file into a dictionary of key-value pairs."""
    properties = {}
    if not os.path.exists(prop_file_path):
        print(f"Error: File {prop_file_path} not found.", file=sys.stderr)
        return properties

    with open(prop_file_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, val = line.split('=', 1)
                properties[key.strip()] = val.strip()
    return properties


def categorize_property(key):
    """Decides the correct AOSP Makefile variable based on Treble property prefixes."""
    if key.startswith('vendor.') or key.startswith('ro.vendor.') or key.startswith('persist.vendor.'):
        return 'PRODUCT_VENDOR_PROPERTIES'
    elif key.startswith('product.') or key.startswith('ro.product.') or key.startswith('persist.product.'):
        if any(x in key for x in ['.system.brand', '.system.name', '.system.device', '.system.model']):
            return 'PRODUCT_SYSTEM_PROPERTIES'
        return 'PRODUCT_PRODUCT_PROPERTIES'
    elif key.startswith('system_ext.') or key.startswith('ro.system_ext.') or key.startswith('persist.system_ext.'):
        return 'PRODUCT_SYSTEM_EXT_PROPERTIES'
    elif key.startswith('odm.') or key.startswith('ro.odm.') or key.startswith('persist.odm.'):
        return 'PRODUCT_ODM_PROPERTIES'
    else:
        return 'PRODUCT_SYSTEM_PROPERTIES'


def filter_prop(prop_key):
    is_filtered = False
    # Check against absolute blacklisted keys
    if prop_key in TROUBLESOME_PROPERTIES:
        logging.info(f"[SKIPPED] Trouble key match: {prop_key}")
        is_filtered = True
    # Check against blacklisted prefixes
    if prop_key.startswith(TROUBLESOME_PREFIXES):
        logging.info(f"[SKIPPED] Trouble prefix match: {prop_key}")
        is_filtered = True
    return is_filtered


def merge_properties(aosp_props, vendor_props):
    merged_properties = {}
    conflict_props = {}
    for key, value in aosp_props.items():
        if filter_prop(key):
            continue
        merged_properties[key] = value

    for key, value in vendor_props.items():
        if filter_prop(key):
            continue
        if key not in merged_properties:
            merged_properties[key] = value
        else:
            conflict_props[key] = (merged_properties[key], value)
            logging.info(f"[CONFLICT] Key '{key}' exists in both AOSP and Vendor properties. Using AOSP value: '{merged_properties[key]}'")

    return merged_properties, conflict_props

def generate_output_string(properties):
    output_string = ""
    for key, value in properties.items():
        output_string += f"{key}={value}\n"
    return output_string


def generate_makefile_string(properties):
    """Generates the dynamic Makefile configuration string while skipping troublesome properties."""
    grouped_props = {
        'PRODUCT_SYSTEM_PROPERTIES': [],
        'PRODUCT_VENDOR_PROPERTIES': [],
        'PRODUCT_PRODUCT_PROPERTIES': [],
        'PRODUCT_SYSTEM_EXT_PROPERTIES': [],
        'PRODUCT_ODM_PROPERTIES': []
    }
    for key, value in properties.items():
        target_var = categorize_property(key)
        grouped_props[target_var].append(f"    {key}={value}")

    mk_lines = [
        "# ========================================================",
        "# Automatically Generated Target Re-hosting Properties",
        "# ========================================================\n"
    ]

    for var_name, prop_list in grouped_props.items():
        if prop_list:
            mk_lines.append(f"{var_name} += \\")
            for prop in sorted(prop_list[:-1]):
                mk_lines.append(f"{prop} \\")
            mk_lines.append(f"{sorted(prop_list)[-1]}\n")

    return "\n".join(mk_lines)

def start_merge(args):
    input_file = args[1]
    vendor_file_path = args[2]
    output_file_path = args[3]
    conf_output_file_path = args[4]
    parsed_props = parse_properties(input_file)
    vendor_props = parse_properties(vendor_file_path)
    merged_props, conflict_props = merge_properties(parsed_props, vendor_props)
    conflict_out_str = generate_output_string(conflict_props)
    out_str = generate_output_string(merged_props)
    with open(output_file_path, mode='w', encoding='utf-8') as f:
        f.write(out_str)

    with open(conf_output_file_path, mode='w', encoding='utf-8') as f:
        f.write(conflict_out_str)



if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 generate_flash_props.py <path_to_build.prop>")
        sys.exit(1)
    start_merge(sys.argv)