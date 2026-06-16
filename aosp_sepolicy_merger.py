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
    Cleans vendor policies inline. Strips duplicate declarations completely,
    but surgically removes individual conflicting tokens from attribute sets
    to preserve distinct vendor-specific types.
    """
    logger.info(f"Surgically scrubbing duplicate platform types from {vendor_cil_in}...")

    decl_pattern = re.compile(r'\((type|typeattribute|macro|common|class)\s+([a-zA-Z0-9_]+)')
    attr_set_pattern = re.compile(r'\(typeattributeset\s+([a-zA-Z0-9_]+)\s+\((.*)\)\)')

    removed_decls = 0
    scrubbed_tokens = 0

    with open(vendor_cil_in, 'r') as infile, open(vendor_cil_out, 'w') as outfile:
        for line in infile:
            # Check 1: Base Duplicate Declarations (Delete whole line)
            decl_match = decl_pattern.search(line)
            if decl_match and decl_match.group(2) in duplicate_set:
                removed_decls += 1
                continue

            # Check 2: Complex Attribute Sets (Scrub individual items within the list)
            attr_match = attr_set_pattern.search(line)
            if attr_match:
                attr_name = attr_match.group(1)
                tokens = attr_match.group(2).split()

                # Filter out the blacklisted duplicate tokens
                cleaned_tokens = [t for t in tokens if t not in duplicate_set]

                # If the attribute block itself is a duplicate platform name, skip it
                if attr_name in duplicate_set:
                    removed_decls += 1
                    continue

                if len(cleaned_tokens) != len(tokens):
                    scrubbed_tokens += (len(tokens) - len(cleaned_tokens))
                    if cleaned_tokens:
                        # Rebuild the line with remaining valid hardware types
                        token_string = " ".join(cleaned_tokens)
                        line = f"(typeattributeset {attr_name} ({token_string}))\n"
                    else:
                        # Skip writing if no elements are left in the group
                        continue

            outfile.write(line)

    logger.info(f"[+] Scrubbing complete: Deleted {removed_decls} base declarations.")
    logger.info(f"[+] Scrubbing complete: Cleared {scrubbed_tokens} internal duplicate tokens.")


def run_secilc(secilc_bin, input_files, output_policy, output_contexts, policy_version="33"):
    """
    Invokes the secilc compiler matching your AOSP 13 tool requirements.
    """
    logger.info("Invoking secilc compiler...")

    # Matching your exact binary options format
    cmd = [
              secilc_bin,
              "-m",  # --multiple-decls
              "-M", "false",  # --mls true|false (Android uses non-MLS profiles by default)
              "-G",  # --expand-generated
              "-c", policy_version,  # --policyvers=<version>
              "-o", output_policy,  # --output=<file>
              "-f", output_contexts  # --filecontext=<file>
          ] + input_files

    logger.info(f"Running command: {' '.join(cmd)}")
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
                            policy_version="33"):
    """
    Core pipeline driver accepting dynamic arguments from your environment.
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
    CLI Parser matching the precise requirements of your AOSP workspace setup.
    """
    parser = argparse.ArgumentParser(
        description="Merge precompiled Android Platform and Vendor CIL files matching your specific secilc profile."
    )

    parser.add_argument("--secilc", required=True, help="Path to host secilc binary")
    parser.add_argument("--plat-cil", required=True, help="Path to platform plat_sepolicy.cil")
    parser.add_argument("--plat-mapping", required=True, help="Path to platform mapping CIL file (e.g., 33.cil)")
    parser.add_argument("--vendor-cil", required=True, help="Path to raw vendor_sepolicy.cil")
    parser.add_argument("--vendor-pub", required=True, help="Path to vendor plat_pub_versioned.cil")
    parser.add_argument("--out-dir", required=True, help="Output directory for merged artifacts")

    # Defaulting to 33 per your tool's default parameters
    parser.add_argument("--policy-version", default="33", help="Target SELinux database version (default: 33)")
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