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
    flattened into dest_dir. Ignores system hidden files.
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
        if result.stdout:
            logging.info("assemble_vintf output: %s", result.stdout.strip())
    except subprocess.CalledProcessError as e:
        stderr_output = e.stderr.strip()

        # Auto-recover sepolicy version profile mismatch
        match = re.search(r"Cannot override existing value ([\d\.]+) with BOARD_SEPOLICY_VERS", stderr_output)
        if match and not forced_version:
            detected_version = match.group(1)
            logging.warning("Detected version restriction profile conflict. Auto-recovering using: %s",
                            detected_version)
            return run_assemble_vintf(binary_path, base_file, fragment_files, output_file, check_file,
                                      forced_version=detected_version)

        raise RuntimeError(
            f"assemble_vintf failed.\nCommand: {' '.join(cmd)}\n"
            f"STDOUT: {e.stdout.strip()}\nSTDERR: {stderr_output}"
        ) from e


def merge_vintf_artifacts(aosp_dir, vendor_dir, host_bin_dir, output_dir):
    """
    Programmatic wrapper to merge AOSP and Vendor VINTF manifests/matrices post-build.
    """
    setup_logging()

    assemble_vintf_bin = os.path.join(host_bin_dir, "assemble_vintf")
    if not os.path.isfile(assemble_vintf_bin):
        raise FileNotFoundError(f"Could not find required 'assemble_vintf' executable at: {assemble_vintf_bin}")

    # Ensure output folder safely exists for multiple runs
    os.makedirs(output_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as temp_dir:
        manifest_temp = os.path.join(temp_dir, "manifests")
        os.makedirs(manifest_temp)

        logging.info("--- Phase 1: Gathering files into temporary workspace ---")
        logging.info("Populating base structures from AOSP VINTF folder...")
        copy_vintf_files(aosp_dir, manifest_temp, overwrite=True)

        logging.info("Injecting vendor-specific components (No overwrite)...")
        copy_vintf_files(vendor_dir, manifest_temp, overwrite=False)

        # --- DYNAMIC INTERACTION SCRUBBER FOR RE-HOSTING CONFLICTS ---
        vendor_allocator = os.path.join(manifest_temp, "vendor.qti.hardware.display.allocator-service.xml")
        if os.path.exists(vendor_allocator):
            logging.info(
                "[!] Vendor QTI allocator detected. Scanning temporary workspace to scrub generic duplicates...")

            # Look through all files except the vendor specific one we just added
            for file_name in os.listdir(manifest_temp):
                if file_name == "vendor.qti.hardware.display.allocator-service.xml":
                    continue

                file_path = os.path.join(manifest_temp, file_name)
                if os.path.isfile(file_path) and file_name.endswith(".xml"):
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            content = f.read()

                        # If a baseline file contains the overlapping graphics allocator block, remove or scrub it
                        if "android.hardware.graphics.allocator" in content:
                            if file_name == "manifest.xml":
                                # If it's inside the big main manifest, strip out the specific <hal> block
                                logging.info(
                                    "Scrubbing duplicate allocator block directly out of baseline manifest.xml")
                                cleaned_content = re.sub(
                                    r"<hal[^>]*>\s*<name>android\.hardware\.graphics\.allocator</name>.*?</hal>",
                                    "", content, flags=re.DOTALL
                                )
                                with open(file_path, "w", encoding="utf-8") as f:
                                    f.write(cleaned_content)
                            else:
                                # If it's a standalone standalone fragment file, we can safely delete it entirely
                                os.remove(file_path)
                                logging.info("Removed duplicate standalone AOSP fragment: %s", file_name)
                    except Exception as e:
                        logging.debug("Could not process file %s for duplication check: %s", file_name, e)

        all_files = os.listdir(manifest_temp)
        manifest_files = [f for f in all_files if f.endswith(".xml") and "compatibility_matrix" not in f]
        matrix_files = [f for f in all_files if f.endswith(".xml") and "compatibility_matrix" in f]

        if not manifest_files:
            raise RuntimeError("No XML manifests discovered inside the compiled input sources.")

        primary_manifest = "manifest.xml" if "manifest.xml" in manifest_files else manifest_files[0]
        manifest_files.remove(primary_manifest)

        base_manifest_path = os.path.join(manifest_temp, primary_manifest)
        manifest_fragments = [os.path.join(manifest_temp, f) for f in manifest_files]

        logging.info("--- Phase 2: Processing manifests via assemble_vintf ---")
        final_manifest_path = os.path.join(output_dir, "manifest.xml")
        run_assemble_vintf(assemble_vintf_bin, base_manifest_path, manifest_fragments, final_manifest_path)

        # --- PROCESS COMPATIBILITY MATRICES ---
        if matrix_files:
            primary_matrix = "compatibility_matrix.xml" if "compatibility_matrix.xml" in matrix_files else matrix_files[
                0]
            matrix_files.remove(primary_matrix)

            base_matrix_path = os.path.join(manifest_temp, primary_matrix)
            matrix_fragments = [os.path.join(manifest_temp, f) for f in matrix_files]

            logging.info("--- Phase 3: Processing compatibility matrices via assemble_vintf ---")
            final_matrix_path = os.path.join(output_dir, "compatibility_matrix.device.xml")
            run_assemble_vintf(assemble_vintf_bin, base_matrix_path, matrix_fragments, final_matrix_path)

            logging.info("--- Phase 4: Performing strict cross-validation check ---")
            run_assemble_vintf(
                binary_path=assemble_vintf_bin,
                base_file=final_manifest_path,
                fragment_files=[],
                output_file="/dev/null",
                check_file=final_matrix_path
            )
        else:
            logging.warning("No compatibility matrices identified in input folders. Skipping verification.")

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