#!/usr/bin/env python3
"""
Parallel ACVTool report runner.

Scans emulator_out folder for firmware/packages, then spawns Docker containers
in parallel to generate coverage reports. Each container processes one package.
"""

import json
import subprocess
import sys
import argparse
import logging
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)


def find_firmware_packages(emulator_out_dir):
    """
    Scan emulator_out directory structure and extract firmware IDs and packages.

    Expected structure:
      emulator_out/
        <firmware_id>_v{sdk}_.../ (e.g., 68b0da2165e2ad36cfe19b3d_v12_sdk_...)
          acv_snaps/
            <package_name>/
              ec_files/
                coverage_*.ec

    Returns:
        dict: {firmware_id: [package_names]}
    """
    firmware_packages = defaultdict(list)
    emulator_path = Path(emulator_out_dir).resolve()

    if not emulator_path.exists():
        logger.error(f"emulator_out directory not found: {emulator_path}")
        return firmware_packages

    for fw_folder in emulator_path.iterdir():
        if not fw_folder.is_dir():
            continue

        firmware_id = fw_folder.name
        acv_snaps = fw_folder / "acv_snaps"

        if not acv_snaps.exists():
            continue

        for pkg_dir in acv_snaps.iterdir():
            if pkg_dir.name in ["pickle_files"] or not pkg_dir.is_dir():
                continue

            ec_files_dir = pkg_dir / "ec_files"
            if not ec_files_dir.exists():
                continue

            ec_files = list(ec_files_dir.glob("coverage_*.ec"))
            if ec_files:
                firmware_packages[firmware_id].append(pkg_dir.name)

    return firmware_packages


