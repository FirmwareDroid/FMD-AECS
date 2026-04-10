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

mkdir -p "$LOG_DIR"
chmod -R 777 "$LOG_DIR" 2>/dev/null || true

# capture script stdout/stderr to logfile while still showing on console
exec > >(tee -a "$LOGFILE") 2> >(tee -a "$LOGFILE" >&2)

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

cleanup() {
  trap - SIGINT SIGTERM EXIT
  log "Cleaning up subprocesses..."
  if [[ $emulator_pid -ne 0 ]]; then
    kill -TERM "$emulator_pid" 2>/dev/null || true
  fi
  if [[ ${#PIDS[@]} -gt 0 ]]; then
    kill -TERM "${PIDS[@]}" 2>/dev/null || true
    sleep 1
    kill -KILL "${PIDS[@]}" 2>/dev/null || true
  fi
  pkill -f "emulator -avd" 2>/dev/null || true
  pkill -f "socat -d tcp-listen:5555" 2>/dev/null || true
  pkill -f "socat -d tcp-listen:8554" 2>/dev/null || true
  pkill -f "pulseaudio" 2>/dev/null || true
  exit 0
}

trap cleanup SIGINT SIGTERM EXIT

ensure_environment() {
  # ensure writable logs and tmp perms
  chmod -R 777 /tmp/ 2>/dev/null || true
}

stop_existing_helpers() {
  # best-effort stop of known helper processes that might conflict
  pkill -f "emulator -avd" 2>/dev/null || true
  pkill -f "socat -d tcp-listen:5555" 2>/dev/null || true
  pkill -f "socat -d tcp-listen:8554" 2>/dev/null || true
  pkill -f "pulseaudio" 2>/dev/null || true
}

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

find_qemu_child() {
  local wrapper_pid="$1"
  local deadline=$((SECONDS+10))
  local pid
  while [[ $SECONDS -lt $deadline ]]; do
    # check immediate children first
    for pid in $(pgrep -P "$wrapper_pid" 2>/dev/null || true); do
      local comm; comm=$(ps -p "$pid" -o comm= 2>/dev/null || true)
      if [[ "$comm" == *qemu* ]]; then
        echo "$pid"
        return 0
      fi
    done
    # fallback: locate global qemu-system processes matching AVD name
    for pid in $(pgrep -f 'qemu-system' 2>/dev/null || true); do
      local args; args=$(ps -p "$pid" -o args= 2>/dev/null || true)
      if [[ "$args" == *"$AVD"* || "$args" == *qemu-system-* ]]; then
        echo "$pid"
        return 0
      fi
    done
    sleep 0.5
  done
  return 1
}

monitor_runtime() {
  local qemu_pid
  qemu_pid=$(find_qemu_child "$emulator_pid" || true)
  if [[ -n "$qemu_pid" ]]; then
    log "Monitoring qemu subprocess pid $qemu_pid (emulator wrapper pid $emulator_pid)"
    while ps -p "$qemu_pid" >/dev/null 2>&1; do
      if tail -n 200 "$LOG_DIR/emulator_${AVD}.log" 2>/dev/null | grep -qi "kvm run failed"; then
        warn "Detected KVM error in emulator log; restarting script..."
        kill -TERM "$qemu_pid" 2>/dev/null || true
        kill -TERM "$emulator_pid" 2>/dev/null || true
        handle_restart
      fi
      sleep 1
    done
    log "qemu subprocess pid $qemu_pid has exited."
  else
    warn "Could not locate qemu subprocess for wrapper pid $emulator_pid; falling back to waiting on wrapper."
    wait "$emulator_pid" || true
    local sts=$?
    if [[ $sts -eq 0 ]]; then
      log "Emulator wrapper process (pid $emulator_pid) exited normally (exit code 0)."
    else
      if [[ $sts -gt 128 ]]; then
        local sig=$((sts - 128))
        log "Emulator wrapper (pid $emulator_pid) terminated by signal $sig (exit status $sts)."
      else
        log "Emulator wrapper (pid $emulator_pid) stopped with exit code $sts."
      fi
    fi
  fi
}

handle_restart() {
  # enforce cooldown and max restarts then re-exec
  local now; now=$(date +%s)
  local last=${LAST_RESTART_TS:-0}
  local elapsed=$((now - last))
  if [[ $elapsed -lt $RESTART_COOLDOWN ]]; then
    local sleep_s=$((RESTART_COOLDOWN - elapsed))
    log "Restart cooldown active; sleeping $sleep_s s before restart..."
    sleep "$sleep_s"
  fi
  export LAST_RESTART_TS=$(date +%s)
  local count=${RESTART_COUNT:-0}
  local next=$((count + 1))
  if [[ $next -gt $MAX_RESTARTS ]]; then
    err "Maximum restart limit ($MAX_RESTARTS) reached (count=$count). Not restarting."
    exit 3
  fi
  export RESTART_COUNT=$next
  exec "$0" "${ORIG_ARGS[@]}"
}

main() {
  ensure_environment
  stop_existing_helpers

  architecture=$(uname -m)
  case "$architecture" in
    x86_64) AVD="x86_64" ;;
    aarch64) AVD="Arm64" ;;
    *) err "Unsupported architecture: $architecture"; exit 2 ;;
  esac

  EMU_CMD=(/android/sdk/emulator/emulator -avd "$AVD" -no-window -no-snapshot -ports "5556,5557" -grpc "8556" -skip-adb-auth -no-snapshot-save -logcat "*:V" -show-kernel -logcat-output "$REAL_LOGCAT_LOG" -shell-serial "file:$REAL_KERNEL_LOG" -gpu swiftshader_indirect -qemu -append "panic=1" -cpu max -machine gic-version=max)

  # Prepare AVD directory and ensure adb server
  prepare_avd "/android/sdk/avd/${AVD}.avd" "/android/sdk/avd/config.ini"
  start_adb_nodaemon

  # Start emulator
  log "EMU_CMD: $(printf '%q ' "${EMU_CMD[@]}")"
  log "Starting emulator in background (logs -> $LOG_DIR/emulator_${AVD}.log)"
  "${EMU_CMD[@]}" >"$LOG_DIR/emulator_${AVD}.log" 2>&1 &
  emulator_pid=$!

  # short early health check
  local deadline=$((SECONDS+8))
  while [[ $SECONDS -lt $deadline ]]; do
    if grep -E "cannot use stdio by multiple character devices|QEMU main loop exits abnormally|Unable to spawn process|kvm run failed|could not install \*smartsocket\* listener" "$LOG_DIR/emulator_${AVD}.log" >/dev/null 2>&1; then
      warn "Detected immediate emulator/QEMU startup failure (see $LOG_DIR/emulator_${AVD}.log)."
      tail -n 200 "$LOG_DIR/emulator_${AVD}.log" || true
      kill -TERM "$emulator_pid" 2>/dev/null || true
      sleep 1
      handle_restart
    fi
    sleep 0.5
  done

  monitor_runtime

  log "Emulator stopped; cleaning up..."
  cleanup
}

main "$@"
