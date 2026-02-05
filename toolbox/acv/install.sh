#!/usr/bin/env bash
# Install script for ACVPatcher
# - downloads ACVPatcher-linux.zip into current directory
# - extracts to ~/acvtool
# - makes the ACVPatcher executable
# - copies ./config.json to ~/acvtool/config.json

set -euo pipefail
trap 'echo "Error on line $LINENO" >&2; exit 1' ERR

URL="https://github.com/pilgun/acvpatcher/releases/download/1.0.8/ACVPatcher-linux.zip"
ZIP_NAME="ACVPatcher-linux.zip"
DEST_DIR="$HOME/acvtool"
EXTRACT_SUBDIR="$DEST_DIR/ACVPatcher-linux"
CONFIG_SRC="./config.json"
CONFIG_DEST="$DEST_DIR/config.json"

echo "==> ACV Patcher installer"

# Download the zip to current directory
echo "Downloading ${URL} -> ./$(basename "$ZIP_NAME")"
if command -v curl >/dev/null 2>&1; then
  curl -fL "$URL" -o "$ZIP_NAME"
else
  echo "curl is required but not found. Please install curl." >&2
  exit 2
fi

# Create destination directory
echo "Creating destination: $DEST_DIR"
mkdir -p "$DEST_DIR"

# Unzip into the destination directory
if command -v unzip >/dev/null 2>&1; then
  echo "Unzipping $ZIP_NAME into $DEST_DIR"
  unzip -o "$ZIP_NAME" -d "$DEST_DIR"
else
  echo "unzip is required but not found. Please install unzip." >&2
  exit 3
fi

# Make the binary executable if present
if [ -f "$EXTRACT_SUBDIR/ACVPatcher" ]; then
  echo "Setting executable permission on $EXTRACT_SUBDIR/ACVPatcher"
  chmod +x "$EXTRACT_SUBDIR/ACVPatcher"
else
  echo "Warning: Expected binary not found at $EXTRACT_SUBDIR/ACVPatcher" >&2
fi

# Copy config.json to destination if it exists in current directory
if [ -f "$CONFIG_SRC" ]; then
  echo "Copying $CONFIG_SRC -> $CONFIG_DEST"
  cp -f "$CONFIG_SRC" "$CONFIG_DEST"
else
  echo "Warning: $CONFIG_SRC not found in current directory. Skipping config copy." >&2
fi

echo "Done. ACVPatcher is installed under $DEST_DIR"
echo "You can run: $EXTRACT_SUBDIR/ACVPatcher"
