#!/bin/bash

# All background pids for the current iteration
PIDS=()
emulator_pid=0
# Save original args so we can re-exec with the same arguments if requested
ORIG_ARGS=("$@")

# Behavior flags (can be set via command-line)
ATTACH=0   # if 1, run the emulator in the foreground attached to this terminal
REEXEC=0   # if 1, when emulator exits re-exec this script (restarts attached)
ENABLE_TCPDUMP=0  # if 1, start tcpdump helper to capture traffic

# Simple CLI parsing (we only remove the flags from $@ but keep ORIG_ARGS intact)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --attach)
      ATTACH=1
      shift
      ;;
    --reexec)
      REEXEC=1
      shift
      ;;
    --attach-reexec)
      ATTACH=1
      REEXEC=1
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
      echo "Usage: $0 [--attach] [--reexec]"
      echo "  --attach        Run emulator in foreground attached to this console"
      echo "  --reexec        When emulator exits, re-exec this script (keeps attachment)"
      echo "  --tcpdump       Enable tcpdump helper to capture emulator traffic (disabled by default)"
      echo "  --attach-reexec Combination of both"
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

cleanup() {
  trap - SIGINT SIGTERM EXIT

  echo "Cleaning up subprocesses..."
  [[ $emulator_pid -ne 0 ]] && kill -TERM "$emulator_pid" 2>/dev/null || true
  if [[ ${#PIDS[@]} -gt 0 ]]; then
    kill -TERM "${PIDS[@]}" 2>/dev/null || true
    sleep 1
    kill -KILL "${PIDS[@]}" 2>/dev/null || true
  fi

  # best-effort fallback kills for known long-running commands
  pkill -f "emulator -avd" 2>/dev/null || true
  pkill -f "socat -d tcp-listen:5555" 2>/dev/null || true
  pkill -f "socat -d tcp-listen:8554" 2>/dev/null || true
  pkill -f "pulseaudio" 2>/dev/null || true

  # Stop tcpdump started by this script (ignore errors)
  if [[ $ENABLE_TCPDUMP -eq 1 && -x "$BASEDIR/tcpdump.sh" ]]; then
    "$BASEDIR/tcpdump.sh" stop >/dev/null 2>&1 || true
  fi

  exit 0
}

trap cleanup SIGINT SIGTERM EXIT

# Cleanup iteration helpers without exiting (used before re-exec)
cleanup_iteration() {
  # don't run trap handlers
  trap - SIGINT SIGTERM EXIT

  echo "Cleaning up iteration subprocesses..."
  [[ $emulator_pid -ne 0 ]] && kill -TERM "$emulator_pid" 2>/dev/null || true
  if [[ ${#PIDS[@]} -gt 0 ]]; then
    kill -TERM "${PIDS[@]}" 2>/dev/null || true
    sleep 1
    kill -KILL "${PIDS[@]}" 2>/dev/null || true
  fi

  # best-effort fallback kills for known long-running commands
  pkill -f "emulator -avd" 2>/dev/null || true
  pkill -f "socat -d tcp-listen:5555" 2>/dev/null || true
  pkill -f "socat -d tcp-listen:8554" 2>/dev/null || true
  pkill -f "pulseaudio" 2>/dev/null || true

  # Stop tcpdump started by this script (ignore errors)
  if [[ $ENABLE_TCPDUMP -eq 1 && -x "$BASEDIR/tcpdump.sh" ]]; then
    "$BASEDIR/tcpdump.sh" stop >/dev/null 2>&1 || true
  fi
}

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
  mkdir -p "$LOG_DIR"
  # Ensure persistent log files exist and are writable. We'll make the emulator
  # write directly to these REAL_* files and stream them to stdout using tail -F.
  REAL_KERNEL_LOG="$LOG_DIR/kernel.log"
  REAL_LOGCAT_LOG="$LOG_DIR/logcat.log"
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

#/android/sdk/platform-tools/adb -a -P 5037 start-server &
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

# Launch emulator either attached to this terminal (foreground) or as background process
if [[ $ATTACH -eq 1 ]]; then
  echo "Starting emulator attached to this terminal (logs also written to $LOG_DIR/emulator_${AVD}.log)"
  # Run emulator in foreground and tee its output to the log file
  # shellcheck disable=SC2086
  ${EMU_CMD[@]} 2>&1 | tee "$LOG_DIR/emulator_${AVD}.log"
  # When the pipeline exits, emulator has stopped. Capture the emulator
  # process exit status from PIPESTATUS[0] so we can log a clear message.
  EMU_EXIT_STATUS=${PIPESTATUS[0]:-0}
  if [[ $EMU_EXIT_STATUS -eq 0 ]]; then
    echo "Emulator process exited normally (exit code 0)."
  else
    if [[ $EMU_EXIT_STATUS -gt 128 ]]; then
      SIG=$((EMU_EXIT_STATUS - 128))
      echo "Emulator process terminated by signal $SIG (exit status $EMU_EXIT_STATUS)."
    else
      echo "Emulator process stopped with exit code $EMU_EXIT_STATUS."
    fi
  fi
else
  echo "Starting emulator in background (logs -> $LOG_DIR/emulator_${AVD}.log)"
  # Redirect emulator stdout/stderr to a log file under LOG_DIR for diagnostics
  # shellcheck disable=SC2086
  ${EMU_CMD[@]} >"$LOG_DIR/emulator_${AVD}.log" 2>&1 &
  emulator_pid=$!
  # wait for emulator to stop; when it does, capture its exit status so we
  # can log whether it stopped cleanly or was terminated by a signal.
  wait "$emulator_pid"
  EMU_EXIT_STATUS=$?
  if [[ $EMU_EXIT_STATUS -eq 0 ]]; then
    echo "Emulator process (pid $emulator_pid) exited normally (exit code 0)."
  else
    if [[ $EMU_EXIT_STATUS -gt 128 ]]; then
      SIG=$((EMU_EXIT_STATUS - 128))
      echo "Emulator process (pid $emulator_pid) terminated by signal $SIG (exit status $EMU_EXIT_STATUS)."
    else
      echo "Emulator process (pid $emulator_pid) stopped with exit code $EMU_EXIT_STATUS."
    fi
  fi
fi

echo "Emulator crashed or exited, stopping subprocesses and restarting..."
# Stop tcpdump associated with this emulator run
if [[ $ENABLE_TCPDUMP -eq 1 && -x "$BASEDIR/tcpdump.sh" ]]; then
  "$BASEDIR/tcpdump.sh" stop >/dev/null 2>&1 || true
fi
# cleanup iteration processes but keep trap active for signals
if [[ ${#PIDS[@]} -gt 0 ]]; then
  kill -TERM "${PIDS[@]}" 2>/dev/null || true
  sleep 1
  kill -KILL "${PIDS[@]}" 2>/dev/null || true
fi

# ensure emulator pid cleared
emulator_pid=0

# Single-run behavior: stop subprocesses and exit (do not restart emulator)
echo "Exiting after single run."
exit 0
