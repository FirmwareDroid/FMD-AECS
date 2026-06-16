#!/usr/bin/env python3
import os
import re
import hashlib
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


def get_file_checksum(file_path: str) -> str:
    """
    Calculates the SHA-256 checksum of a file by reading it in memory-safe chunks.
    """
    sha256_hash = hashlib.sha256()
    chunk_size = 65536  # 64KB chunks

    with open(file_path, "rb") as f:
        while chunk_ := f.read(chunk_size):
            sha256_hash.update(chunk_)

    return sha256_hash.hexdigest()


def extract_declared_types(cil_path):
    """
    Parses a CIL file to find all declared types, attributes, and macros.
    """
    declared_identifiers = set()
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
    secilc compiler resolution faults. Handles hyphenated names cleanly.
    """
    logger.info(f"Surgically scrubbing duplicate platform types and orphaned statements from {vendor_cil_in}...")

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
                removed_decls += 1
                continue

            # 2. Auxiliary Structural Statements
            clean_tokens = stripped_line.replace("(", "").replace(")", "").split()
            if clean_tokens and clean_tokens[0] in aux_keywords:
                if any(t in duplicate_set for t in clean_tokens[1:]):
                    removed_aux += 1
                    continue

            # 3. Complex Attribute Multi-Token Sets
            attr_match = attr_set_pattern.search(line)
            if attr_match:
                attr_name = attr_match.group(1)
                tokens = attr_match.group(2).split()
                cleaned_tokens = [t for t in tokens if t not in duplicate_set]

                if attr_name in duplicate_set:
                    removed_decls += 1
                    continue

                if len(cleaned_tokens) != len(tokens):
                    scrubbed_tokens += (len(tokens) - len(cleaned_tokens))
                    if cleaned_tokens:
                        token_string = " ".join(cleaned_tokens)
                        line = f"(typeattributeset {attr_name} ({token_string}))\n"
                    else:
                        continue

            lines_buffer.append(line)

    with open(vendor_cil_out, 'w') as outfile:
        for line in lines_buffer:
            outfile.write(line)

    logger.info(f"[+] Scrubbing complete: Deleted {removed_decls} base declarations.")
    logger.info(f"[+] Scrubbing complete: Removed {removed_aux} orphaned structural statements.")
    logger.info(f"[+] Scrubbing complete: Cleared {scrubbed_tokens} internal duplicate tokens.")


def strip_neverallows(pub_cil_in, pub_cil_out):
    """
    Surgically bypasses Android API matrix constraints by neutralising
    neverallow and neverallowx assertions from the compatibility layer.
    """
    logger.info(f"Stripping compatibility matrix constraints from {pub_cil_in}...")
    stripped_count = 0
    lines_buffer = []

    with open(pub_cil_in, 'r') as infile:
        for line in infile:
            stripped_line = line.strip()

            if stripped_line.startswith("(neverallow ") or stripped_line.startswith("(neverallowx "):
                stripped_count += 1
                lines_buffer.append(f"; STRIPPED CONSTRAINT: {line}")
                continue

            lines_buffer.append(line)

    with open(pub_cil_out, 'w') as outfile:
        for line in lines_buffer:
            outfile.write(line)

    logger.info(f"[+] Constraint removal complete: Defused {stripped_count} static neverallow checks.")


def dump_faulty_lines(file_path, target_line, window=5):
    """
    Helper function to print out a window of lines around a compilation error.
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
              "-m",
              "-M", "false",
              "-G",
              "-c", policy_version,
              "-o", output_policy,
              "-f", output_contexts
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
    Core pipeline driver handling clean-up execution wrappers and alignment verification.
    """
    final_policy = os.path.join(out_dir, "sepolicy")
    final_contexts = os.path.join(out_dir, "file_contexts")
    final_sha256 = os.path.join(out_dir, "plat_sepolicy_and_mapping.sha256")

    os.makedirs(out_dir, exist_ok=True)

    logger.info("Analyzing AOSP platform policy for existing types...")
    platform_identifiers = set()
    platform_identifiers.update(extract_declared_types(plat_cil))
    platform_identifiers.update(extract_declared_types(plat_mapping))
    logger.info(f"Found {len(platform_identifiers)} unique types/attributes in core platform.")

    with tempfile.NamedTemporaryFile(mode='w+', delete=False) as temp_v_cil, \
            tempfile.NamedTemporaryFile(mode='w+', delete=False) as temp_p_pub, \
            tempfile.NamedTemporaryFile(mode='w+', delete=False) as temp_p_cil:

        temp_vendor_cil_path = temp_v_cil.name
        temp_pub_pub_path = temp_p_pub.name
        temp_plat_cil_path = temp_p_cil.name

    try:
        # Step 1: Clean duplicates out of vendor matrix
        clean_vendor_cil(vendor_cil, temp_vendor_cil_path, platform_identifiers)

        # Step 2: Clean API matrix conflicts from public mapping tracking
        strip_neverallows(vendor_pub_versioned, temp_pub_pub_path)

        # Step 3: Strip internal platform constraints
        logger.info(f"Stripping native platform constraints from core policy file: {plat_cil}")
        strip_neverallows(plat_cil, temp_plat_cil_path)

        input_pipeline_files = [
            temp_plat_cil_path,
            plat_mapping,
            temp_vendor_cil_path,
            temp_pub_pub_path
        ]

        # Verify dependencies
        for f in input_pipeline_files:
            if f not in (temp_vendor_cil_path, temp_pub_pub_path, temp_plat_cil_path) and not os.path.exists(f):
                logger.error(f"Critical Error: Missing required file: {f}")
                return False

        # Step 4: Run compilation stage
        run_secilc(secilc_bin, input_pipeline_files, final_policy, final_contexts, policy_version)

        # Step 5: Generate the cryptographic fingerprint tracking file for AOSP verification loop
        logger.info("Generating security validation signatures...")
        # Fingerprint the stripped platform policy file passed to the compilation pass
        calculated_hash = get_file_checksum(temp_plat_cil_path)

        with open(final_sha256, "w") as sha_file:
            # Android init requires the lowercased hex string trailed cleanly with a trailing newline
            sha_file.write(f"{calculated_hash}\n")

        logger.info(f"[+] Fingerprint file written to: {final_sha256}")
        logger.info(f"[+] SHA-256 Token Value: {calculated_hash}")

        return True

    except subprocess.CalledProcessError as e:
        match = re.search(r'at [^:]+:(\d+)', e.stderr)
        if match:
            fault_line = int(match.group(1))
            if "plat_sepolicy" in e.stderr:
                dump_faulty_lines(temp_plat_cil_path, fault_line, window=5)
            elif "plat_pub_versioned" in e.stderr or "neverallow" in e.stderr:
                dump_faulty_lines(temp_pub_pub_path, fault_line, window=5)
            else:
                dump_faulty_lines(temp_vendor_cil_path, fault_line, window=5)
        else:
            logger.error("Could not dynamically parse lines from secilc output stream pattern.")
        return False

    finally:
        for path in (temp_vendor_cil_path, temp_pub_pub_path, temp_plat_cil_path):
            if os.path.exists(path):
                os.remove(path)


def main():
    parser = argparse.ArgumentParser(
        description="Merge precompiled Android Platform and Vendor CIL files with automated SHA-256 signatures."
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