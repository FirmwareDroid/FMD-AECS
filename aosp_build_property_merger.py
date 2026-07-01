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
    """Parses a build.prop file preserving all raw lines and structural text."""
    properties = {}
    file_structure = []  # List of tuples: ('text', raw_line) OR ('prop', key)

    if not os.path.exists(prop_file_path):
        print(f"Error: File {prop_file_path} not found.", file=sys.stderr)
        return properties, file_structure

    with open(prop_file_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            raw_line = line.rstrip('\r\n')
            stripped = raw_line.strip()

            # Separate completely empty lines from structural strings or comments
            if not stripped:
                file_structure.append(('text', ''))
                continue

            if stripped.startswith('#') or '=' not in stripped:
                file_structure.append(('text', raw_line))
                continue

            # It's a property line
            key, val = stripped.split('=', 1)
            key = key.strip()
            val = val.strip()

            properties[key] = val
            file_structure.append(('prop', key))

    return properties, file_structure


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
    allow_filter = False
    is_filtered = False
    # Check against absolute blacklisted keys
    if allow_filter:
        if prop_key in TROUBLESOME_PROPERTIES:
            logging.info(f"[SKIPPED] Trouble key match: {prop_key}")
            is_filtered = True
        # Check against blacklisted prefixes
        if prop_key.startswith(TROUBLESOME_PREFIXES):
            logging.info(f"[SKIPPED] Trouble prefix match: {prop_key}")
            is_filtered = True
    return is_filtered


def merge_properties(aosp_props, vendor_props, aosp_structure, vendor_structure):
    merged_output_lines = []
    conflict_props = {}

    # 1. Map out final desired pairs (AOSP overrides Vendor)
    temp_final_values = {}
    for key, value in vendor_props.items():
        if filter_prop(key):
            continue
        temp_final_values[key] = value

    for key, value in aosp_props.items():
        if filter_prop(key):
            continue
        if key in temp_final_values:
            conflict_props[key] = (value, temp_final_values[key])
            logging.info(f"[CONFLICT] Key '{key}' exists in both. Using AOSP value: '{value}'")
        temp_final_values[key] = value

    # Keep a tracker of keys that successfully found a home in the AOSP layout
    written_keys = set()

    # 2. Rebuild line-by-line using AOSP's blueprint layout
    for line_type, data in aosp_structure:
        if line_type == 'text':
            merged_output_lines.append(data)
        elif line_type == 'prop':
            key = data
            if key in temp_final_values:
                value = temp_final_values[key]
                merged_output_lines.append(f"{key}={value}")
                written_keys.add(key)

    # 3. Stream the remaining vendor structure down in its exact original order
    # We only inject the header if there is actually something unique to add
    has_header = False

    for line_type, data in vendor_structure:
        if line_type == 'text':
            # Skip empty spacing buffers, but retain raw text, comments, and imports
            # Check to ensure we don't accidentally duplicate a text line that AOSP already had
            if data and data not in merged_output_lines:
                if not has_header:
                    merged_output_lines.append("\n# Added unique structural entries and properties from Vendor build")
                    has_header = True
                merged_output_lines.append(data)

        elif line_type == 'prop':
            key = data
            # If this key wasn't swallowed by the AOSP layout and wasn't filtered
            if key not in written_keys and key in temp_final_values:
                if not has_header:
                    merged_output_lines.append("\n# Added unique structural entries and properties from Vendor build")
                    has_header = True
                value = temp_final_values[key]
                merged_output_lines.append(f"{key}={value}")
                written_keys.add(key)

    return merged_output_lines, conflict_props



def generate_output_string(merged_output_lines):
    # Simply join the lines with newlines
    return "\n".join(merged_output_lines) + "\n"

def generate_conflict_string(conflict_props):
    """Safely formats the conflict tuples for readability in the conflict log."""
    output_lines = []
    for key, (aosp_val, vendor_val) in conflict_props.items():
        output_lines.append(f"{key} -> AOSP: {aosp_val} | Vendor: {vendor_val}")
    return "\n".join(output_lines) + "\n"


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

def start_property_merge(aosp_prop_path, vendor_prop_file_path, out_file_path, conflicts_out_file_path):
    try:
        logging.info(f"Starting property merging with AOSP: {aosp_prop_path} and Vendor: {vendor_prop_file_path} to {out_file_path} and {conflicts_out_file_path}")
        # Parse files and retain their structural tracking streams
        aosp_props, aosp_structure = parse_properties(aosp_prop_path)
        vendor_props, vendor_structure = parse_properties(vendor_prop_file_path)

        # Merge execution passing down both stream footprints
        merged_lines, conflicts = merge_properties(aosp_props, vendor_props, aosp_structure, vendor_structure)

        out_str = generate_output_string(merged_lines)
        conflict_out_str = generate_conflict_string(conflicts)
        with open(out_file_path, mode='w', encoding='utf-8') as f:
            f.write(out_str)

        with open(conflicts_out_file_path, mode='w', encoding='utf-8') as f:
            f.write(conflict_out_str)
    except Exception as e:
        logging.error(e)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 generate_flash_props.py <path_to_build.prop>")
        sys.exit(1)
    args = sys.argv
    if len(args) < 5:
        print("Usage: python3 script.py <aosp_file> <vendor_file> <output_file> <conflict_file>", file=sys.stderr)
        sys.exit(1)
    aosp_file_path = args[1]
    vendor_file_path = args[2]
    output_file_path = args[3]
    conf_output_file_path = args[4]
    start_property_merge(aosp_file_path, vendor_file_path, output_file_path, conf_output_file_path)