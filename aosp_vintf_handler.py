#!/usr/bin/env python3
import os
import sys
import shutil
import argparse
import subprocess
import tempfile
import logging

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
    Copies all files from src_dir to dest_dir.
    If overwrite is False, it skips files that already exist in dest_dir.
    """
    if not os.path.exists(src_dir):
        logging.warning("Source directory %s does not exist. Skipping.", src_dir)
        return

    for item in os.listdir(src_dir):
        src_path = os.path.join(src_dir, item)
        dest_path = os.path.join(dest_dir, item)

        if os.path.isfile(src_path):
            if not overwrite and os.path.exists(dest_path):
                logging.info("Skipping existing file to prevent overwrite: %s", item)
                continue
            shutil.copy2(src_path, dest_path)
            logging.info("Copied file: %s (Overwrite=%s)", item, overwrite)


def run_assemble_vintf(binary_path, base_file, fragment_files, output_file, check_file=None):
    """
    Executes assemble_vintf matching its precise CLI requirements.
    Ensures the base configuration file is always the first '-i' argument.
    """
    env = os.environ.copy()
    if "BOARD_SEPOLICY_VERS" not in env:
        env["BOARD_SEPOLICY_VERS"] = "10000.0"

    # Required for the -c flag to actually enforce compatibility checks
    if check_file:
        env["PRODUCT_ENFORCE_VINTF_MANIFEST"] = "true"

    cmd = [binary_path]

    # Crucial: The first -i file dictates the structural format and tags
    cmd.extend(["-i", base_file])

    # Subsequent files are treated as fragments contributing <hal> tags
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
        raise RuntimeError(
            f"assemble_vintf failed.\nCommand: {' '.join(cmd)}\n"
            f"STDOUT: {e.stdout.strip()}\nSTDERR: {e.stderr.strip()}"
        ) from e


def merge_vintf_artifacts(aosp_dir, vendor_dir, host_bin_dir, output_dir):
    """
    Programmatic wrapper to merge AOSP and Vendor VINTF manifests/matrices post-build.

    Args:
        aosp_dir (str): Path to AOSP VINTF base artifacts directory.
        vendor_dir (str): Path to extracted Vendor VINTF folder.
        host_bin_dir (str): Path to the directory containing 'assemble_vintf'.
        output_dir (str): Target destination directory for final merged files.

    Raises:
        FileNotFoundError: If the assemble_vintf executable is missing.
        RuntimeError: If VINTF generation or validation logic breaks.
    """
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

        # Separate files out into Manifest types vs Matrix types
        all_files = os.listdir(manifest_temp)
        manifest_files = [f for f in all_files if f.endswith(".xml") and "compatibility_matrix" not in f]
        matrix_files = [f for f in all_files if f.endswith(".xml") and "compatibility_matrix" in f]

        # --- PROCESS MANIFESTS ---
        if not manifest_files:
            raise RuntimeError("No XML manifests discovered inside the compiled input sources.")

        # Enforce CLI rules: Identify the main top-level base manifest file to go first
        # We try to prioritize manifest.xml if present, otherwise grab the first available
        primary_manifest = "manifest.xml" if "manifest.xml" in manifest_files else manifest_files[0]
        manifest_files.remove(primary_manifest)

        base_manifest_path = os.path.join(manifest_temp, primary_manifest)
        manifest_fragments = [os.path.join(manifest_temp, f) for f in manifest_files]

        logging.info("--- Phase 2: Processing manifests via assemble_vintf ---")
        final_manifest_path = os.path.join(output_dir, "manifest.xml")
        run_assemble_vintf(assemble_vintf_bin, base_manifest_path, manifest_fragments, final_manifest_path)

        # --- PROCESS COMPATIBILITY MATRICES ---
        if matrix_files:
            # Enforce CLI rules: Identify the primary compatibility matrix to go first
            primary_matrix = "compatibility_matrix.xml" if "compatibility_matrix.xml" in matrix_files else matrix_files[
                0]
            matrix_files.remove(primary_matrix)

            base_matrix_path = os.path.join(manifest_temp, primary_matrix)
            matrix_fragments = [os.path.join(manifest_temp, f) for f in matrix_files]

            logging.info("--- Phase 3: Processing compatibility matrices via assemble_vintf ---")
            final_matrix_path = os.path.join(output_dir, "compatibility_matrix.device.xml")
            run_assemble_vintf(assemble_vintf_bin, base_matrix_path, matrix_fragments, final_matrix_path)

            logging.info("--- Phase 4: Performing strict cross-validation check ---")
            # Uses -c flag on the generated output to check compatibility against the unified matrix
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