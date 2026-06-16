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

    os.makedirs(output_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as temp_dir:
        manifest_temp = os.path.join(temp_dir, "manifests")
        os.makedirs(manifest_temp)

        # 1. Separate staging areas to analyze overlap profile bounds
        aosp_stage = os.path.join(temp_dir, "aosp_stage")
        vendor_stage = os.path.join(temp_dir, "vendor_stage")
        os.makedirs(aosp_stage)
        os.makedirs(vendor_stage)

        logging.info("--- Phase 1: Gathering files into temporary workspace ---")
        copy_vintf_files(aosp_dir, aosp_stage, overwrite=True)
        copy_vintf_files(vendor_dir, vendor_stage, overwrite=True)

        # 2. Extract HAL names claimed directly by the vendor package
        vendor_hal_signatures = set()
        hal_pattern = re.compile(r"<name>(android\.hardware\.[a-z0-9_\.]+)</name>")

        for file_name in os.listdir(vendor_stage):
            if file_name.endswith(".xml") and file_name != "compatibility_matrix.xml":
                try:
                    with open(os.path.join(vendor_stage, file_name), "r", encoding="utf-8") as f:
                        for match in hal_pattern.finditer(f.read()):
                            vendor_hal_signatures.add(match.group(1))
                except Exception as e:
                    logging.debug("Error pre-parsing vendor component %s: %s", file_name, e)

        logging.info("[!] Identified %d discrete HAL interfaces from vendor source.", len(vendor_hal_signatures))

        # 3. Dynamic Filtering: Drop generic AOSP files providing overridden HALs
        logging.info("Populating workspace and dropping legacy baseline overlaps...")
        for file_name in os.listdir(aosp_stage):
            aosp_file_path = os.path.join(aosp_stage, file_name)

            if file_name.endswith(".xml") and file_name not in ["manifest.xml", "compatibility_matrix.xml"]:
                try:
                    with open(aosp_file_path, "r", encoding="utf-8") as f:
                        content = f.read()

                    # If an AOSP fragment references any interface vendor has explicitly upgraded
                    if any(hal in content for hal in vendor_hal_signatures):
                        logging.info(
                            "[!] Re-hosting scrub: Dropping reference '%s' superseded by vendor hardware manifest.",
                            file_name)
                        continue  # Skip copying this file to the workspace
                except Exception as e:
                    logging.debug("Error checking AOSP file %s: %s", file_name, e)

            # Copy non-conflicting files over
            shutil.copy2(aosp_file_path, os.path.join(manifest_temp, file_name))

        # 4. Inject remaining vendor components
        logging.info("Injecting vendor-specific hardware components...")
        copy_vintf_files(vendor_stage, manifest_temp, overwrite=False)

        # 5. Fallback inline scrubbing for unified manifest.xml blocks
        base_manifest_path = os.path.join(manifest_temp, "manifest.xml")
        if os.path.exists(base_manifest_path):
            with open(base_manifest_path, "r", encoding="utf-8") as f:
                manifest_content = f.read()

            modified = False
            for target_hal in vendor_hal_signatures:
                # Surgically drop inline blocks from target reference manifest if found
                if f"<name>{target_hal}</name>" in manifest_content:
                    logging.info("Scrubbing inline '%s' block directly out of baseline manifest.xml", target_hal)
                    manifest_content = re.sub(
                        r"<hal[^>]*>\s*<name>" + re.escape(target_hal) + r"</name>.*?</hal>",
                        "", manifest_content, flags=re.DOTALL
                    )
                    modified = True

            if modified:
                with open(base_manifest_path, "w", encoding="utf-8") as f:
                    f.write(manifest_content)

        # Proceed normally into Phase 2 compilation parsing
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

            # --- Phase 4: Performing strict cross-validation check ---
            # Only cross-validate if we are checking matching relational pairs
            # (e.g., Device Manifest vs Framework Matrix).
            # Device Manifest vs Device Matrix is an architectural type violation.

            framework_matrix_path = os.path.join(aosp_dir,
                                                 "compatibility_matrix.xml")  # check for framework matrix presence

            if os.path.exists(framework_matrix_path) and "type=\"framework\"" in open(framework_matrix_path).read():
                logging.info(
                    "--- Phase 4: Performing strict cross-validation check (Device Manifest vs Framework Matrix) ---")
                run_assemble_vintf(
                    binary_path=assemble_vintf_bin,
                    base_file=final_manifest_path,
                    fragment_files=[],
                    output_file="/dev/null",
                    check_file=framework_matrix_path
                )
            else:
                logging.info("--- Phase 4: Skipping Cross-Validation ---")
                logging.info(
                    "Validation skipped: Input compatibility matrix is device-type; type-matching requires an FCM for verification.")

            logging.info("VINTF merge complete. Unified artifacts successfully exported to: %s", output_dir)
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