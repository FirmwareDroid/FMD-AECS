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

# Unzip into a temporary directory, then move the entire extracted folder to DEST_DIR/ACVPatcher-linux
if command -v unzip >/dev/null 2>&1; then
  echo "Unzipping $ZIP_NAME into a temporary directory"
  TMPDIR=$(mktemp -d)
  unzip -o "$ZIP_NAME" -d "$TMPDIR"

  # Determine extracted content. Prefer a directory named ACVPatcher-linux if present.
  if [ -d "$TMPDIR/ACVPatcher-linux" ]; then
    echo "Moving extracted folder to $EXTRACT_SUBDIR"
    rm -rf "$EXTRACT_SUBDIR"
    mv "$TMPDIR/ACVPatcher-linux" "$EXTRACT_SUBDIR"
  else
    # If the archive didn't contain the expected top-level folder, move everything under TMPDIR into EXTRACT_SUBDIR
    echo "Archive doesn't contain ACVPatcher-linux folder; moving all contents into $EXTRACT_SUBDIR"
    rm -rf "$EXTRACT_SUBDIR"
    mkdir -p "$EXTRACT_SUBDIR"
    shopt -s dotglob
    mv "$TMPDIR"/* "$EXTRACT_SUBDIR" 2>/dev/null || true
    shopt -u dotglob
  fi

  # cleanup tmp
  rm -rf "$TMPDIR"
else
  echo "unzip is required but not found. Please install unzip." >&2
  exit 3
fi

# Make the main binary executable if present (search for ACVPatcher executable inside EXTRACT_SUBDIR)
BINARY_PATH=""
if [ -x "$EXTRACT_SUBDIR/ACVPatcher" ]; then
  BINARY_PATH="$EXTRACT_SUBDIR/ACVPatcher"
elif [ -f "$EXTRACT_SUBDIR/ACVPatcher" ]; then
  BINARY_PATH="$EXTRACT_SUBDIR/ACVPatcher"
else
  # try to find any file named ACVPatcher anywhere inside the folder
  FOUND=$(find "$EXTRACT_SUBDIR" -maxdepth 3 -type f -name 'ACVPatcher' -print -quit || true)
  if [ -n "$FOUND" ]; then
    BINARY_PATH="$FOUND"
  fi
fi

if [ -n "$BINARY_PATH" ]; then
  echo "Setting executable permission on $BINARY_PATH"
  chmod +x "$BINARY_PATH"
else
  echo "Warning: Expected binary 'ACVPatcher' not found under $EXTRACT_SUBDIR" >&2
fi

# Copy config.json to destination if it exists in current directory
if [ -f "$CONFIG_SRC" ]; then
  echo "Copying $CONFIG_SRC -> $CONFIG_DEST"
  cp -f "$CONFIG_SRC" "$CONFIG_DEST"
else
  echo "Warning: $CONFIG_SRC not found in current directory. Skipping config copy." >&2
fi

echo "Done. ACVPatcher is installed under $DEST_DIR"
