#!/bin/bash

# All background pids for the current iteration
PIDS=()
emulator_pid=0
# Preserve original args so we can re-exec with the same arguments if needed
ORIG_ARGS=("$@")
ENABLE_TCPDUMP=0  # if 1, start tcpdump helper to capture traffic

# Simple CLI parsing (we only remove the flags from $@ but keep ORIG_ARGS intact)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --reexec|--attach-reexec)
      # legacy aliases removed; this option is no longer supported
      echo "Warning: --reexec is deprecated and ignored."
      shift
      ;;
    --tcpdump|--enable-tcpdump)
      ENABLE_TCPDUMP=1
      shift
      ;;
    --no-tcpdump|--disable-tcpdump)
      ENABLE_TCPDUMP=0
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [--tcpdump]"
      echo "  --tcpdump       Enable tcpdump helper to capture emulator traffic (disabled by default)"
      exit 0
      ;;
    *)
      # ignore other args for now
      shift
      ;;
  esac
done

BASEDIR="$(cd "$(dirname "$0")" && pwd)"

# Centralize all runtime logs under BASEDIR/out/logs instead of /tmp
# Create the log directory early so subsequent echo output can be captured.
LOG_DIR="$BASEDIR/out/logs"
mkdir -p "$LOG_DIR"
REAL_KERNEL_LOG="$LOG_DIR/kernel.log"
REAL_LOGCAT_LOG="$LOG_DIR/logcat.log"
chmod -R 777 "$LOG_DIR" 2>/dev/null || true
# Script-wide logfile (captures all stdout/stderr from this script while
# still printing to the console). We use process substitution with tee so
# normal console output is preserved and a persistent copy is kept.
LOGFILE="$LOG_DIR/emulator_start.log"
exec > >(tee -a "$LOGFILE") 2> >(tee -a "$LOGFILE" >&2)

# Ensure SSL keylog file so TLS keys from processes that honor SSLKEYLOGFILE
# (e.g. BoringSSL/OpenSSL-based apps) are written to a persistent file. We
# place it under BASEDIR/pcaps so it lives with captures.
SSLKEY_DIR="$BASEDIR/out/pcaps"
SSLKEYFILE="$SSLKEY_DIR/out/sslkeylog.log"
mkdir -p "$SSLKEY_DIR"
touch "$SSLKEYFILE" 2>/dev/null || true
# make it readable/writable by the current user only
chmod 777 "$SSLKEYFILE" 2>/dev/null || true
export SSLKEYLOGFILE="$SSLKEYFILE"
echo "SSLKEYLOGFILE set -> $SSLKEYLOGFILE"

echo "Log directory set -> $LOG_DIR"
chmod -R 777 "/tmp/"

setup_stop_existing_emulator() {
  pkill -f "emulator -avd"
  pkill -f "socat -d tcp-listen:5555"
  pkill -f "socat -d tcp-listen:8554"
  pkill -f "pulseaudio"
  pkill -f "tail --retry -f $LOG_DIR/goldfish_rtc_0"
  pkill -f "cat $LOG_DIR/kernel.log"
  pkill -f "cat $LOG_DIR/logcat.log"
}

