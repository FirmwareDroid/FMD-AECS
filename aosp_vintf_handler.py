#!/usr/bin/env python3
import os
import sys
import shutil
import argparse
import subprocess
import tempfile
import logging
import re
import xml.etree.ElementTree as ET


def setup_logging():
    """Configures the global logging format if not already configured."""
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(message)s',
            handlers=[logging.StreamHandler(sys.stdout)]
        )


def copy_vintf_files(src_dir, dest_dir, overwrite=True):
    """Recursively discovers and copies all XML files."""
    if not os.path.exists(src_dir):
        logging.warning("Source directory %s does not exist. Skipping.", src_dir)
        return

    for root, _, files in os.walk(src_dir):
        for file in files:
            if file.startswith('.'):
                continue
            if file.endswith(".xml"):
                src_path = os.path.join(root, file)
                dest_path = os.path.join(dest_dir, file)

                if not overwrite and os.path.exists(dest_path):
                    continue
                shutil.copy2(src_path, dest_path)


def inject_permissive_attributes(file_path):
    """Injects override="true" into manifests and optional="true" into matrices."""
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        modified = False

        is_matrix = "compatibility_matrix" in root.tag or "compatibility_matrix" in os.path.basename(file_path)

        for hal in root.findall('.//hal'):
            if is_matrix:
                if hal.get('optional') != 'true':
                    hal.set('optional', 'true')
                    modified = True
            else:
                if hal.get('override') != 'true':
                    hal.set('override', 'true')
                    modified = True

        if modified:
            tree.write(file_path, encoding="utf-8", xml_declaration=True)
            logging.debug("[*] Injected permissive attributes into: %s", os.path.basename(file_path))
    except ET.ParseError:
        logging.warning("Failed to parse %s for permissive injection (Malformed XML).", file_path)


def extract_hal_names_from_file(file_path):
    """Parses an XML file safely and extracts all HAL names declared within <hal> tags."""
    hal_names = set()
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        for hal in root.findall('.//hal'):
            name_node = hal.find('name')
            if name_node is not None and name_node.text:
                hal_names.add(name_node.text.strip())
    except ET.ParseError:
        hal_pattern = re.compile(r"<hal[^>]*>.*?<name>([a-zA-Z0-9_\.]+)</name>", re.DOTALL)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                for match in hal_pattern.finditer(content):
                    hal_names.add(match.group(1).strip())
        except Exception:
            pass
    return hal_names


def remove_duplicate_hals_from_file(file_path, target_hals):
    """Surgically extracts and removes entire <hal> blocks matching target_hals."""
    if not target_hals:
        return False
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        modified = False

        hals_to_remove = []
        for hal in root.findall('.//hal'):
            name_node = hal.find('name')
            if name_node is not None and name_node.text and name_node.text.strip() in target_hals:
                hals_to_remove.append(hal)

        for hal in hals_to_remove:
            for parent in root.findall('.//'):
                if hal in list(parent):
                    parent.remove(hal)
                    modified = True
            if hal in list(root):
                root.remove(hal)
                modified = True

        if modified:
            remaining_hals = root.findall('.//hal')
            if not remaining_hals and root.tag != 'manifest':
                return "EMPTY"

            tree.write(file_path, encoding="utf-8", xml_declaration=True)
            return True
    except ET.ParseError:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            modified = False
            for hal in target_hals:
                pattern = r"<hal[^>]*>(?:(?!<\/hal>).)*?<name>" + re.escape(hal) + r"<\/name>.*?<\/hal>"
                if re.search(pattern, content, re.DOTALL):
                    content = re.sub(pattern, "", content, flags=re.DOTALL)
                    modified = True

            if modified:
                if not re.search(r"<hal[^>]*>", content) and "manifest" not in os.path.basename(file_path):
                    return "EMPTY"
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)
                return True
        except Exception:
            pass
    return False


def get_vintf_type(file_path):
    """Reads the root element to determine if this is a 'device' or 'framework' VINTF file."""
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        return root.get('type', 'device')  # Default to device if not specified
    except ET.ParseError:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                if re.search(r'<[^>]*type\s*=\s*[\"\']framework[\"\']', content):
                    return 'framework'
        except Exception:
            pass
    return 'device'


def separate_by_type(file_paths):
    """Classifies a list of file paths into device and framework categories."""
    device_files = []
    framework_files = []
    for fp in file_paths:
        vintf_type = get_vintf_type(fp)
        if vintf_type == 'framework':
            framework_files.append(fp)
        else:
            device_files.append(fp)
    return device_files, framework_files


def compile_vintf_group(assemble_bin, files_list, output_path, default_primary_name, fatal=True):
    """Handles the finding of the primary manifest and passing it to assemble_vintf."""
    if not files_list:
        return None

    primary = None
    for f in files_list:
        if os.path.basename(f) == default_primary_name:
            primary = f
            break

    # Fallback if preferred primary name isn't found
    if not primary:
        primary = files_list[0]

    fragments = [f for f in files_list if f != primary]

    run_assemble_vintf(assemble_bin, primary, fragments, output_path, fatal=fatal)
    return output_path


