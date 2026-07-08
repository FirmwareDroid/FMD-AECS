#!/usr/bin/env bash
set -euo pipefail

# Parallel ACVTool runner wrapper script.
#
# Usage:
#   ./run_reports.sh [options]
#   ./run_reports.sh --help

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="$SCRIPT_DIR/run_parallel.py"

if [ ! -f "$RUNNER" ]; then
    echo "Error: $RUNNER not found"
    exit 1
fi

python3 "$RUNNER" "$@"

