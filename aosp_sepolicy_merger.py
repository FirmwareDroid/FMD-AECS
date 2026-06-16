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
    # CHANGED: Added '-' to character class
    pattern = re.compile(r'\((type|typeattribute|macro|common|class)\s+([a-zA-Z0-9_-]+)')

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
    Cleans vendor policies inline with enhanced logging for tracking down
    secilc compiler resolution faults.
    """
    logger.info(f"Surgically scrubbing duplicate platform types and orphaned statements from {vendor_cil_in}...")

    # CHANGED: Added '-' to character classes across all compiler patterns
    decl_pattern = re.compile(r'\((type|typeattribute|macro|common|class)\s+([a-zA-Z0-9_-]+)')
    attr_set_pattern = re.compile(r'\(typeattributeset\s+([a-zA-Z0-9_-]+)\s+\((.*)\)\)')
    aux_keywords = ("roletype", "typeattribute", "typebounds")

    removed_decls = 0
    scrubbed_tokens = 0
    removed_aux = 0

    lines_buffer = []

    with open(vendor_cil_in, 'r') as infile:
        for line_num, line in enumerate(infile, 1):
            stripped_line = line.strip()

            if not stripped_line or stripped_line.startswith(";"):
                lines_buffer.append(line)
                continue

            # 1. Base Duplicate Declarations
            decl_match = decl_pattern.search(line)
            if decl_match and decl_match.group(2) in duplicate_set:
                logger.debug(f"[Line {line_num}] Dropping duplicate declaration: {decl_match.group(2)}")
                removed_decls += 1
                continue

            # 2. Auxiliary Structural Statements (Now handles hyphens safely via split strings)
            clean_tokens = stripped_line.replace("(", "").replace(")", "").split()
            if clean_tokens and clean_tokens[0] in aux_keywords:
                if any(t in duplicate_set for t in clean_tokens[1:]):
                    logger.debug(f"[Line {line_num}] Dropping orphaned aux block ({clean_tokens[0]}): {stripped_line}")
                    removed_aux += 1
                    continue

            # 3. Complex Attribute Multi-Token Sets
            attr_match = attr_set_pattern.search(line)
            if attr_match:
                attr_name = attr_match.group(1)
                tokens = attr_match.group(2).split()
                cleaned_tokens = [t for t in tokens if t not in duplicate_set]

                if attr_name in duplicate_set:
                    logger.debug(f"[Line {line_num}] Dropping duplicate typeattributeset core name: {attr_name}")
                    removed_decls += 1
                    continue

                if len(cleaned_tokens) != len(tokens):
                    scrubbed_tokens += (len(tokens) - len(cleaned_tokens))
                    if cleaned_tokens:
                        token_string = " ".join(cleaned_tokens)
                        line = f"(typeattributeset {attr_name} ({token_string}))\n"
                        logger.debug(f"[Line {line_num}] Scrubbed internal duplicate tokens for: {attr_name}")
                    else:
                        logger.debug(f"[Line {line_num}] Dropping empty typeattributeset sequence for: {attr_name}")
                        continue

            lines_buffer.append(line)

    with open(vendor_cil_out, 'w') as outfile:
        for line in lines_buffer:
            outfile.write(line)

    logger.info(f"[+] Scrubbing complete: Deleted {removed_decls} base declarations.")
    logger.info(f"[+] Scrubbing complete: Removed {removed_aux} orphaned structural statements.")
    logger.info(f"[+] Scrubbing complete: Cleared {scrubbed_tokens} internal duplicate tokens.")


def dump_faulty_lines(file_path, target_line, window=5):
    """
    Helper function to print out a window of lines around a compilation error
    to inspect context formatting directly.
    """
    logger.error(f"--- INSPECTING CONTEXT AROUND FAULTY LINE {target_line} ---")
    if not os.path.exists(file_path):
        logger.error("Parsed temporary file could not be recovered for analysis.")
        return

    start = max(1, target_line - window)
    end = target_line + window

    with open(file_path, 'r') as f:
        for idx, line in enumerate(f, 1):
            if start <= idx <= end:
                marker = ">>>" if idx == target_line else "   "
                logger.error(f"{marker} [{idx}]: {line.rstrip()}")
    logger.error("-" * 60)


def run_secilc(secilc_bin, input_files, output_policy, output_contexts, policy_version="33"):
    """
    Invokes the secilc compiler matching your AOSP 13 tool requirements.
    """
    logger.info("Invoking secilc compiler...")

    cmd = [
              secilc_bin,
              "-m",  # --multiple-decls
              "-M", "false",  # --mls true|false
              "-G",  # --expand-generated
              "-c", policy_version,  # --policyvers
              "-o", output_policy,  # --output
              "-f", output_contexts  # --filecontext
          ] + input_files

    logger.info(f"Running command: {' '.join(cmd)}")
    try:
        res = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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

    except subprocess.CalledProcessError as e:
        # Match error patterns like: "Failed to resolve roletype statement at /tmp/tmptdk4g7tg:1669"
        match = re.search(r'statement at [^:]+:(\d+)', e.stderr)
        if match:
            fault_line = int(match.group(1))
            dump_faulty_lines(temp_vendor_cil_path, fault_line, window=5)
        else:
            logger.error("Could not dynamically parse lines from secilc output stream pattern.")
        return False

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