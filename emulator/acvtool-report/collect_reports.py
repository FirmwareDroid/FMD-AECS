#!/usr/bin/env python3
"""Collect ACVTool reports from per-package working directories to firmware-level acv_reports.

Structure:
  Before:  emulator_out/<firmware>/acv_snaps/<package>/.acv_wd/report/
  After:   emulator_out/<firmware>/acv_reports/<package>/
"""

from __future__ import annotations

import argparse
import logging
import shutil
import os
from pathlib import Path
import concurrent.futures

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent

def collect_reports_for_firmware(fw_dir: Path, skip_existing: bool = False) -> tuple[int, int]:
    """Collect reports from all packages in a firmware folder.

    Args:
        fw_dir: Firmware directory
        skip_existing: If True, don't overwrite existing reports in acv_reports

    Returns:
        (copied_count, skipped_count)
    """
    fw_id = fw_dir.name
    acv_snaps = fw_dir / "acv_snaps"
    acv_reports_root = fw_dir / "acv_reports"

    if not acv_snaps.exists():
        return 0, 0

    copied_count = 0
    skipped_count = 0

    for pkg_dir in acv_snaps.iterdir():
        if not pkg_dir.is_dir() or pkg_dir.name == "pickle_files":
            continue

        pkg_name = pkg_dir.name
        src_report_dir = pkg_dir / ".acv_wd" / "report"
        dest_report_dir = acv_reports_root / pkg_name

        if not src_report_dir.exists():
            continue

        if dest_report_dir.exists() and skip_existing:
            skipped_count += 1
            continue

        acv_reports_root.mkdir(parents=True, exist_ok=True)

        if dest_report_dir.exists() and dest_report_dir.is_dir() and str(BASE_DIR) in dest_report_dir.resolve().as_posix():
            try:
                logger.info(f"Deleting {dest_report_dir}")
                shutil.rmtree(dest_report_dir)
            except Exception as e:
                logger.error(f"Deleting {dest_report_dir} failed: {e}")

        shutil.copytree(src_report_dir, dest_report_dir, dirs_exist_ok=True)
        copied_count += 1

    return copied_count, skipped_count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect generated reports from .acv_wd/report to acv_reports per firmware"
    )
    parser.add_argument(
        "--emulator-out",
        default="./data/01_journal_extension/emulator_out",
        help="Path to emulator_out directory (default: %(default)s)",
    )
    parser.add_argument(
        "--firmware",
        action="append",
        default=[],
        help="Firmware name filter (substring match). Can be repeated.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip packages that already have reports in acv_reports",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be copied without actually copying",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of parallel copy worker threads (default: %(default)s)",
    )
    args = parser.parse_args()

    emulator_out = Path(args.emulator_out).resolve()
    if not emulator_out.exists():
        logger.error(f"emulator_out not found: {emulator_out}")
        return 1

    fw_dirs = sorted([p for p in emulator_out.iterdir() if p.is_dir()])

    if args.firmware:
        needles = [f.strip() for f in args.firmware if f.strip()]
        fw_dirs = [fw for fw in fw_dirs if any(n in fw.name for n in needles)]
        logger.info(f"filter: firmware contains one of {args.firmware}")

    if not fw_dirs:
        logger.info("summary: ok=0 skipped=0 (no firmware matched filters)")
        return 0

    if args.dry_run:
        logger.info("[DRY-RUN MODE]")

    # Build global list of copy tasks (fw_id, src_report_dir, dest_report_dir)
    copy_tasks = []
    total_would_copy = 0
    total_skipped_pre = 0
    total_fw = len(fw_dirs)
    fw_processed = 0
    for fw_dir in fw_dirs:
        fw_processed += 1
        fw_id = fw_dir.name
        acv_snaps = fw_dir / "acv_snaps"
        acv_reports_root = fw_dir / "acv_reports"
        if not acv_snaps.exists():
            if fw_processed % 50 == 0 or fw_processed == total_fw:
                logger.info(f"Scanned firmware: {fw_processed}/{total_fw}")
            continue
        for pkg_dir in acv_snaps.iterdir():
            if not pkg_dir.is_dir() or pkg_dir.name == "pickle_files":
                continue
            pkg_name = pkg_dir.name
            src_report_dir = pkg_dir / ".acv_wd" / "report"
            dest_report_dir = acv_reports_root / pkg_name
            if not src_report_dir.exists():
                continue
            if args.skip_existing and dest_report_dir.exists():
                total_skipped_pre += 1
                continue
            # Include fw_dir so worker can validate and safely remove existing dest before copying
            copy_tasks.append((fw_id, src_report_dir, dest_report_dir, fw_dir))
            total_would_copy += 1
        # Log scan progress periodically
        if fw_processed % 50 == 0 or fw_processed == total_fw:
            logger.info(f"Scanned firmware: {fw_processed}/{total_fw} (tasks so far: {len(copy_tasks)})")

    logger.info(f"Built copy task list: {len(copy_tasks)} tasks, pre-existing skipped {total_skipped_pre}")

    if args.dry_run:
        logger.info(f"summary: would copy {total_would_copy} report(s), skipped pre-existing {total_skipped_pre}")
        return 0

    total_copied = 0
    total_skipped = total_skipped_pre

    # Define worker
    def copy_worker(task):
        fw_id, src, dest, fw_dir = task
        acv_reports_root = dest.parent
        try:
            acv_reports_root.mkdir(parents=True, exist_ok=True)
            # If destination exists, remove it to ensure clean copy (overwrite behavior)
            if dest.exists() and dest.is_dir():
                try:
                    # Ensure dest is under the firmware directory for safety
                    try:
                        dest.resolve().relative_to(fw_dir.resolve())
                        inside_fw = True
                    except Exception:
                        inside_fw = False

                    if inside_fw:
                        logger.info(f"Deleting existing report directory {dest}")
                        shutil.rmtree(dest)
                    else:
                        logger.warning(f"Destination {dest} is outside firmware dir {fw_dir}; not removing. Will merge instead.")
                except Exception as e:
                    logger.warning(f"Could not remove existing destination {dest}: {e}")

            shutil.copytree(src, dest, dirs_exist_ok=True)
            return (True, fw_id, dest)
        except Exception as e:
            return (False, fw_id, str(e))

    # Run copy tasks in parallel
    workers = max(1, args.workers)
    total = len(copy_tasks)
    processed = 0
    logger.info(f"Starting copy with {workers} workers for {total} task(s)")
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_task = {ex.submit(copy_worker, t): t for t in copy_tasks}
        for fut in concurrent.futures.as_completed(future_to_task):
            try:
                ok, fw_id, info = fut.result()
            except Exception as e:
                logger.error(f"Unexpected error during copy: {e}")
                processed += 1
                if processed % 20 == 0 or processed == total:
                    logger.info(f"Copy progress: {processed}/{total} completed")
                continue
            processed += 1
            if ok:
                total_copied += 1
                logger.info(f"Copy progress: {processed}/{total} - {fw_id}: copied {Path(info).name}")
            else:
                logger.error(f"Copy progress: {processed}/{total} - {fw_id}: copy failed: {info}")

            if processed % 50 == 0 or processed == total:
                logger.info(f"Copy progress: {processed}/{total} completed")

    logger.info(f"summary: copied {total_copied}, skipped {total_skipped}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