setup_pulse_audio() {
  mkdir -p /root/.config/pulse
  export PULSE_SERVER=unix:/tmp/pulse-socket

  # Ensure log file exists so tail -F won't complain about missing file
  touch "$LOG_DIR/pulseverbose.log"

  # start pulseaudio in daemon mode, but tolerate failures
  pulseaudio -D -vvvv --log-time=1 --log-target=newfile:$LOG_DIR/pulseverbose.log --exit-idle-time=-1 2>>$LOG_DIR/pulseverbose.err || true
  PA_PID=$!
  # give pulse some time to initialize and poll using pactl
  MAX_TRIES=10
  TRY=0
  AUDIO_OK=0
  while [[ $TRY -lt $MAX_TRIES ]]; do
    sleep 0.5
    if pactl list 1>/dev/null 2>/dev/null; then
      AUDIO_OK=1
      break
    fi
    TRY=$((TRY+1))
  done

  if [[ $AUDIO_OK -eq 1 && $PA_PID -ne 0 ]]; then
    # Use tail -F (follow and retry) and suppress stderr to avoid noisy messages
    tail -F "$LOG_DIR/pulseverbose.log" -n +1 2>/dev/null | sed -u 's/^/pulse: /g' &
    PIDS+=($!)
  else
    echo "Warning: pulseaudio did not become available; audio disabled for this run." >&2
    # collect pulseaudio stderr for diagnostics but do not abort
    if [[ -s "$LOG_DIR/pulseverbose.err" ]]; then
      echo "pulseaudio stderr (first 20 lines):"
      head -n 20 "$LOG_DIR/pulseverbose.err" || true
    fi
  fi
}

setup_logger_forwarding() {
  : > "$REAL_KERNEL_LOG" 2>/dev/null || true
  : > "$REAL_LOGCAT_LOG" 2>/dev/null || true

  # For goldfish RTC data, use tail -F to avoid immediate failure if file not present yet
  # Redirect stderr to /dev/null to avoid noisy messages when emulator isn't producing the file yet
  tail -F "$LOG_DIR/goldfish_rtc_0" 2>/dev/null | sed -u 's/^/video: /g' &
  PIDS+=($!)

  # Stream the persistent files to stdout (prefix lines) so they appear in docker logs
  (tail -F "$REAL_KERNEL_LOG" 2>/dev/null | sed -u 's/^/kernel: /g') &
  PIDS+=($!)

  (tail -F "$REAL_LOGCAT_LOG" 2>/dev/null | sed -u 's/^/logcat: /g') &
  PIDS+=($!)
}

setup_port_forwarding() {
  sleep 1
  # Redirect socat stderr to per-listener logs to keep console clean and capture diagnostics
  socat -d tcp-listen:5555,reuseaddr,fork tcp:127.0.0.1:5557 2>"$LOG_DIR/socat-5555.log" &
  PIDS+=($!)
  socat -d tcp-listen:8554,reuseaddr,fork tcp:127.0.0.1:8556 2>"$LOG_DIR/socat-8554.log" &
  PIDS+=($!)
}

setup_stop_existing_emulator

architecture=$(uname -m)

# Single-run mode: perform setup and start emulator once
# reset iteration PIDs so we only track processes started for this run
PIDS=()

setup_port_forwarding
setup_pulse_audio
setup_logger_forwarding

/android/sdk/platform-tools/adb -a -P 5037 nodaemon server &
PIDS+=($!)

if [[ $architecture == "x86_64" ]]; then
  AVD="x86_64"
  # Prepare command for emulator (fallback: write logs to persistent files)
  EMU_CMD=(/android/sdk/emulator/emulator -avd "$AVD" -no-window -no-snapshot -ports "5556,5557" -grpc "8556" -skip-adb-auth -no-snapshot-save -wipe-data -show-kernel -logcat-output "$REAL_LOGCAT_LOG" -shell-serial "file:$REAL_KERNEL_LOG" -gpu swiftshader_indirect -turncfg "${TURN}" -qemu -append "panic=1")
elif [[ $architecture == "aarch64" ]]; then
  AVD="Arm64"
  # Prepare command for emulator (fallback: write logs to persistent files)
  EMU_CMD=(/android/sdk/emulator/emulator -avd "$AVD" -no-window -no-snapshot -ports "5556,5557" -grpc "8556" -skip-adb-auth -no-snapshot-save -logcat "*:V" -show-kernel -logcat-output "$REAL_LOGCAT_LOG" -shell-serial "file:$REAL_KERNEL_LOG" -gpu swiftshader_indirect -qemu -append "panic=1" -cpu max -machine gic-version=max)
else
  echo "Unsupported architecture"
  cleanup
