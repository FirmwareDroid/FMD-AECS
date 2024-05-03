#!/bin/bash

setup_stop_existing_emulator() {
  # Stops the existing emulator
  pkill -f "emulator -avd"
  pkill -f "socat -d tcp-listen:5555"
  pkill -f "socat -d tcp-listen:8554"
  pkill -f "pulseaudio"
  pkill -f "tail --retry -f /tmp/android-unknown/goldfish_rtc_0"
  pkill -f "cat /tmp/android-unknown/kernel.log"
  pkill -f "cat /tmp/android-unknown/logcat.log"
}

setup_pulse_audio() {
  # Setups pulse audio for the emulator
  mkdir -p /root/.config/pulse
  export PULSE_SERVER=unix:/tmp/pulse-socket
  pulseaudio -D -vvvv --log-time=1 --log-target=newfile:/tmp/pulseverbose.log --log-time=1 --exit-idle-time=-1
  tail -f /tmp/pulseverbose.log -n +1 | sed -u 's/^/pulse: /g' &
  pactl list || exit 1
}

setup_logger_forwarding() {
  # Forward logcat and kernel logs to stdout
  mkdir /tmp/android-unknown
  mkfifo /tmp/android-unknown/kernel.log
  mkfifo /tmp/android-unknown/logcat.log
  tail --retry -f /tmp/android-unknown/goldfish_rtc_0 | sed -u 's/^/video: /g' &
  cat /tmp/android-unknown/kernel.log | sed -u 's/^/kernel: /g' &
  cat /tmp/android-unknown/logcat.log | sed -u 's/^/logcat: /g' &
}

setup_port_forwarding() {
  # Setups port forwarding for adb and gRPC
  sleep 1
  # Forward adb port
  socat -d tcp-listen:5555,reuseaddr,fork tcp:127.0.0.1:5557 &
  # Forward gRPC port
  socat -d tcp-listen:8554,reuseaddr,fork tcp:127.0.0.1:8556 &
}

setup_stop_existing_emulator
architecture=$(uname -m)
setup_port_forwarding
setup_pulse_audio

while true; do
  setup_logger_forwarding

  /android/sdk/platform-tools/adb start-server &

  if [[ $architecture == "x86_64" ]]; then
    AVD="x86_64"
    /android/sdk/emulator/emulator -avd $AVD -no-window -no-snapshot -ports "5556,5557" -grpc "8556" -skip-adb-auth -no-snapshot-save -wipe-data -show-kernel -logcat-output "/tmp/android-unknown/logcat.log" -shell-serial "file:/tmp/android-unknown/kernel.log" -no-boot-anim -gpu swiftshader_indirect -turncfg "${TURN}" -qemu -append "panic=1" &
    pid=$!
  elif [[ $architecture == "aarch64" ]]; then
    AVD="Arm64"
    /android/sdk/emulator/emulator -avd $AVD -no-window -no-snapshot -ports "5556,5557" -grpc "8556" -skip-adb-auth -no-snapshot-save -logcat "*:V" -show-kernel -logcat-output "/tmp/android-unknown/logcat.log" -shell-serial "file:/tmp/android-unknown/kernel.log" -no-boot-anim -wipe-data -gpu swiftshader_indirect -qemu -append "panic=1" -cpu max -machine gic-version=max &
    pid=$!
  else
    echo "Unsupported architecture"
    exit 1
  fi

  wait $pid

  echo "Emulator crashed, restarting..."
  sleep 10
done
