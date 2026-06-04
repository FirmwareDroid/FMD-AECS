#!/usr/bin/env bash

set -euo pipefail

# emulator_start.sh - start and monitor Android emulator (single run)
# Clean, modular implementation with logging and restart safeguards.

PIDS=()
emulator_pid=0
ORIG_ARGS=("$@")

# Configuration (can be overridden via env)
RESTART_COOLDOWN=${RESTART_COOLDOWN:-10}
MAX_RESTARTS=${MAX_RESTARTS:-5}

BASEDIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$BASEDIR/out/logs"
REAL_KERNEL_LOG="$LOG_DIR/kernel.log"
REAL_LOGCAT_LOG="$LOG_DIR/logcat.log"
LOGFILE="$LOG_DIR/emulator_start.log"
TCPDUMP_PCAP="$BASEDIR/out/pcaps/emulator_traffic_full.pcap"

mkdir -p "$LOG_DIR"
chmod -R 777 "$LOG_DIR" 2>/dev/null || true

SSLKEY_DIR="$BASEDIR/out/pcaps"
SSLKEYFILE="$SSLKEY_DIR/out/sslkeylog.log"
mkdir -p "$SSLKEY_DIR"
touch "$SSLKEYFILE" 2>/dev/null || true
chmod 600 "$SSLKEYFILE" 2>/dev/null || true || true
export SSLKEYLOGFILE="$SSLKEYFILE"

echo "SSLKEYLOGFILE set -> $SSLKEYLOGFILE"
echo "Log directory set -> $LOG_DIR"

log() { printf '%s %s
' "[INFO]" "$*"; }
warn() { printf '%s %s
' "[WARN]" "$*"; }
err() { printf '%s %s
' "[ERROR]" "$*"; }


start_adb_nodaemon() {
  # ensure no conflicting adb server is running, then start nodaemon adb
  if command -v pgrep >/dev/null 2>&1; then
    if pgrep -x adb >/dev/null 2>&1; then
      log "Stopping existing adb processes to avoid smartsocket conflicts..."
      pkill -x adb || true
      sleep 0.5
    fi
  else
    pkill -f "/android/sdk/platform-tools/adb" >/dev/null 2>&1 || true
    sleep 0.5
  fi
  /android/sdk/platform-tools/adb kill-server || true
  /android/sdk/platform-tools/adb -a -P 5037 nodaemon server &
  PIDS+=("$!")
}

prepare_avd() {
  local avd_dir="$1"
  local config_src="$2"
  log "Preparing AVD directory: $avd_dir"
  if [[ -d "$avd_dir" ]]; then
    log "Removing existing AVD directory: $avd_dir"
    rm -rf "$avd_dir" || true
  fi
  mkdir -p "$avd_dir"
  if [[ -f "$config_src" ]]; then
    log "Copying $config_src -> $avd_dir/config.ini"
    cp "$config_src" "$avd_dir/config.ini" || true
    chmod 644 "$avd_dir/config.ini" 2>/dev/null || true
  else
    warn "AVD config source not found: $config_src"
  fi
}

main() {
  architecture=$(uname -m)
  case "$architecture" in
    x86_64) AVD="x86_64" ;;
    aarch64) AVD="Arm64" ;;
    *) err "Unsupported architecture: $architecture"; exit 2 ;;
  esac

  EMU_CMD=(/android/sdk/emulator/emulator -avd "$AVD" -tcpdump "$TCPDUMP_PCAP" -no-window -no-snapshot -ports "5556,5557" -grpc "8556" -skip-adb-auth -no-snapshot-save -logcat "*:V" -show-kernel -logcat-output "$REAL_LOGCAT_LOG" -shell-serial "file:$REAL_KERNEL_LOG" -gpu swiftshader_indirect -qemu -append "panic=5" -cpu max -machine gic-version=max)

  # Prepare AVD directory and ensure adb server
  prepare_avd "/android/sdk/avd/${AVD}.avd" "/android/sdk/avd/config.ini"
  start_adb_nodaemon

  # Start emulator
  log "EMU_CMD: $(printf '%q ' "${EMU_CMD[@]}")"
  log "Starting emulator in background (logs -> $LOG_DIR/emulator_${AVD}.log)"
  "${EMU_CMD[@]}" >"$LOG_DIR/emulator_${AVD}.log" 2>&1 &
  emulator_pid=$!
}

main "$@"