fi
# Start tcpdump capture on host once emulator is launched. Use a pcaps subdir next to this script.
if [[ $ENABLE_TCPDUMP -eq 1 ]]; then
  if [[ -x "$BASEDIR/tcpdump.sh" ]]; then
    echo "Starting tcpdump helper to capture emulator traffic..."
    # start will wait for adb boot completion inside tcpdump.sh; provide outdir under emulator/pcaps
    "$BASEDIR/tcpdump.sh" start "$BASEDIR/pcaps" >/dev/null 2>&1 || true
  else
    echo "tcpdump helper not found at $BASEDIR/tcpdump.sh; skipping tcpdump start." >&2
  fi
fi

# Launch emulator in background (single-run)
# Show the full command we're about to run for easier debugging
echo "EMU_CMD: $(printf '%q ' "${EMU_CMD[@]}")"
echo "Starting emulator in background (logs -> $LOG_DIR/emulator_${AVD}.log)"
# Redirect emulator stdout/stderr to a log file under LOG_DIR for diagnostics
# shellcheck disable=SC2086
${EMU_CMD[@]} >"$LOG_DIR/emulator_${AVD}.log" 2>&1 &
emulator_pid=$!

# Quick early health-check: watch the emulator wrapper log for immediate fatal errors
EARLY_LOG_DEADLINE=$((SECONDS+8))
while [[ $SECONDS -lt $EARLY_LOG_DEADLINE ]]; do
  if grep -E "cannot use stdio by multiple character devices|QEMU main loop exits abnormally|Unable to spawn process|kvm run failed" "$LOG_DIR/emulator_${AVD}.log" >/dev/null 2>&1; then
    echo "Detected immediate emulator/QEMU startup failure (see $LOG_DIR/emulator_${AVD}.log). Restarting script..."
    echo "--- emulator log tail ---"
    tail -n 200 "$LOG_DIR/emulator_${AVD}.log" || true
    # Best-effort kill
    kill -TERM "$emulator_pid" 2>/dev/null || true
    sleep 1
    # Respect a restart cooldown to avoid rapid restart loops. Use
    # LAST_RESTART_TS env var (seconds since epoch) if present.
    RESTART_COOLDOWN=${RESTART_COOLDOWN:-10}
    NOW=$(date +%s)
    LAST=${LAST_RESTART_TS:-0}
    ELAPSED=$((NOW - LAST))
    if [[ $ELAPSED -lt $RESTART_COOLDOWN ]]; then
      SLEEP=$((RESTART_COOLDOWN - ELAPSED))
      echo "Restart cooldown active; sleeping $SLEEP s before restart..."
      sleep "$SLEEP"
    fi
    export LAST_RESTART_TS=$(date +%s)
    # Enforce max restart limit to avoid endless respawn loops
    MAX_RESTARTS=${MAX_RESTARTS:-5}
    RESTART_COUNT=${RESTART_COUNT:-0}
    NEXT_RESTART_COUNT=$((RESTART_COUNT + 1))
    if [[ $NEXT_RESTART_COUNT -gt $MAX_RESTARTS ]]; then
      echo "Maximum restart limit ($MAX_RESTARTS) reached (count=$RESTART_COUNT). Not restarting."
      exit 3
    fi
    export RESTART_COUNT=$NEXT_RESTART_COUNT
    exec "$0" "${ORIG_ARGS[@]}"
  fi
  sleep 0.5
done

# Attempt to locate qemu subprocess and monitor it. If a fatal KVM error
# appears in the log while running, restart the script.
QEMU_PID=""
FIND_DEADLINE=$((SECONDS+10))
while [[ -z "$QEMU_PID" && SECONDS -lt $FIND_DEADLINE ]]; do
  for c in $(pgrep -P "$emulator_pid" 2>/dev/null || true); do
    cmd=$(ps -p "$c" -o comm= 2>/dev/null || true)
    if [[ "$cmd" == *qemu* ]]; then
      QEMU_PID=$c
      break
    fi
  done
  if [[ -z "$QEMU_PID" ]]; then
    for c in $(pgrep -f 'qemu-system' 2>/dev/null || true); do
      cmdline=$(ps -p "$c" -o args= 2>/dev/null || true)
      if [[ "$cmdline" == *"$AVD"* || "$cmdline" == *qemu-system-* ]]; then
        QEMU_PID=$c
        break
      fi
    done
  fi
  sleep 0.5
