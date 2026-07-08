#!/usr/bin/env bash
# Script to help stage pickle files and EC files for ACVTool report generation
#
# This script demonstrates how to prepare the working directory structure
# that matches the notebook workflow before running acv report.

set -euo pipefail

# Usage
if [ "$#" -lt 2 ]; then
    cat <<'EOF'
Usage: prepare_working_dir.sh <emulator_out_root> <firmware_id> <package_name> [--download-pickles]

Prepares the acvtool working directory structure for report generation.

Example:
  ./prepare_working_dir.sh \
    ./data/01_journal_extension/emulator_out \
    68af5a9765e2ad36cfb14a36_v12_sdk_phone64_arm64_userdebug_r9_dev \
    com.android.calculator

  Options:
    --download-pickles    Download pickle files from Nexus (requires credentials)

Working directory structure created:
  <firmware>/<package>/.acv_wd/
  ├── pickles/     (pickle files with code coverage info)
  ├── ec/                  (execution coverage .ec files)
  └── report/              (output location for reports)

Note:
  - EC files are copied from: <firmware>/acv_snaps/<package>/ec_files/
  - Pickle files typically need to be downloaded from Nexus or another source
  - Adjust NEXUS_BASE_URL and credentials as needed
EOF
    exit 1
fi

EMULATOR_OUT="$1"
FIRMWARE_ID="$2"
PACKAGE="$3"
DOWNLOAD_PICKLES="${4:-}"

# Paths
NEXUS_BASE_URL="https://fmd-repo.cloudlab.zhaw.ch:8443/repository/raw_files/"
FW_DIR="$EMULATOR_OUT/$FIRMWARE_ID"
PACKAGE_DIR="$FW_DIR/acv_snaps/$PACKAGE"
ACV_WD="$PACKAGE_DIR/.acv_wd"

# Verify inputs
if [ ! -d "$FW_DIR" ]; then
    echo "Error: Firmware directory not found: $FW_DIR"
    exit 1
fi

if [ ! -d "$PACKAGE_DIR" ]; then
    echo "Error: Package directory not found: $PACKAGE_DIR"
    exit 1
fi

# Create working directory structure
echo "[*] Creating working directory structure..."
mkdir -p "$ACV_WD/pickles"
mkdir -p "$ACV_WD/ec"
mkdir -p "$ACV_WD/report"

echo "    Root: $ACV_WD"
echo "    ├── pickles/"
echo "    ├── ec/"
echo "    └── report/"

# Stage EC files from the package
if [ -d "$PACKAGE_DIR/ec_files" ]; then
    echo ""
    echo "[*] Staging EC files..."
    ec_count=$(find "$PACKAGE_DIR/ec_files" -name "coverage_*.ec" -type f | wc -l)
    if [ "$ec_count" -gt 0 ]; then
        cp "$PACKAGE_DIR/ec_files"/coverage_*.ec "$ACV_WD/ec/" 2>/dev/null || true
        echo "    Staged $ec_count EC file(s) to $ACV_WD/ec/"
    else
        echo "    Warning: No EC files found in $PACKAGE_DIR/ec_files"
    fi
else
    echo ""
    echo "    Warning: EC directory not found: $PACKAGE_DIR/ec_files"
fi

# Handle pickle download if requested
if [ "$DOWNLOAD_PICKLES" = "--download-pickles" ]; then
    echo ""
    echo "[*] Downloading pickle files from Nexus..."
    pickle_filename="acvtool_$FIRMWARE_ID.zip"
    nexus_url="$NEXUS_BASE_URL/$pickle_filename"

    echo "    From: $nexus_url"
    echo "    To:   $ACV_WD/pickles/"

    # Download and extract pickles for this package
    temp_dir=$(mktemp -d)
    trap "rm -rf $temp_dir" EXIT

    if ! curl -fsSL -o "$temp_dir/$pickle_filename" "$nexus_url"; then
        echo "    Error: Failed to download pickles from Nexus"
        exit 1
    fi

    if ! unzip -q "$temp_dir/$pickle_filename" -d "$temp_dir"; then
        echo "    Error: Failed to extract pickle archive"
        exit 1
    fi

    # Find and copy pickles for this package
    pickle_count=0
    find "$temp_dir" -name "*.pickle" -type f | while read -r pickle; do
        if [[ "$pickle" =~ /$PACKAGE\.pickle$ ]]; then
            cp "$pickle" "$ACV_WD/pickles/"
            ((pickle_count++))
        fi
    done

    if [ "$pickle_count" -gt 0 ]; then
        echo "    Staged $pickle_count pickle file(s)"
    else
        echo "    Warning: No pickles found for package $PACKAGE"
    fi
else
    echo ""
    echo "[*] Pickle files not downloaded (use --download-pickles to enable)"
    echo "    You must manually stage .pickle files to: $ACV_WD/pickles/"
fi

echo ""
echo "[✓] Working directory prepared successfully"
echo ""
echo "Next steps:"
echo "  1. If needed, manually add pickle files to: $ACV_WD/pickles/"
echo "  2. Run: acv cover-pickles $PACKAGE --wd $ACV_WD"
echo "  3. Then run: acv report $PACKAGE --wd $ACV_WD"
echo ""

