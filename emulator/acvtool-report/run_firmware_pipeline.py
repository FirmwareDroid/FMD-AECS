#!/usr/bin/env python3
"""Run ACV report pipeline firmware-by-firmware.

For each selected firmware this script performs:
1) Fetch pickles for that firmware
2) Generate docker-compose file for that firmware
3) Run docker compose to generate reports
4) Collect reports into <firmware>/acv_reports/
5) Cleanup pickle zip + extracted pickle_files

Snap files and runtime artifacts under acv_snaps/<package>/ are kept.
"""

from __future__ import annotations

import argparse
import logging
import os.path
import shutil
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
RUN_COLLECT = False

def run_cmd(cmd: list[str], dry_run: bool) -> int:
    logger.info("$ %s", " ".join(cmd))
    if dry_run:
        return 0
    completed = subprocess.run(cmd)
    return completed.returncode


def list_firmwares(emulator_out: Path) -> list[Path]:
    return sorted([p for p in emulator_out.iterdir() if p.is_dir() and (p / "acv_snaps").exists()])


def select_firmwares(firmwares: list[Path], filters: list[str], samples: int) -> list[Path]:
    selected = firmwares
    if filters:
        needles = [f.strip() for f in filters if f.strip()]
        selected = [fw for fw in selected if any(n in fw.name for n in needles)]
    if samples > 0:
        selected = selected[:samples]
    return selected


def cleanup_pickle_artifacts(fw_dir: Path, dry_run: bool) -> None:
    fw_id = fw_dir.name
    acv_snaps = fw_dir / "acv_snaps"
    zip_path = acv_snaps / f"acvtool_{fw_id}.zip"
    pickle_dir = Path(os.path.abspath(acv_snaps / "pickle_files"))

    if zip_path.exists():
        logger.info(f"cleanup: remove {zip_path}")
        if not dry_run:
            zip_path.unlink()

    if pickle_dir.exists():
        logger.info(f"cleanup: remove {pickle_dir}")
        if not dry_run:
            try:
                if pickle_dir.is_dir() and str(BASE_DIR) in str(pickle_dir):
                    shutil.rmtree(pickle_dir)
            except Exception as e:
                logger.error(f"cleanup: failed to remove {pickle_dir}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run full ACV docker pipeline one firmware at a time")
    parser.add_argument("--emulator-out", default="./data/01_journal_extension/emulator_out", help="Path to emulator_out")
    parser.add_argument("--base-url", default="https://fmd-repo.cloudlab.zhaw.ch:8443/repository/raw_files/", help="Nexus raw_files base URL")
    parser.add_argument("--image", default="acvtool-report:2.3.6", help="Docker image tag for report containers")
    parser.add_argument("--firmware", action="append", default=[], help="Firmware name filter (substring match), repeatable")
    parser.add_argument("--samples", type=int, default=0, help="Limit number of selected firmware folders (0=no limit)")
    parser.add_argument("--fetch-workers", type=int, default=1, help="Parallel workers inside fetch_pickles (use 1 for one firmware)")
    parser.add_argument("--progress-interval", type=int, default=10, help="Download progress step percent for fetch_pickles")
    parser.add_argument("--overwrite-pickles", action="store_true", help="Force re-download/overwrite pickle_files")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue with next firmware if one firmware fails")
    parser.add_argument("--dry-run", action="store_true", help="Print commands only")
    args = parser.parse_args()

    emulator_out = Path(args.emulator_out).resolve()
    if not emulator_out.exists():
        logger.error(f"emulator_out not found: {emulator_out}")
        return 1

    script_dir = Path(__file__).resolve().parent
    fetch_script = script_dir / "fetch_pickles.py"
    compose_gen_script = script_dir / "generate_compose.py"
    collect_script = script_dir / "collect_reports.py"

    for p in (fetch_script, compose_gen_script, collect_script):
        if not p.exists():
            logger.error(f"missing required script: {p}")
            return 1

    firmwares = select_firmwares(list_firmwares(emulator_out), args.firmware, args.samples)
    if not firmwares:
        logger.info("no firmware selected")
        return 0

    logger.info(f"selected firmware count: {len(firmwares)}")
    failures = 0

    for idx, fw_dir in enumerate(firmwares, start=1):
        fw = fw_dir.name
        compose_file = script_dir / f"docker-compose.{fw}.yml"
        logger.info("=" * 90)
        logger.info(f"[{idx}/{len(firmwares)}] firmware: {fw}")

        firmware_failed = False
        try:
            # 1) fetch pickles for this firmware only
            fetch_cmd = [
                sys.executable,
                str(fetch_script),
                "--emulator-out", str(emulator_out),
                "--base-url", args.base_url,
                "--firmware", fw,
                "--samples", "1",
                "--max-workers", str(max(1, args.fetch_workers)),
                "--progress-interval", str(max(1, args.progress_interval)),
            ]
            if args.overwrite_pickles:
                fetch_cmd.append("--overwrite")
            if args.dry_run:
                fetch_cmd.append("--dry-run")
            if run_cmd(fetch_cmd, args.dry_run) != 0:
                firmware_failed = True
                raise RuntimeError("fetch_pickles failed")

            # 2) generate compose for this firmware only
            gen_cmd = [
                sys.executable,
                str(compose_gen_script),
                "--emulator-out", str(emulator_out),
                "--output", str(compose_file),
                "--firmware", fw,
                "--samples", "1",
            ]
            if run_cmd(gen_cmd, args.dry_run) != 0:
                firmware_failed = True
                raise RuntimeError("generate_compose failed")

            # 3) start compose services for report generation
            up_cmd = [
                "docker", "compose",
                "-f", str(compose_file),
                "up", "--remove-orphans",
            ]
            if run_cmd(up_cmd, args.dry_run) != 0:
                firmware_failed = True
                raise RuntimeError("docker compose up failed")

            if RUN_COLLECT:
                # 4) collect reports into /<firmware>/acv_reports/
                collect_cmd = [
                    sys.executable,
                    str(collect_script),
                    "--emulator-out", str(emulator_out),
                    "--firmware", fw,
                ]
                if run_cmd(collect_cmd, args.dry_run) != 0:
                    firmware_failed = True
                    raise RuntimeError("collect_reports failed")

        except Exception as exc:
            firmware_failed = True
            logger.error(f"firmware {fw} failed: {exc}")
            failures += 1
        finally:
            # Stop/remove compose resources for this firmware compose file
            down_cmd = ["docker", "compose", "-f", str(compose_file), "down", "--remove-orphans"]
            run_cmd(down_cmd, args.dry_run)

            # Remove temporary compose file
            if compose_file.exists():
                logger.info(f"cleanup: remove {compose_file}")
                if not args.dry_run:
                    compose_file.unlink()

            # 5) remove zip and extracted pickle_files
            cleanup_pickle_artifacts(fw_dir, args.dry_run)

        if firmware_failed and not args.continue_on_error:
            logger.error("stopping due to error (use --continue-on-error to continue)")
            return 2

    logger.info("=" * 90)
    logger.info(f"done: processed={len(firmwares)} failures={failures}")
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
