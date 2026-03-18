#!/usr/bin/env bash

# Control script for running tcpdump when the Android emulator has fully booted.
# Usage: tcpdump.sh start [outdir]
#        tcpdump.sh stop
#        tcpdump.sh restart [outdir]
#        tcpdump.sh status
#
# The script will wait until the emulator reports sys.boot_completed==1 via adb,
# then start tcpdump (as root via sudo if necessary) capturing on the 'any'
# interface and write a timestamped pcap file into the specified output
# directory (default ./pcaps). A pidfile is used to allow stop/restart without
# stopping the emulator.

set -euo pipefail

WORKDIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="/tmp/emulator_tcpdump.pid"
OUTFILE_INFO="/tmp/emulator_tcpdump.info"
LOGFILE="/tmp/emulator_tcpdump.log"

usage() {
  cat <<EOF
Usage: $0 {start|stop|restart|status} [outdir] [device]

Commands:
  start [outdir] [device]   Wait for emulator boot (optionally on specified device), then start tcpdump and write pcap into outdir (default ./pcaps)
  stop             Stop the running tcpdump started by this script
  restart [outdir] [device] Stop (if running) then start tcpdump again (optionally target device)
  status           Show status of tcpdump and last pcap file created
EOF
}

find_adb() {
  if [ -x "./adb" ]; then
    echo "./adb"
  elif command -v adb >/dev/null 2>&1; then
    command -v adb
  else
    echo "";
  fi
}

# Select a device serial. If a device serial is specified (non-empty), return it.
# Otherwise return the first serial listed by `adb devices` (first non-empty device line).
select_device() {
  adb_bin="$1"
  specified="$2"
  if [ -n "$specified" ]; then
    echo "$specified"
    return 0
  fi
  # Pick the first non-empty device listed by `adb devices` (skip header)
  serial=$("$adb_bin" devices | awk 'NR>1 && NF{print $1; exit}' || true)
  echo "$serial"
}

wait_for_boot() {
  adb_bin="$1"
  device="$2"
  echo "Waiting for emulator to fully boot..."
  adb_cmd=("$adb_bin")
  if [ -n "$device" ]; then
    adb_cmd+=("-s" "$device")
    echo "Using adb device: $device"
  fi
  # Use the user's check loop
  while true; do
    output=$("${adb_cmd[@]}" shell getprop sys.boot_completed 2>/dev/null || true)
    if [ "$(printf '%s' "$output" | tr -d '\r')" = "1" ]; then
      break
    fi
    echo "Waiting for emulator to fully boot..."
    sleep 30
  done
  echo "Emulator reports boot completed."
}

start_tcpdump() {
  outdir="$1"
  mkdir -p "$outdir"
  timestamp=$(date +"%Y%m%d_%H%M%S")
  outfile="$outdir/emulator_$timestamp.pcap"

  if [ -f "$PIDFILE" ]; then
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "tcpdump already running (pid=$pid). Use '$0 stop' first or 'restart'." >&2
      exit 1
    else
      # Stale pidfile
      rm -f "$PIDFILE" || true
    fi
  fi

  if ! command -v tcpdump >/dev/null 2>&1; then
    echo "tcpdump not found in PATH. Please install tcpdump." >&2
    exit 2
  fi

  # Use sudo when not root so tcpdump can open interfaces
  SUDO=""
  if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
      SUDO=sudo
    else
      echo "Not running as root and sudo is not available. tcpdump may fail to open the interface." >&2
    fi
  fi

  echo "Starting tcpdump -> $outfile"
  # -U flush packet output as we go, -s 0 capture full packets, -i any to capture all interfaces
  # redirect tcpdump stderr to logfile for diagnostics
  $SUDO tcpdump -i any -s 0 -U -w "$outfile" 2>"$LOGFILE" &
  tcp_pid=$!
  # Save pid and info
  echo "$tcp_pid" > "$PIDFILE"
  echo "$outfile" > "$OUTFILE_INFO"
  echo "tcpdump started (pid=$tcp_pid), writing to $outfile"
}

stop_tcpdump() {
  if [ ! -f "$PIDFILE" ]; then
    echo "No pidfile found. tcpdump not running (or started differently)." >&2
    return 1
  fi
  pid=$(cat "$PIDFILE" 2>/dev/null || true)
  if [ -z "$pid" ]; then
    echo "Pidfile empty; removing." >&2
    rm -f "$PIDFILE" "$OUTFILE_INFO" || true
    return 1
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "Process $pid not running; cleaning pidfile." >&2
    rm -f "$PIDFILE" "$OUTFILE_INFO" || true
    return 1
  fi

  echo "Stopping tcpdump (pid=$pid)"
  kill -TERM "$pid" 2>/dev/null || true
  # give it a little time to flush and exit
  for i in {1..10}; do
    if kill -0 "$pid" 2>/dev/null; then
      sleep 0.5
    else
      break
    fi
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "tcpdump did not exit, sending KILL" >&2
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$PIDFILE" || true
  echo "Stopped."
}

status_tcpdump() {
  if [ -f "$PIDFILE" ]; then
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      outfile="$(cat "$OUTFILE_INFO" 2>/dev/null || echo "(unknown)")"
      echo "tcpdump running (pid=$pid) -> $outfile"
      return 0
    else
      echo "tcpdump pidfile present but process not running." >&2
      return 1
    fi
  else
    echo "tcpdump is not running." >&2
    return 1
  fi
}

if [ "$#" -lt 1 ]; then
  usage
  exit 1
fi

cmd="$1"
arg_outdir="${2:-./pcaps}"
arg_device="${3:-}"    # optional device serial passed as 3rd positional argument

adb_bin=$(find_adb)
if [ -z "$adb_bin" ]; then
  echo "Warning: adb not found in PATH and ./adb not present. The script won't be able to wait for emulator boot." >&2
else
  # If ADB_DEVICE env var is set and no positional device passed, prefer it
  if [ -z "$arg_device" ] && [ -n "${ADB_DEVICE:-}" ]; then
    arg_device="$ADB_DEVICE"
  fi
  # If no explicit device, and more than one device connected, select the first
  device_count=$("$adb_bin" devices | awk 'NR>1 && NF{count++}END{print count+0}')
  if [ -z "$arg_device" ] && [ "$device_count" -gt 1 ]; then
    echo "Multiple adb devices detected ($device_count). Selecting the first device reported by 'adb devices'."
  fi
  selected_device=$(select_device "$adb_bin" "$arg_device")
  if [ -z "$selected_device" ]; then
    echo "No adb device found." >&2
  else
    echo "Selected adb device: $selected_device"
  fi
fi

case "$cmd" in
  start)
    if [ -n "$adb_bin" ]; then
      wait_for_boot "$adb_bin" "$selected_device"
    else
      echo "Skipping emulator boot wait because adb was not found. Proceeding to start tcpdump..." >&2
    fi
    start_tcpdump "$arg_outdir"
    ;;
  stop)
    stop_tcpdump
    ;;
  restart)
    stop_tcpdump || true
    if [ -n "$adb_bin" ]; then
      wait_for_boot "$adb_bin" "$selected_device"
    fi
    start_tcpdump "$arg_outdir"
    ;;
  status)
    status_tcpdump
    ;;
  *)
    usage
    exit 2
    ;;
esac

