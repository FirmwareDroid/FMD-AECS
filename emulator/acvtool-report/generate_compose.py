#!/usr/bin/env python3
"""
Generate a docker-compose.yml file from discovered firmware/packages.

Usage:
    python3 generate_compose.py [--emulator-out PATH] [--output docker-compose.yml]
"""

import sys
import argparse
import logging
from pathlib import Path
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def find_firmware_packages(emulator_out_dir):
    """Scan emulator_out and return {firmware_id: [package_names]}."""
    firmware_packages = defaultdict(list)
    emulator_path = Path(emulator_out_dir).resolve()

    if not emulator_path.exists():
        logger.error(f"Directory not found: {emulator_path}")
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


def filter_firmware_packages(firmware_packages, firmware_filters, samples):
    """Filter discovered firmware map by firmware substring and sample count."""
    items = sorted(firmware_packages.items(), key=lambda x: x[0])

    if firmware_filters:
        needles = [f.strip() for f in firmware_filters if f.strip()]
        items = [(fw, pkgs) for fw, pkgs in items if any(n in fw for n in needles)]

    if samples > 0:
        items = items[:samples]

    return dict(items)


def generate_compose_from_services(services, emulator_out_path):
    """
    Generate docker-compose.yml YAML content from a list of (firmware_id, package_name) tuples.

    Args:
        services: list of (firmware_id, package_name)
        emulator_out_path: absolute path to emulator_out

    Returns:
        str: YAML content
    """
    lines = [
        "services:",
        ""
    ]

    for idx, (firmware_id, package_name) in enumerate(services, start=1):
        # Generate safe service name: alphanumeric + dash only
        safename = f"report-{idx}".replace("_", "-")[:32]

        avc_wd = f"/work/input/{firmware_id}/acv_snaps/{package_name}/.acv_wd"

        lines.extend([
            f"  {safename}:",
            f"    image: acvtool-report:2.3.6",
            f"    volumes:",
            f"      - {emulator_out_path}:/work/input",
            f"    environment:",
            f"      ACV_WD: {avc_wd}",
            f"    command: report {package_name}",
            f"    # restart: on-failure",
            f"",
        ])

    lines.append("# Optional: Add resource limits")
    lines.append("# deploy:")
    lines.append("#   resources:")
    lines.append("#     limits:")
    lines.append("#       cpus: '2'")
    lines.append("#       memory: 4G")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate docker-compose.yml from discovered firmware/packages"
    )
    parser.add_argument(
        "--emulator-out",
        default="./data/01_journal_extension/emulator_out",
        help="Path to emulator_out directory (default: %(default)s)"
    )
    parser.add_argument(
        "--output",
        default="docker-compose.yml",
        help="Output file (default: %(default)s)"
    )
    parser.add_argument(
        "--firmware",
        action="append",
        default=[],
        help="Firmware name filter (substring match). Can be repeated."
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=0,
        help="Limit number of firmware folders (0 = no limit)"
    )
    parser.add_argument(
        "--max-services",
        type=int,
        default=250,
        help="Maximum number of services per docker-compose file (default: %(default)s)"
    )

    args = parser.parse_args()

    emulator_out = Path(args.emulator_out).resolve()
    output_path = Path(args.output).resolve()

    logger.info(f"Scanning {emulator_out}...")
    firmware_packages = find_firmware_packages(emulator_out)
    firmware_packages = filter_firmware_packages(firmware_packages, args.firmware, args.samples)

    if not firmware_packages:
        logger.error("No firmware packages found.")
        return 1

    if args.firmware:
        logger.info(f"Filter firmware contains one of: {args.firmware}")
    if args.samples > 0:
        logger.info(f"Limit to first {args.samples} firmware sample(s)")

    total_tasks = sum(len(p) for p in firmware_packages.values())
    logger.info(f"Found {len(firmware_packages)} firmware(s), {total_tasks} package(s)")

    # Flatten firmware/package pairs into a stable list and skip packages with empty ec_files
    services = []
    for fw in sorted(firmware_packages.keys()):
        for pkg in sorted(firmware_packages[fw]):
            ec_dir = emulator_out / fw / "acv_snaps" / pkg / "ec_files"
            has_files = False
            if ec_dir.exists():
                try:
                    has_files = bool(next(ec_dir.glob(".ec"), None))
                except Exception:
                    has_files = False
            if not has_files:
                logger.warning(f"Skipping {fw}/{pkg}: no files in {ec_dir}")
                continue
            services.append((fw, pkg))

    # Recompute actual number of services and inform if some were skipped
    actual_services = len(services)
    if actual_services != total_tasks:
        logger.info(f"{total_tasks - actual_services} package(s) skipped because ec_files was empty")

    max_per_file = args.max_services
    if max_per_file <= 0:
        max_per_file = 500

    # Partition into chunks
    chunks = [services[i:i+max_per_file] for i in range(0, len(services), max_per_file)]

    written_files = []
    for idx, chunk in enumerate(chunks, start=1):
        if len(chunks) == 1:
            out_path = output_path
        else:
            out_name = f"{output_path.stem}-{idx}{output_path.suffix}"
            out_path = output_path.with_name(out_name)

        compose_content = generate_compose_from_services(chunk, str(emulator_out))
        out_path.write_text(compose_content, encoding='utf-8')
        written_files.append(out_path)
        logger.info(f"Wrote {out_path}")

    if len(written_files) == 1:
        logger.info(f"\nNext steps:")
        logger.info(f"  docker compose -f {written_files[0].name} up -d")
        logger.info(f"  docker compose -f {written_files[0].name} logs -f")
        logger.info(f"  docker compose -f {written_files[0].name} down")
    else:
        logger.info("\nWrote multiple compose files:")
        for p in written_files:
            logger.info(f"  {p.name}")
        logger.info("\nNext steps (example):")
        logger.info(f"  docker compose -f {written_files[0].name} up -d")
        logger.info(f"  docker compose -f {written_files[0].name} logs -f")
        logger.info(f"  docker compose -f {written_files[0].name} down")

    return 0


if __name__ == "__main__":
    sys.exit(main())

