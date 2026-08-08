#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   report <package_name> [extra acv report args]
#   acv <acv args>
#   <any command>

if [ "$#" -eq 0 ]; then
  echo "Usage: report <package_name> [extra args] | acv <args> | <command>"
  exit 1
fi

case "$1" in
  report)
    if [ "$#" -lt 2 ]; then
      echo "Error: missing package name"
      echo "Usage: report <package_name> [extra acv report args]"
      exit 1
    fi
    pkg="$2"
    shift 2

    # Determine working directory (matches notebook workflow)
    wd="${ACV_WD:-/work/acvtool_working_dir}"
    pkg_dir="$(dirname "$wd")"
    acv_snaps_dir="$(dirname "$pkg_dir")"

    # Ensure working directory and required subdirectories exist
    # This matches the (updated) structure expected by acvtool usage here:
    #   - pickles/: pickle files (code coverage profiles)
    #   - ec/: execution coverage (.ec) files
    #   - report/: output reports
    mkdir -p "$wd/pickles"
    mkdir -p "$wd/ec_files"

    # If a previous run left a covered_pickles or report folder, clean them so
    # we start from a fresh state. Remove and recreate to handle any leftover
    # files (including hidden files).
    if [ -d "$wd/covered_pickles" ]; then
      echo "[*] Cleaning existing covered_pickles: $wd/covered_pickles"
      rm -rf "$wd/covered_pickles"
    fi
    mkdir -p "$wd/covered_pickles"

    if [ -d "$wd/report" ]; then
      echo "[*] Cleaning existing report directory: $wd/report"
      rm -rf "$wd/report"
    fi
    mkdir -p "$wd/report"

    # Stage EC files from <package>/ec_files into the acv working dir.
    ec_src_dir="$pkg_dir/ec_files"
    ec_staged=0
    if [ -d "$ec_src_dir" ]; then
      while IFS= read -r -d '' ec_file; do
        cp -f "$ec_file" "$wd/ec_files/"
        ec_staged=$((ec_staged + 1))
      done < <(find "$ec_src_dir" -maxdepth 1 -type f -name '*.ec' -print0)
    fi

    # Stage matching pickle files from <firmware>/acv_snaps/pickle_files.
    # The notebook normalizes pickle stem by removing a trailing _<digits> suffix.
    pickle_staged=0
    pickle_src_root="$acv_snaps_dir/pickle_files"
    if [ -d "$pickle_src_root" ]; then
      while IFS= read -r -d '' pfile; do
        stem="$(basename "$pfile" .pickle)"
        normalized="$stem"
        if [[ "$stem" =~ _[0-9]+$ ]]; then
          normalized="${stem%_*}"
        fi
        normalized="$stem"
        if [ "$normalized" = "$pkg" ]; then
          cp -f "$pfile" "$wd/pickles/"
          pickle_staged=$((pickle_staged + 1))
        fi
      done < <(find "$pickle_src_root" -type f -name '*.pickle' -print0)
    fi

    echo "[*] ACVTool Working Directory Structure:"
    echo "    Root: $wd"
    echo "    ├── pickles/ ($(ls -1 "$wd/pickles" 2>/dev/null | wc -l) files)"
    echo "    ├── ec_files/ ($(ls -1 "$wd/ec_files" 2>/dev/null | wc -l) files)"
    echo "    └── report/ (output)"
    echo "    Staged EC files this run: $ec_staged"
    echo "    Staged pickle files this run: $pickle_staged"
    if [ "$pickle_staged" -eq 0 ] && [ "$(ls -1 "$wd/pickles" 2>/dev/null | wc -l)" -eq 0 ]; then
      echo "[!] Warning: no pickle files available for package '$pkg'."
      echo "    Expected source: $pickle_src_root"
    fi
    echo ""

    # Run acv cover-pickles to register staged pickles, then run acv report
    echo "[*] Registering staged pickles with: acv cover-pickles $pkg --wd $wd"
    acv cover-pickles "$pkg" --wd "$wd"

    # Finally run acv report with the prepared working directory
    exec acv report "$pkg" --wd "$wd" "$@"
    ;;
  acv)
    shift
    exec acv "$@"
    ;;
  *)
    exec "$@"
    ;;
esac

