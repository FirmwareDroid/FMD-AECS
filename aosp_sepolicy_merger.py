#!/usr/bin/env python3
import os
import re
import subprocess
import sys
import tempfile
import logging
import argparse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("sepolicy_merger")


def extract_declared_types(cil_path):
    """
    Parses a CIL file to find all declared types, attributes, and macros.
    """
    declared_identifiers = set()
    pattern = re.compile(r'\((type|typeattribute|macro|common|class)\s+([a-zA-Z0-9_]+)')

    if not os.path.exists(cil_path):
        logger.warning(f"Reference file not found: {cil_path}")
        return declared_identifiers

    with open(cil_path, 'r') as f:
        for line in f:
            match = pattern.search(line)
            if match:
                declared_identifiers.add(match.group(2))

    return declared_identifiers


def clean_vendor_cil(vendor_cil_in, vendor_cil_out, duplicate_set):
    """
    Filters out any line containing a declaration of an identifier already present in the platform policy.
    """
    logger.info(f"Stripping duplicate platform declarations from {vendor_cil_in}...")
    pattern = re.compile(r'\((type|typeattribute|macro|common|class)\s+([a-zA-Z0-9_]+)')
    removed_count = 0

    with open(vendor_cil_in, 'r') as infile, open(vendor_cil_out, 'w') as outfile:
        for line in infile:
            match = pattern.search(line)
            if match and match.group(2) in duplicate_set:
                removed_count += 1
                continue
            outfile.write(line)

    logger.info(f"Successfully stripped {removed_count} duplicate declarations.")


def run_secilc(secilc_bin, input_files, output_policy, output_contexts, policy_version="30"):
    """
    Invokes the secilc compiler with the cleaned pipeline files.
    """
    logger.info("Invoking secilc compiler...")
    cmd = [
              secilc_bin,
              "-m",  # Allow multiple declarations
              "-M", "true",  # Target platform policy optimization
              "-G",  # Expand auto-generated attributes
              "-c", policy_version,
              "-o", output_policy,
              "-f", output_contexts
          ] + input_files

    logger.debug(f"Running command: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        logger.info("Success! Merged policy generated.")
        logger.info(f"Binary Policy: {output_policy}")
        logger.info(f"File Contexts: {output_contexts}")
    except subprocess.CalledProcessError as e:
        logger.error("secilc compilation failed!")
        logger.error(e.stderr)
        raise e


def merge_sepolicy_pipeline(secilc_bin, plat_cil, plat_mapping, vendor_cil, vendor_pub_versioned, out_dir,
                            policy_version="30"):
    """
    Core wrapper function that accepts paths dynamically.
    This can be safely called directly from other Python automation scripts.
    """
    final_policy = os.path.join(out_dir, "sepolicy")
    final_contexts = os.path.join(out_dir, "file_contexts")

    os.makedirs(out_dir, exist_ok=True)

    logger.info("Analyzing AOSP platform policy for existing types...")
    platform_identifiers = set()
    platform_identifiers.update(extract_declared_types(plat_cil))
    platform_identifiers.update(extract_declared_types(plat_mapping))
    logger.info(f"Found {len(platform_identifiers)} unique types/attributes in core platform.")

    with tempfile.NamedTemporaryFile(mode='w+', delete=False) as temp_vendor_cil:
        temp_vendor_cil_path = temp_vendor_cil.name

    try:
        clean_vendor_cil(vendor_cil, temp_vendor_cil_path, platform_identifiers)

        input_pipeline_files = [
            plat_cil,
            plat_mapping,
            temp_vendor_cil_path,
            vendor_pub_versioned
        ]

        # Verify dependencies
        for f in input_pipeline_files:
            if f != temp_vendor_cil_path and not os.path.exists(f):
                logger.error(f"Critical Error: Missing required file: {f}")
                return False

        run_secilc(secilc_bin, input_pipeline_files, final_policy, final_contexts, policy_version)
        return True

    finally:
        if os.path.exists(temp_vendor_cil_path):
            os.remove(temp_vendor_cil_path)


def main():
    """
    CLI Parser handling execution via arguments.
    """
    parser = argparse.ArgumentParser(
        description="Merge precompiled Android Platform and Vendor CIL files for Rehosting dynamically."
    )

    # Arguments mapped directly to paths
    parser.add_argument("--secilc", required=True, help="Path to host secilc binary")
    parser.add_argument("--plat-cil", required=True, help="Path to platform plat_sepolicy.cil")
    parser.add_argument("--plat-mapping", required=True, help="Path to platform mapping CIL file (e.g., 33.cil)")
    parser.add_argument("--vendor-cil", required=True, help="Path to raw vendor_sepolicy.cil")
    parser.add_argument("--vendor-pub", required=True, help="Path to vendor plat_pub_versioned.cil")
    parser.add_argument("--out-dir", required=True, help="Output directory for merged artifacts")

    parser.add_argument("--policy-version", default="30", help="Target SELinux database version (default: 30)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging output")

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    success = merge_sepolicy_pipeline(
        secilc_bin=args.secilc,
        plat_cil=args.plat_cil,
        plat_mapping=args.plat_mapping,
        vendor_cil=args.vendor_cil,
        vendor_pub_versioned=args.vendor_pub,
        out_dir=args.out_dir,
        policy_version=args.policy_version
    )

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()