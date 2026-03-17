#!/bin/bash

# All background pids for the current iteration
PIDS=()
emulator_pid=0
BASEDIR="$(cd "$(dirname "$0")" && pwd)"

# Ensure SSL keylog file so TLS keys from processes that honor SSLKEYLOGFILE
# (e.g. BoringSSL/OpenSSL-based apps) are written to a persistent file. We
# place it under BASEDIR/pcaps so it lives with captures.
SSLKEY_DIR="$BASEDIR/pcaps"
SSLKEYFILE="$SSLKEY_DIR/sslkeylog.log"
mkdir -p "$SSLKEY_DIR"
touch "$SSLKEYFILE" 2>/dev/null || true
# make it readable/writable by the current user only
chmod 600 "$SSLKEYFILE" 2>/dev/null || true
export SSLKEYLOGFILE="$SSLKEYFILE"
echo "SSLKEYLOGFILE set -> $SSLKEYLOGFILE"

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
  if [[ -x "$BASEDIR/tcpdump.sh" ]]; then
    "$BASEDIR/tcpdump.sh" stop >/dev/null 2>&1 || true
  fi

  exit 0
}

trap cleanup SIGINT SIGTERM EXIT

setup_stop_existing_emulator() {
  pkill -f "emulator -avd"
  pkill -f "socat -d tcp-listen:5555"
  pkill -f "socat -d tcp-listen:8554"
  pkill -f "pulseaudio"
  pkill -f "tail --retry -f /tmp/android-unknown/goldfish_rtc_0"
  pkill -f "cat /tmp/android-unknown/kernel.log"
  pkill -f "cat /tmp/android-unknown/logcat.log"
}

setup_pulse_audio() {
  mkdir -p /root/.config/pulse
  export PULSE_SERVER=unix:/tmp/pulse-socket

  # Ensure log file exists so tail -F won't complain about missing file
  touch /tmp/pulseverbose.log

  # start pulseaudio in daemon mode, but tolerate failures
  pulseaudio -D -vvvv --log-time=1 --log-target=newfile:/tmp/pulseverbose.log --exit-idle-time=-1 2>>/tmp/pulseverbose.err || true
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
    tail -F /tmp/pulseverbose.log -n +1 2>/dev/null | sed -u 's/^/pulse: /g' &
    PIDS+=($!)
  else
    echo "Warning: pulseaudio did not become available; audio disabled for this run." >&2
    # collect pulseaudio stderr for diagnostics but do not abort
    if [[ -s /tmp/pulseverbose.err ]]; then
      echo "pulseaudio stderr (first 20 lines):"
      head -n 20 /tmp/pulseverbose.err || true
    fi
  fi
}

setup_logger_forwarding() {
  mkdir -p /tmp/android-unknown
  rm -f /tmp/android-unknown/kernel.log /tmp/android-unknown/logcat.log
  # create FIFOs for kernel and logcat streams; ignore errors if they already exist
  mkfifo /tmp/android-unknown/kernel.log 2>/dev/null || true
  mkfifo /tmp/android-unknown/logcat.log 2>/dev/null || true
  # For goldfish RTC data, use tail -F to avoid immediate failure if file not present yet
  # Redirect stderr to /dev/null to avoid noisy messages when emulator isn't producing the file yet
  tail -F /tmp/android-unknown/goldfish_rtc_0 2>/dev/null | sed -u 's/^/video: /g' &
  PIDS+=($!)
  # read from FIFOs
  (cat /tmp/android-unknown/kernel.log 2>/dev/null | sed -u 's/^/kernel: /g') &
  PIDS+=($!)
  (cat /tmp/android-unknown/logcat.log 2>/dev/null | sed -u 's/^/logcat: /g') &
  PIDS+=($!)
}

setup_port_forwarding() {
  sleep 1
  # Redirect socat stderr to per-listener logs to keep console clean and capture diagnostics
  socat -d tcp-listen:5555,reuseaddr,fork tcp:127.0.0.1:5557 2>/tmp/socat-5555.log &
  PIDS+=($!)
  socat -d tcp-listen:8554,reuseaddr,fork tcp:127.0.0.1:8556 2>/tmp/socat-8554.log &
  PIDS+=($!)
}

setup_stop_existing_emulator

architecture=$(uname -m)

while true; do
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
    /android/sdk/emulator/emulator -avd $AVD -no-window -no-snapshot -ports "5556,5557" -grpc "8556" -skip-adb-auth -no-snapshot-save -wipe-data -show-kernel -logcat-output "/tmp/android-unknown/logcat.log" -shell-serial "file:/tmp/android-unknown/kernel.log" -no-boot-anim -gpu swiftshader_indirect -turncfg "${TURN}" -qemu -append "panic=1" &
    emulator_pid=$!
  elif [[ $architecture == "aarch64" ]]; then
    AVD="Arm64"
    /android/sdk/emulator/emulator -avd $AVD -no-window -no-snapshot -ports "5556,5557" -grpc "8556" -skip-adb-auth -no-snapshot-save -logcat "*:V" -show-kernel -logcat-output "/tmp/android-unknown/logcat.log" -shell-serial "file:/tmp/android-unknown/kernel.log" -no-boot-anim -gpu swiftshader_indirect -qemu -append "panic=1" -cpu max -machine gic-version=max &
    emulator_pid=$!
  else
    echo "Unsupported architecture"
    cleanup
  fi

  # Start tcpdump capture on host once emulator is launched. Use a pcaps subdir next to this script.
  if [[ -x "$BASEDIR/tcpdump.sh" ]]; then
    echo "Starting tcpdump helper to capture emulator traffic..."
    # start will wait for adb boot completion inside tcpdump.sh; provide outdir under emulator/pcaps
    "$BASEDIR/tcpdump.sh" start "$BASEDIR/pcaps" >/dev/null 2>&1 || true
  else
    echo "tcpdump helper not found at $BASEDIR/tcpdump.sh; skipping tcpdump start." >&2
  fi

  # wait for emulator to stop; when it does, kill the iteration subprocesses and restart
  wait "$emulator_pid" 2>/dev/null || true

  echo "Emulator crashed or exited, stopping subprocesses and restarting..."
  # Stop tcpdump associated with this emulator run
  if [[ -x "$BASEDIR/tcpdump.sh" ]]; then
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

  sleep 10
done
