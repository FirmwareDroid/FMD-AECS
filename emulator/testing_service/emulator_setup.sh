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