done

if [[ -n "$QEMU_PID" ]]; then
  echo "Monitoring qemu subprocess pid $QEMU_PID (emulator wrapper pid $emulator_pid)"
  while ps -p "$QEMU_PID" >/dev/null 2>&1; do
    # Check for KVM error in recent log lines
    if tail -n 200 "$LOG_DIR/emulator_${AVD}.log" 2>/dev/null | grep -qi "kvm run failed"; then
      echo "Detected KVM error in emulator log; restarting script..."
      kill -TERM "$QEMU_PID" 2>/dev/null || true
      kill -TERM "$emulator_pid" 2>/dev/null || true
      sleep 1
      # Respect restart cooldown to avoid rapid respawn loops
      RESTART_COOLDOWN=${RESTART_COOLDOWN:-10}
      NOW=$(date +%s)
      LAST=${LAST_RESTART_TS:-0}
      ELAPSED=$((NOW - LAST))
      if [[ $ELAPSED -lt $RESTART_COOLDOWN ]]; then
        SLEEP=$((RESTART_COOLDOWN - ELAPSED))
        echo "Restart cooldown active; sleeping $SLEEP s before restart..."
        sleep "$SLEEP"
      fi
      export LAST_RESTART_TS=$(date +%s)
      # Enforce max restart limit to avoid endless respawn loops
      MAX_RESTARTS=${MAX_RESTARTS:-5}
      RESTART_COUNT=${RESTART_COUNT:-0}
      NEXT_RESTART_COUNT=$((RESTART_COUNT + 1))
      if [[ $NEXT_RESTART_COUNT -gt $MAX_RESTARTS ]]; then
        echo "Maximum restart limit ($MAX_RESTARTS) reached (count=$RESTART_COUNT). Not restarting."
        exit 3
      fi
      export RESTART_COUNT=$NEXT_RESTART_COUNT
      exec "$0" "${ORIG_ARGS[@]}"
    fi
    sleep 1
  done
  echo "qemu subprocess pid $QEMU_PID has exited."
else
  echo "Could not locate qemu subprocess for wrapper pid $emulator_pid; falling back to waiting on wrapper."
  wait "$emulator_pid"
  EMU_EXIT_STATUS=$?
  if [[ $EMU_EXIT_STATUS -eq 0 ]]; then
    echo "Emulator wrapper process (pid $emulator_pid) exited normally (exit code 0)."
  else
    if [[ $EMU_EXIT_STATUS -gt 128 ]]; then
      SIG=$((EMU_EXIT_STATUS - 128))
      echo "Emulator wrapper (pid $emulator_pid) terminated by signal $SIG (exit status $EMU_EXIT_STATUS)."
    else
      echo "Emulator wrapper (pid $emulator_pid) stopped with exit code $EMU_EXIT_STATUS."
    fi
  fi
fi

# Proceed with cleanup after VM exit
echo "Emulator stopped; cleaning up..."
if [[ $ENABLE_TCPDUMP -eq 1 && -x "$BASEDIR/tcpdump.sh" ]]; then
  "$BASEDIR/tcpdump.sh" stop >/dev/null 2>&1 || true
fi
if [[ ${#PIDS[@]} -gt 0 ]]; then
  kill -TERM "${PIDS[@]}" 2>/dev/null || true
  sleep 1
  kill -KILL "${PIDS[@]}" 2>/dev/null || true
fi
emulator_pid=0
echo "Exiting after single run."
exit 0