def run_container(
    firmware_id,
    package_name,
    emulator_out_host,
    image_name,
    dry_run=False,
    container_prefix="acv-report"
):
    """
    Run a single Docker container for a firmware/package pair.

    This matches the notebook workflow:
    1. Mounts emulator_out to /work/input
    2. Sets ACV_WD to point to a working directory within the package
    3. Expects pickle files and EC files to be staged before container runs
    4. Container entrypoint creates subdirectories and runs acv report

    Args:
        firmware_id: Firmware folder name
        package_name: Package to process
        emulator_out_host: Host path to emulator_out directory
        image_name: Docker image name:tag
        dry_run: If True, print command instead of executing
        container_prefix: Prefix for container names

    Returns:
        dict: Result with status, container_id, package_name, etc.
    """
    container_name = f"{container_prefix}-{firmware_id[:8]}-{package_name[:12]}"

    # Paths inside container and on host
    input_dir = "/work/input"
    package_acv_snaps = Path(emulator_out_host) / firmware_id / "acv_snaps" / package_name
    avc_wd_host = package_acv_snaps / ".acv_wd"
    avc_wd_container = f"{input_dir}/{firmware_id}/acv_snaps/{package_name}/.acv_wd"

    # Ensure working directory exists on host
    avc_wd_host.mkdir(parents=True, exist_ok=True)

    # Create required subdirectories
    pickles_dir = avc_wd_host / "pickles"
    ec_dir = avc_wd_host / "ec"
    report_dir = avc_wd_host / "report"
    pickles_dir.mkdir(exist_ok=True)
    ec_dir.mkdir(exist_ok=True)

    # If previous runs left covered_pickles or report directories, remove them
    # to ensure a clean working directory before staging files. Only perform
    # destructive cleanup when not in dry-run mode.
    if not dry_run:
        covered_pickles_dir = avc_wd_host / "covered_pickles"
        if covered_pickles_dir.exists():
            try:
                logger.info(f"Cleaning existing covered_pickles at {covered_pickles_dir}")
                shutil.rmtree(covered_pickles_dir)
            except Exception:
                logger.warning(f"Failed to remove {covered_pickles_dir}, continuing")

        if report_dir.exists():
            try:
                logger.info(f"Cleaning existing report directory at {report_dir}")
                shutil.rmtree(report_dir)
            except Exception:
                logger.warning(f"Failed to remove {report_dir}, continuing")

        report_dir.mkdir(exist_ok=True)

    # Stage EC files from package directory
    ec_files_src = package_acv_snaps / "ec_files"
    ec_count = 0
    pickle_count = 0

    if ec_files_src.exists() and ec_files_src.is_dir():
        for ec_file in ec_files_src.glob("coverage_*.ec"):
            shutil.copy2(ec_file, ec_dir)
            ec_count += 1

    # Stage package-matching pickles from <firmware>/acv_snaps/pickle_files.
    pickle_src_root = package_acv_snaps.parent / "pickle_files"
    pickles_staged = 0
    if pickle_src_root.exists() and pickle_src_root.is_dir():
        for pickle_file in pickle_src_root.rglob("*.pickle"):
            stem = pickle_file.stem
            normalized = stem
            if "_" in stem and stem.rsplit("_", 1)[-1].isdigit():
                normalized = stem.rsplit("_", 1)[0]
            if normalized == package_name:
                shutil.copy2(pickle_file, pickles_dir / pickle_file.name)
                pickles_staged += 1

    # Check for pickles after staging
    pickle_files = list(pickles_dir.glob("*.pickle"))
    pickle_count = len(pickle_files)

    cmd = [
        "docker", "run",
        "--rm",
        "--name", container_name,
        "-v", f"{emulator_out_host}:{input_dir}",
        "-e", f"ACV_WD={avc_wd_container}",
        image_name,
        "report", package_name
    ]

    result = {
        "firmware_id": firmware_id,
        "package_name": package_name,
        "container_name": container_name,
        "working_dir_host": str(avc_wd_host),
        "working_dir_container": avc_wd_container,
        "ec_files_staged": ec_count,
        "pickle_files_staged": pickles_staged,
        "pickle_files_present": pickle_count,
        "command": " ".join(cmd),
        "status": "pending",
        "returncode": None,
        "stdout": "",
        "stderr": ""
    }

    if dry_run:
        logger.info(f"[DRY-RUN] {container_name}")
        logger.info(f"  Working Dir (host):      {avc_wd_host}")
        logger.info(f"  Working Dir (container): {avc_wd_container}")
        logger.info(f"  EC files staged:         {ec_count}")
        logger.info(f"  Pickle files staged:     {pickles_staged}")
        logger.info(f"  Pickle files present:    {pickle_count}")
        if pickle_count == 0:
            logger.warning(f"    ⚠️  No pickle files found! Report may fail.")
        result["status"] = "dry_run"
        return result

    logger.info(f"Starting container: {container_name}")
    logger.info(f"  Package: {package_name}")
    logger.info(f"  Working Dir (host): {avc_wd_host}")
    logger.info(f"  EC files staged: {ec_count}")
    logger.info(f"  Pickle files staged: {pickles_staged}")
    logger.info(f"  Pickle files present: {pickle_count}")
    if pickle_count == 0:
        logger.warning(f"    ⚠️  No pickle files found! Report may fail. Manually stage pickles to: {pickles_dir}")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600  # 1 hour timeout per container
        )
        result["status"] = "completed" if proc.returncode == 0 else "failed"
        result["returncode"] = proc.returncode
        result["stdout"] = proc.stdout[-500:] if proc.stdout else ""  # Last 500 chars
        result["stderr"] = proc.stderr[-500:] if proc.stderr else ""

        if result["status"] == "completed":
            logger.info(f"✓ {container_name} completed successfully")
        else:
            logger.warning(f"✗ {container_name} failed with code {proc.returncode}")

    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["stderr"] = "Container execution exceeded 1 hour timeout"
        logger.warning(f"✗ {container_name} timeout")

    except Exception as e:
        result["status"] = "error"
        result["stderr"] = str(e)
        logger.error(f"✗ {container_name} error: {e}")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Run parallel ACVTool report generation via Docker"
    )
    parser.add_argument(
        "--emulator-out",
        default="./data/01_journal_extension/emulator_out",
        help="Path to emulator_out directory (default: %(default)s)"
    )
    parser.add_argument(
        "--image",
        default="acvtool-report:2.3.6",
        help="Docker image name:tag (default: %(default)s)"
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=4,
        help="Maximum parallel containers (default: %(default)s)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without executing"
    )
    parser.add_argument(
        "--output",
        help="Save results as JSON to file"
    )

    args = parser.parse_args()

    # Resolve paths
    emulator_out = Path(args.emulator_out).resolve()

    logger.info(f"Scanning {emulator_out} for firmware/packages...")
    firmware_packages = find_firmware_packages(emulator_out)

    if not firmware_packages:
        logger.error("No firmware packages found. Exiting.")
        return 1

    # Build list of tasks
    tasks = []
    for firmware_id, packages in sorted(firmware_packages.items()):
        for package in sorted(packages):
            tasks.append((firmware_id, package))

    logger.info(f"Found {len(tasks)} tasks across {len(firmware_packages)} firmware(s)")
    for fw, pkgs in sorted(firmware_packages.items()):
        logger.info(f"  {fw}: {len(pkgs)} package(s)")

    if args.dry_run:
        logger.info("DRY-RUN mode enabled")

    # Run in parallel
    results = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = {
            executor.submit(
                run_container,
                fw_id,
                pkg,
                str(emulator_out),
                args.image,
                args.dry_run
            ): (fw_id, pkg)
            for fw_id, pkg in tasks
        }

        for future in as_completed(futures):
            result = future.result()
            results.append(result)

    # Summary
    logger.info("\n" + "="*70)
    logger.info("SUMMARY")
    logger.info("="*70)

    statuses = defaultdict(int)
    for r in results:
        statuses[r["status"]] += 1

    for status, count in sorted(statuses.items()):
        logger.info(f"  {status}: {count}")

    failed_results = [r for r in results if r["status"] not in ["completed", "dry_run"]]
    if failed_results:
        logger.warning("\nFailed/errored tasks:")
        for r in failed_results:
            logger.warning(
                f"  {r['firmware_id']}/{r['package_name']}: {r['status']}"
            )
            if r["stderr"]:
                logger.warning(f"    Error: {r['stderr']}")

    # Save results if requested
    if args.output:
        output_path = Path(args.output)
        with open(output_path, 'w') as f:
            json.dump(results, f, indent=2)
        logger.info(f"\nResults saved to {output_path}")

    # Exit code: 0 if all completed, else 1
    exit_code = 0 if all(r["status"] in ["completed", "dry_run"] for r in results) else 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())


