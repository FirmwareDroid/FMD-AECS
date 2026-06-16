#!/usr/bin/env python3
import os
import sys
import shutil
import argparse
import subprocess
import tempfile
import logging
import re


def setup_logging():
    """Configures the global logging format if not already configured."""
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(message)s',
            handlers=[logging.StreamHandler(sys.stdout)]
        )


def copy_vintf_files(src_dir, dest_dir, overwrite=True):
    """
    Recursively discovers and copies all XML files from src_dir (including subdirs)
    flattened into dest_dir. Ignores metadata files like .DS_Store.
    """
    if not os.path.exists(src_dir):
        logging.warning("Source directory %s does not exist. Skipping.", src_dir)
        return

    for root, _, files in os.walk(src_dir):
        for file in files:
            if file.startswith('.'):  # Skip hidden system files like .DS_Store
                continue
            if file.endswith(".xml"):
                src_path = os.path.join(root, file)
                dest_path = os.path.join(dest_dir, file)

                if not overwrite and os.path.exists(dest_path):
                    logging.info("Skipping existing file to prevent overwrite: %s", file)
                    continue
                shutil.copy2(src_path, dest_path)
                logging.info("Copied file: %s (Overwrite=%s)", file, overwrite)


def run_assemble_vintf(binary_path, base_file, fragment_files, output_file, check_file=None, forced_version=None):
    """
    Executes assemble_vintf matching its precise CLI requirements.
    Dynamically falls back to target versions if conflicts arise.
    """
    env = os.environ.copy()

    # Use the explicitly requested version if available, otherwise fallback to mock default
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

    logging.info("Executing command: %s (BOARD_SEPOLICY_VERS=%s)", ' '.join(cmd), env["BOARD_SEPOLICY_VERS"])
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
        logging.info("Successfully generated: %s", os.path.basename(output_file))
        if result.stdout:
            logging.info("assemble_vintf output: %s", result.stdout.strip())
    except subprocess.CalledProcessError as e:
        stderr_output = e.stderr.strip()

        # Detect if assemble_vintf rejected the mock 10000.0 value due to pre-existing profile constraints
        match = re.search(r"Cannot override existing value ([\d\.]+) with BOARD_SEPOLICY_VERS", stderr_output)
        if match and not forced_version:
            detected_version = match.group(1)
            logging.warning("Detected version restriction profile conflict. Auto-recovering using: %s",
                            detected_version)

            # Recursive self-healing retry call with the targeted profile version
            return run_assemble_vintf(
                binary_path=binary_path,
                base_file=base_file,
                fragment_files=fragment_files,
                output_file=output_file,
                check_file=check_file,
                forced_version=detected_version
            )

        raise RuntimeError(
            f"assemble_vintf failed.\nCommand: {' '.join(cmd)}\n"
            f"STDOUT: {e.stdout.strip()}\nSTDERR: {stderr_output}"
        ) from e


def merge_vintf_artifacts(aosp_dir, vendor_dir, host_bin_dir, output_dir):
    setup_logging()

    assemble_vintf_bin = os.path.join(host_bin_dir, "assemble_vintf")
    if not os.path.isfile(assemble_vintf_bin):
        raise FileNotFoundError(f"Could not find required 'assemble_vintf' executable at: {assemble_vintf_bin}")

    os.makedirs(output_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as temp_dir:
        manifest_temp = os.path.join(temp_dir, "manifests")
        os.makedirs(manifest_temp)

        logging.info("--- Phase 1: Gathering files into temporary workspace ---")
        logging.info("Populating base structures from AOSP VINTF folder...")
        copy_vintf_files(aosp_dir, manifest_temp, overwrite=True)

        logging.info("Injecting vendor-specific components (No overwrite)...")
        copy_vintf_files(vendor_dir, manifest_temp, overwrite=False)

        # --- RE-HOSTING INTERACTION FIX: SCRUB ARCHITECTURAL REFERENCE COLLISONS ---
        # If vendor display allocator is present, remove the conflicting generic emulator allocator
        vendor_allocator = os.path.join(manifest_temp, "vendor.qti.hardware.display.allocator-service.xml")
        aosp_allocator_remnants = [
            os.path.join(manifest_temp, "android.hardware.graphics.allocator@3.0-service.xml"),
            # Common reference location
        ]

        if os.path.exists(vendor_allocator):
            logging.info("[!] Re-hosting adjustment: Vendor QTI allocator detected. Purging duplicate references...")
            for aosp_file in aosp_allocator_remnants:
                if os.path.exists(aosp_file):
                    os.remove(aosp_file)
                    logging.info("Cleaned up redundant reference: %s", os.path.basename(aosp_file))

        all_files = os.listdir(manifest_temp)
        manifest_files = [f for f in all_files if f.endswith(".xml") and "compatibility_matrix" not in f]
        matrix_files = [f for f in all_files if f.endswith(".xml") and "compatibility_matrix" in f]

        # --- PROCESS MANIFESTS ---
        if not manifest_files:
            raise RuntimeError("No XML manifests discovered inside the compiled input sources.")

        primary_manifest = "manifest.xml" if "manifest.xml" in manifest_files else manifest_files[0]
        manifest_files.remove(primary_manifest)


def main():
    """Handles standard Command Line Interface (CLI) execution."""
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