def run_assemble_vintf(binary_path, base_file, fragment_files, output_file, check_file=None, forced_version=None,
                       fatal=True):
    """Executes assemble_vintf matching its precise CLI requirements."""
    env = os.environ.copy()
    env["BOARD_SEPOLICY_VERS"] = forced_version if forced_version else "10000.0"

    if check_file:
        env["PRODUCT_ENFORCE_VINTF_MANIFEST"] = "true"

    cmd = [binary_path]
    cmd.extend(["-i", base_file])

    for f in fragment_files:
        cmd.extend(["-i", f])

    cmd.extend(["-o", output_file])

    if check_file:
        cmd.extend(["-c", check_file])

    logging.info("Executing command: %s", ' '.join(cmd))
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
        logging.info("Successfully generated: %s", os.path.basename(output_file))
    except subprocess.CalledProcessError as e:
        stderr_output = e.stderr.strip()

        # Auto-recover sepolicy version profile mismatch
        match = re.search(r"Cannot override existing value ([\d\.]+) with BOARD_SEPOLICY_VERS", stderr_output)
        if match and not forced_version:
            detected_version = match.group(1)
            logging.warning("Detected version restriction profile conflict. Auto-recovering using: %s",
                            detected_version)
            return run_assemble_vintf(binary_path, base_file, fragment_files, output_file, check_file,
                                      forced_version=detected_version, fatal=fatal)

        error_msg = f"assemble_vintf failed.\nCommand: {' '.join(cmd)}\nSTDOUT: {e.stdout.strip()}\nSTDERR: {stderr_output}"

        if fatal:
            raise RuntimeError(error_msg) from e
        else:
            logging.warning("NON-FATAL ERROR: %s", error_msg)
            logging.warning("Validation failed, but skipping exit to maximize boot probability.")


