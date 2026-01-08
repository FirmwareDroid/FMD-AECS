#!/bin/bash

# All background pids for the current iteration
PIDS=()
emulator_pid=0

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
  pulseaudio -D -vvvv --log-time=1 --log-target=newfile:/tmp/pulseverbose.log --log-time=1 --exit-idle-time=-1 &
  PIDS+=($!)
  tail -f /tmp/pulseverbose.log -n +1 | sed -u 's/^/pulse: /g' &
  PIDS+=($!)
  pactl list || exit 1
}

setup_logger_forwarding() {
  mkdir -p /tmp/android-unknown
  rm -f /tmp/android-unknown/kernel.log /tmp/android-unknown/logcat.log
  mkfifo /tmp/android-unknown/kernel.log
  mkfifo /tmp/android-unknown/logcat.log
  tail --retry -f /tmp/android-unknown/goldfish_rtc_0 | sed -u 's/^/video: /g' &
  PIDS+=($!)
  cat /tmp/android-unknown/kernel.log | sed -u 's/^/kernel: /g' &
  PIDS+=($!)
  cat /tmp/android-unknown/logcat.log | sed -u 's/^/logcat: /g' &
  PIDS+=($!)
}

setup_port_forwarding() {
  sleep 1
  socat -d tcp-listen:5555,reuseaddr,fork tcp:127.0.0.1:5557 &
  PIDS+=($!)
  socat -d tcp-listen:8554,reuseaddr,fork tcp:127.0.0.1:8556 &
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

  /android/sdk/platform-tools/adb start-server &
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

  # wait for emulator to stop; when it does, kill the iteration subprocesses and restart
  wait "$emulator_pid" 2>/dev/null || true

  echo "Emulator crashed or exited, stopping subprocesses and restarting..."
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
