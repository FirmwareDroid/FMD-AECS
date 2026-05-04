#!/bin/bash
set -e

# Setup helper for emulator container. Starts port forwarding (socat),
# pulseaudio helper and logger forwarding (tail -F) that were previously
# embedded inside emulator_start.sh. Intended to be launched by the
# docker entrypoint as a background helper.

BASEDIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$BASEDIR/out/logs"
REAL_KERNEL_LOG="$LOG_DIR/kernel.log"
REAL_LOGCAT_LOG="$LOG_DIR/logcat.log"

mkdir -p "$LOG_DIR"
chmod -R 777 "$LOG_DIR" 2>/dev/null || true

echo "[setup] Starting emulator environment helper (logs -> $LOG_DIR)"

# Ensure persistent log files exist
: > "$REAL_KERNEL_LOG" 2>/dev/null || true
: > "$REAL_LOGCAT_LOG" 2>/dev/null || true

# Enforce maximum log file size to avoid unbounded growth. We keep the last
# MAX_LOG_BYTES bytes when truncating. This runs once at startup and also in a
# background monitor that periodically trims files that exceed the limit.
MAX_LOG_BYTES=$((100 * 1024 * 1024)) # 100 MB

truncate_keep_tail() {
  local f="$1"
  local max="$2"
  # If file doesn't exist or is not readable, nothing to do
  [ -f "$f" ] || return 0
  # Use wc -c to get file size in bytes (portable)
  local sz
  sz=$(wc -c < "$f" 2>/dev/null || echo 0)
  if [ -z "$sz" ]; then sz=0; fi
  if [ "$sz" -le "$max" ]; then
    return 0
  fi
  # Keep the last $max bytes using tail -c. Write to a temporary file and
  # atomically move it into place. Preserve file permissions where possible.
  local tmp
  tmp="${f}.tmp.$$"
  # Some busybox tails may not support -c with negative values; use positive
  tail -c "$max" "$f" > "$tmp" 2>/dev/null || {
    # Fallback: copy last 100MB using dd if seek from end (POSIX shells vary);
    # As a last resort, just truncate to zero to avoid unbounded growth.
    : > "$f"
    return 0
  }
  # Attempt to preserve mode/owner
  if command -v stat >/dev/null 2>&1; then
    if stat_out=$(stat -c '%a %u %g' "$f" 2>/dev/null); then
      set -- $stat_out || true
      perm="$1"; uid="$2"; gid="$3"
      chmod "$perm" "$tmp" 2>/dev/null || true
      chown "$uid":"$gid" "$tmp" 2>/dev/null || true
    fi
  fi
  mv -f "$tmp" "$f"
}

# Run a single truncation pass at startup to trim any oversized logs left from
# previous runs before we begin tailing them.
truncate_keep_tail "$REAL_KERNEL_LOG" "$MAX_LOG_BYTES"
truncate_keep_tail "$REAL_LOGCAT_LOG" "$MAX_LOG_BYTES"

# Start socat port forwarders for emulator connectivity
#echo "[setup] Starting socat listeners (5555 -> 5557, 8554 -> 8556)"
#socat -d tcp-listen:5555,reuseaddr,fork tcp:127.0.0.1:5557 2>"$LOG_DIR/socat-5555.log" &
#echo $! > "$LOG_DIR/.socat-5555.pid" 2>/dev/null || true

#socat -d tcp-listen:8554,reuseaddr,fork tcp:127.0.0.1:8556 2>"$LOG_DIR/socat-8554.log" &
#echo $! > "$LOG_DIR/.socat-8554.pid" 2>/dev/null || true

# Start pulseaudio in daemon mode if available
if command -v pulseaudio >/dev/null 2>&1; then
  echo "[setup] Starting pulseaudio (daemon)"
  touch "$LOG_DIR/pulseverbose.log"
  pulseaudio -D -vvvv --log-time=1 --log-target=newfile:$LOG_DIR/pulseverbose.log --exit-idle-time=-1 2>>"$LOG_DIR/pulseverbose.err" || true
  echo $! > "$LOG_DIR/.pulseaudio.pid" 2>/dev/null || true
  # stream pulse logs
  tail -F "$LOG_DIR/pulseverbose.log" -n +1 2>/dev/null | sed -u 's/^/pulse: /g' &
  echo $! > "$LOG_DIR/.tail-pulse.pid" 2>/dev/null || true
else
  echo "[setup] pulseaudio not available; skipping"
fi

# Stream emulator-specific persistent files so they appear in container logs
echo "[setup] Starting tails for kernel/logcat/goldfish"
tail -F "$LOG_DIR/goldfish_rtc_0" 2>/dev/null | sed -u 's/^/video: /g' &
echo $! > "$LOG_DIR/.tail-goldfish.pid" 2>/dev/null || true

tail -F "$REAL_KERNEL_LOG" 2>/dev/null | sed -u 's/^/kernel: /g' &
echo $! > "$LOG_DIR/.tail-kernel.pid" 2>/dev/null || true

tail -F "$REAL_LOGCAT_LOG" 2>/dev/null | sed -u 's/^/logcat: /g' &
echo $! > "$LOG_DIR/.tail-logcat.pid" 2>/dev/null || true

echo "[setup] Emulator environment helper finished starting components"

# Exit; components run in background. The entrypoint can start emulator_start.sh
exit 0

# Background monitor to periodically trim the kernel/logcat logs to the
# configured MAX_LOG_BYTES to avoid unbounded disk usage. Runs every 60s.
(
  while true; do
    sleep 60
    truncate_keep_tail "$REAL_KERNEL_LOG" "$MAX_LOG_BYTES"
    truncate_keep_tail "$REAL_LOGCAT_LOG" "$MAX_LOG_BYTES"
  done
) &
echo $! > "$LOG_DIR/.log-trim-monitor.pid" 2>/dev/null || true