def merge_vintf_artifacts(aosp_dir, vendor_dir, host_bin_dir, output_dir):
    setup_logging()
    logging.info("Starting Permissive VINTF artifact merging process...")
    assemble_vintf_bin = os.path.join(host_bin_dir, "assemble_vintf")

    if not os.path.isfile(assemble_vintf_bin):
        raise FileNotFoundError(f"Could not find required 'assemble_vintf' executable at: {assemble_vintf_bin}")

    os.makedirs(output_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as temp_dir:
        manifest_temp = os.path.join(temp_dir, "manifests")
        os.makedirs(manifest_temp)

        aosp_stage = os.path.join(temp_dir, "aosp_stage")
        vendor_stage = os.path.join(temp_dir, "vendor_stage")
        os.makedirs(aosp_stage)
        os.makedirs(vendor_stage)

        logging.info("--- Phase 1: Gathering files into temporary workspace ---")
        copy_vintf_files(aosp_dir, aosp_stage, overwrite=True)
        copy_vintf_files(vendor_dir, vendor_stage, overwrite=True)

        base_manifest_source = os.path.join(aosp_stage, "manifest.xml")
        if not os.path.exists(base_manifest_source):
            base_manifest_source = os.path.join(vendor_stage, "manifest.xml")

        primary_manifest_hals = set()
        if os.path.exists(base_manifest_source):
            primary_manifest_hals = extract_hal_names_from_file(base_manifest_source)

        vendor_hal_signatures = set()
        for file_name in os.listdir(vendor_stage):
            if file_name.endswith(".xml") and "compatibility_matrix" not in file_name:
                vendor_hal_signatures.update(extract_hal_names_from_file(os.path.join(vendor_stage, file_name)))

        global_exclusion_signatures = vendor_hal_signatures.union(primary_manifest_hals)

        # 2. Process AOSP files
        logging.info("Processing baseline AOSP configurations...")
        for file_name in os.listdir(aosp_stage):
            aosp_file_path = os.path.join(aosp_stage, file_name)
            dest_file_path = os.path.join(manifest_temp, file_name)

            if file_name.endswith(".xml") and "compatibility_matrix" not in file_name:
                if file_name == "manifest.xml":
                    shutil.copy2(aosp_file_path, dest_file_path)
                    remove_duplicate_hals_from_file(dest_file_path, vendor_hal_signatures)
                    inject_permissive_attributes(dest_file_path)
                    continue

                aosp_hals = extract_hal_names_from_file(aosp_file_path)
                if aosp_hals and aosp_hals.issubset(global_exclusion_signatures):
                    continue

                shutil.copy2(aosp_file_path, dest_file_path)
                status = remove_duplicate_hals_from_file(dest_file_path, global_exclusion_signatures)
                if status == "EMPTY":
                    os.remove(dest_file_path)
                else:
                    inject_permissive_attributes(dest_file_path)
            else:
                shutil.copy2(aosp_file_path, dest_file_path)
                inject_permissive_attributes(dest_file_path)

        # 3. Process Vendor files
        logging.info("Processing Vendor configurations...")
        for file_name in os.listdir(vendor_stage):
            vendor_file_path = os.path.join(vendor_stage, file_name)
            dest_file_path = os.path.join(manifest_temp, file_name)

            if file_name.endswith(".xml") and "compatibility_matrix" not in file_name:
                if file_name == "manifest.xml":
                    if os.path.exists(dest_file_path):
                        dest_file_path = os.path.join(manifest_temp, "manifest_vendor_root.xml")
                    shutil.copy2(vendor_file_path, dest_file_path)
                    remove_duplicate_hals_from_file(dest_file_path, primary_manifest_hals)
                    inject_permissive_attributes(dest_file_path)
                    continue

                vendor_hals = extract_hal_names_from_file(vendor_file_path)
                if vendor_hals and vendor_hals.issubset(primary_manifest_hals):
                    continue

                if os.path.exists(dest_file_path):
                    name_root, ext = os.path.splitext(file_name)
                    dest_file_path = os.path.join(manifest_temp, f"{name_root}_vendor_override{ext}")

                shutil.copy2(vendor_file_path, dest_file_path)
                status = remove_duplicate_hals_from_file(dest_file_path, primary_manifest_hals)
                if status == "EMPTY":
                    os.remove(dest_file_path)
                else:
                    inject_permissive_attributes(dest_file_path)
            else:
                if not os.path.exists(dest_file_path):
                    shutil.copy2(vendor_file_path, dest_file_path)
                    inject_permissive_attributes(dest_file_path)

        # Build final processing arrays
        all_files = [os.path.join(manifest_temp, f) for f in os.listdir(manifest_temp) if f.endswith(".xml")]
        manifest_files = [f for f in all_files if "compatibility_matrix" not in os.path.basename(f)]
        matrix_files = [f for f in all_files if "compatibility_matrix" in os.path.basename(f)]

        # --- NEW ENGINE: Sort by Type (Framework vs Device) ---
        logging.info("Categorizing VINTF artifacts by hardware/framework type...")
        device_manifests, framework_manifests = separate_by_type(manifest_files)
        device_matrices, framework_matrices = separate_by_type(matrix_files)

        # --- PROCESS MANIFESTS ---
        final_manifest_path = None
        logging.info("--- Phase 2A: Processing Device Manifests via assemble_vintf ---")
        if device_manifests:
            final_manifest_path = os.path.join(output_dir, "manifest.xml")
            compile_vintf_group(assemble_vintf_bin, device_manifests, final_manifest_path, "manifest.xml", fatal=True)

        logging.info("--- Phase 2B: Processing Framework Manifests via assemble_vintf ---")
        if framework_manifests:
            compile_vintf_group(assemble_vintf_bin, framework_manifests,
                                os.path.join(output_dir, "framework_manifest.xml"), "framework_manifest.xml",
                                fatal=False)

        # --- PROCESS COMPATIBILITY MATRICES ---
        logging.info("--- Phase 3A: Processing Device Matrices via assemble_vintf ---")
        if device_matrices:
            compile_vintf_group(assemble_vintf_bin, device_matrices,
                                os.path.join(output_dir, "compatibility_matrix.device.xml"),
                                "compatibility_matrix.device.xml", fatal=False)

        logging.info("--- Phase 3B: Processing Framework Matrices via assemble_vintf ---")
        if framework_matrices:
            compile_vintf_group(assemble_vintf_bin, framework_matrices,
                                os.path.join(output_dir, "compatibility_matrix.xml"), "compatibility_matrix.xml",
                                fatal=False)

        # --- Phase 4: Cross Validation ---
        if final_manifest_path:
            framework_matrix_path = os.path.join(aosp_dir, "compatibility_matrix.xml")
            if os.path.exists(framework_matrix_path) and "type=\"framework\"" in open(framework_matrix_path).read():
                logging.info("--- Phase 4: Performing non-fatal cross-validation check ---")
                run_assemble_vintf(
                    binary_path=assemble_vintf_bin,
                    base_file=final_manifest_path,
                    fragment_files=[],
                    output_file="/dev/null",
                    check_file=framework_matrix_path,
                    fatal=False
                )
            else:
                logging.info("--- Phase 4: Skipping Cross-Validation ---")

        logging.info("VINTF merge complete. Unified artifacts successfully exported to: %s", output_dir)


def main():
    parser = argparse.ArgumentParser(
        description="Merge AOSP and Vendor VINTF manifests/matrices cleanly using assemble_vintf post-build."
    )
    parser.add_argument("--aosp-dir", required=True, help="Path to the AOSP VINTF folder containing base profiles")
    parser.add_argument("--vendor-dir", required=True, help="Path to the extracted Vendor VINTF folder")
    parser.add_argument("--host-bin-dir", required=True,
                        help="Path to AOSP host tools directory containing 'assemble_vintf'")
    parser.add_argument("--output-dir", required=True, help="Target destination directory for the merged output files")
    args = parser.parse_args()

    try:
        merge_vintf_artifacts(
            aosp_dir=args.aosp_dir,
            vendor_dir=args.vendor_dir,
            host_bin_dir=args.host_bin_dir,
            output_dir=args.output_dir
        )
    except (FileNotFoundError, RuntimeError) as e:
        logging.error("Execution aborted: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()