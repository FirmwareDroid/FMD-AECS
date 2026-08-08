#!/usr/bin/env python3
"""Download and extract ACVTool pickle archives per firmware.

This mirrors the notebook approach where each firmware uses:
  acvtool_<firmware_id>.zip
from the Nexus raw files repository.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# logging is thread-safe by default; keep a lock only for the progress lines
# so multi-line progress blocks from different workers don't interleave.
_log_lock = threading.Lock()


def _log(msg: str, level: int = logging.INFO) -> None:
    with _log_lock:
        logger.log(level, msg)


def list_firmwares(emulator_out: Path) -> list[Path]:
    return sorted([p for p in emulator_out.iterdir() if p.is_dir()])


def filter_firmwares(fw_dirs: list[Path], firmware_filters: list[str], samples: int) -> list[Path]:
    selected = fw_dirs
    if firmware_filters:
        needles = [f.strip() for f in firmware_filters if f.strip()]
        selected = [fw for fw in selected if any(n in fw.name for n in needles)]
    if samples > 0:
        selected = selected[:samples]
    return selected


def has_pickles(pickle_root: Path) -> bool:
    return any(pickle_root.rglob("*.pickle")) if pickle_root.exists() else False


def download_zip_with_progress(
    url: str,
    zip_path: Path,
    timeout: int,
    fw_id: str,
    progress_interval: int,
) -> requests.Response:
    response = requests.get(url, stream=True, timeout=timeout)
    if response.status_code != 200:
        return response

    total = int(response.headers.get("content-length", 0))
    downloaded = 0
    next_progress = progress_interval
    start = time.time()

    _log(f"[{fw_id}] download started")
    with zip_path.open("wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            f.write(chunk)
            downloaded += len(chunk)

            if total > 0 and progress_interval > 0:
                percent = int(downloaded * 100 / total)
                if percent >= next_progress:
                    _log(
                        f"[{fw_id}] download {percent}%"
                        f" ({downloaded / (1024 * 1024):.1f}/{total / (1024 * 1024):.1f} MiB)"
                    )
                    while percent >= next_progress:
                        next_progress += progress_interval

    elapsed = time.time() - start
    _log(f"[{fw_id}] download done ({downloaded / (1024 * 1024):.1f} MiB in {elapsed:.1f}s)")
    return response


def fetch_one(
    fw_dir: Path,
    base_url: str,
    dry_run: bool,
    overwrite: bool,
    timeout: int,
    progress_interval: int,
) -> tuple[str, str]:
    fw_id = fw_dir.name
    acv_snaps = fw_dir / "acv_snaps"
    pickle_root = acv_snaps / "pickle_files"

    if not acv_snaps.exists():
        logger.warning(f"[{fw_id}] no acv_snaps folder found")
        return "skip", f"skip {fw_id}: no acv_snaps"

    if has_pickles(pickle_root) and not overwrite:
        logger.warning(f"[{fw_id}] already have pickle files")
        return "skip", f"skip {fw_id}: pickle_files already populated"

    url = f"{base_url.rstrip('/')}/acvtool_{fw_id}.zip"
    zip_path = acv_snaps / f"acvtool_{fw_id}.zip"

    if dry_run:
        return "ok", f"dry-run {fw_id}: would fetch {url}"

    try:
        _log(f"[{fw_id}] fetching {url}")
        resp = download_zip_with_progress(
            url=url,
            zip_path=zip_path,
            timeout=timeout,
            fw_id=fw_id,
            progress_interval=progress_interval,
        )
    except Exception as exc:
        return "fail", f"fail {fw_id}: request error: {exc}"

    if resp.status_code != 200:
        return "fail", f"fail {fw_id}: status {resp.status_code} for {url}"

    if pickle_root.exists() and overwrite:
        shutil.rmtree(pickle_root)

    pickle_root.mkdir(parents=True, exist_ok=True)

    try:
        shutil.unpack_archive(str(zip_path), str(pickle_root))
    except Exception as exc:
        return "fail", f"fail {fw_id}: unzip failed: {exc}"
    finally:
        if zip_path.exists():
            zip_path.unlink()

    count = sum(1 for _ in pickle_root.rglob("*.pickle"))
    if count == 0:
        return "fail", f"fail {fw_id}: extracted 0 pickle files"

    return "ok", f"ok {fw_id}: extracted {count} pickles"


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch ACVTool pickle archives for all firmware folders")
    parser.add_argument("--emulator-out", default="./data/01_journal_extension/emulator_out", help="Path to emulator_out")
    parser.add_argument("--base-url", default="https://fmd-repo.cloudlab.zhaw.ch:8443/repository/raw_files/", help="Nexus raw_files base URL")
    parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout seconds")
    parser.add_argument("--max-workers", type=int, default=10, help="Parallel download workers")
    parser.add_argument("--progress-interval", type=int, default=10, help="Progress print interval in percent")
    parser.add_argument("--firmware", action="append", default=[], help="Firmware name filter (substring match). Can be repeated.")
    parser.add_argument("--samples", type=int, default=0, help="Limit number of selected firmware folders (0 = no limit)")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing pickle_files content")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be downloaded")
    args = parser.parse_args()

    emulator_out = Path(args.emulator_out).resolve()
    if not emulator_out.exists():
        logger.error(f"emulator_out not found: {emulator_out}")
        return 1

    fw_dirs = list_firmwares(emulator_out)
    fw_dirs = filter_firmwares(fw_dirs, args.firmware, args.samples)
    if not fw_dirs:
        logger.info("summary: ok=0 skip=0 fail=0")
        logger.info("note: no firmware matched filters")
        return 0

    if args.firmware:
        logger.info(f"filter: firmware contains one of {args.firmware}")
    if args.samples > 0:
        logger.info(f"filter: limiting to first {args.samples} sample(s)")

    ok = 0
    skip = 0
    fail = 0
    total = len(fw_dirs)

    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as executor:
        futures = {
            executor.submit(
                fetch_one,
                fw_dir,
                args.base_url,
                args.dry_run,
                args.overwrite,
                args.timeout,
                max(1, args.progress_interval),
            ): fw_dir.name
            for fw_dir in fw_dirs
        }

        completed = 0
        for future in as_completed(futures):
            completed += 1
            fw_id = futures[future]
            try:
                status, msg = future.result()
            except Exception as exc:
                status, msg = "fail", f"fail {fw_id}: unexpected error: {exc}"

            level = logging.WARNING if status == "fail" else logging.INFO
            _log(f"[{completed}/{total}] {msg}", level)
            if status == "ok":
                ok += 1
            elif status == "skip":
                skip += 1
            else:
                fail += 1

    logger.info(f"summary: ok={ok} skip={skip} fail={fail}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
