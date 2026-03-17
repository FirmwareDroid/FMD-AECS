#!/bin/sh
set -e

# Docker entrypoint for emulator container
# Starts sshd and fail2ban, then after a short delay starts emulator_start.sh in the background.

# Redirect host HTTP(S) traffic to emulator web server port 8080 for mitmproxy access
#iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080
#iptables -t nat -A PREROUTING -p tcp --dport 443 -j REDIRECT --to-port 8080

# Redirect all TCP / UDP traffic to mitmproxy
iptables -t nat -A PREROUTING -p tcp -j REDIRECT --to-port 8080
iptables -t nat -A PREROUTING -p udp -j REDIRECT --to-port 8080

# Allow overriding the delay (seconds) via EMULATOR_START_DELAY env var
DELAY=${EMULATOR_START_DELAY:-5}

# Ensure run dirs
mkdir -p /var/run/sshd


if [ -S /var/run/fail2ban/fail2ban.sock ]; then
  echo "Removing existing fail2ban socket before start: /var/run/fail2ban/fail2ban.sock"
  rm -f /var/run/fail2ban/fail2ban.sock || true
fi

# Start sshd (best-effort). If it fails, log but continue so container can still be used.
if command -v /usr/sbin/sshd >/dev/null 2>&1; then
  echo "Starting sshd..."
  /usr/sbin/sshd || echo "Warning: sshd failed to start"
else
  echo "sshd not installed"
fi

# Path to the emulator start script (override via env EMULATOR_START_SCRIPT)
EMULATOR_START_SCRIPT="${EMULATOR_START_SCRIPT:-/android/testing_service/emulator_start.sh}"
# Where to write emulator_start logs (override via env EMULATOR_START_LOG)
EMULATOR_START_LOG="${EMULATOR_START_LOG:-/var/log/emulator_start.log}"

# Ensure emulator script is executable
if [ -f "$EMULATOR_START_SCRIPT" ]; then
  chmod +x "$EMULATOR_START_SCRIPT" || true
fi

# Delay to allow container networking and other services to settle
if [ "$DELAY" -gt 0 ] 2>/dev/null; then
  echo "Waiting $DELAY second(s) before starting emulator script..."
  sleep "$DELAY"
fi

# Start emulator script in foreground (so it becomes PID 1) if present
if [ -f "$EMULATOR_START_SCRIPT" ]; then
  echo "Starting ${EMULATOR_START_SCRIPT} in foreground (logs -> ${EMULATOR_START_LOG})"
  # ensure log dir exists
  mkdir -p "$(dirname "$EMULATOR_START_LOG")"
  chmod +x "$EMULATOR_START_SCRIPT" || true

  # Check if emulator_start.sh is already running (prefer pgrep, fallback to ps+grep)
  ALREADY_RUNNING=1
  if command -v pgrep >/dev/null 2>&1; then
    if pgrep -f "$EMULATOR_START_SCRIPT" >/dev/null 2>&1; then
      ALREADY_RUNNING=0
    fi
  else
    if ps aux | grep "$(basename "$EMULATOR_START_SCRIPT")" >/dev/null 2>&1; then
      ALREADY_RUNNING=0
    fi
  fi

  if [ "$ALREADY_RUNNING" -eq 0 ]; then
    echo "${EMULATOR_START_SCRIPT} already running; stopping existing instance before restart."
    # Try graceful termination first (SIGTERM) using pkill if available
    if command -v pkill >/dev/null 2>&1; then
      pkill -f "$EMULATOR_START_SCRIPT" || true
    else
      # fallback to finding PIDs and killing
      PIDS_TO_KILL=$(ps aux | grep "$EMULATOR_START_SCRIPT" | grep -v grep | awk '{print $2}') || true
      for p in $PIDS_TO_KILL; do
        kill -TERM "$p" 2>/dev/null || true
      done
    fi

    # Also proactively stop known child processes that emulator_start.sh would have started
    echo "Stopping potential child processes: emulator qemu, socat, pulseaudio, tails"
    # kill emulator qemu processes
    pkill -f "emulator -avd" 2>/dev/null || true
    # kill socat listeners
    pkill -f "socat -d tcp-listen:5555" 2>/dev/null || true
    pkill -f "socat -d tcp-listen:8554" 2>/dev/null || true
    # kill pulseaudio instances
    pkill -f "pulseaudio" 2>/dev/null || true
    # kill tail/cat readers on FIFOs
    pkill -f "tail -F /tmp/android-unknown/goldfish_rtc_0" 2>/dev/null || true
    pkill -f "cat /tmp/android-unknown/kernel.log" 2>/dev/null || true
    pkill -f "cat /tmp/android-unknown/logcat.log" 2>/dev/null || true

    # Remove FIFOs/sockets that might block a fresh run
    rm -f /tmp/android-unknown/kernel.log /tmp/android-unknown/logcat.log 2>/dev/null || true
    rm -f /tmp/pulse-socket 2>/dev/null || true

    # Wait up to 15s for processes to exit
    WAIT=0
    while ( (command -v pgrep >/dev/null 2>&1 && pgrep -f "$EMULATOR_START_SCRIPT" >/dev/null 2>&1) || ( ! command -v pgrep >/dev/null 2>&1 && ps aux | grep "$EMULATOR_START_SCRIPT" | grep -v grep >/dev/null 2>&1 ) ) && [ $WAIT -lt 15 ]; do
      sleep 1
      WAIT=$((WAIT+1))
    done

    # If still running, force kill
    if command -v pgrep >/dev/null 2>&1 && pgrep -f "$EMULATOR_START_SCRIPT" >/dev/null 2>&1; then
      echo "Existing ${EMULATOR_START_SCRIPT} did not stop in time; forcing kill"
      pkill -9 -f "$EMULATOR_START_SCRIPT" || true
      sleep 1
    elif ! command -v pgrep >/dev/null 2>&1 && ps aux | grep "$EMULATOR_START_SCRIPT" | grep -v grep >/dev/null 2>&1; then
      echo "Existing ${EMULATOR_START_SCRIPT} did not stop in time; forcing kill (fallback)"
      PIDS_TO_KILL=$(ps aux | grep "$EMULATOR_START_SCRIPT" | grep -v grep | awk '{print $2}') || true
      for p in $PIDS_TO_KILL; do
        kill -9 "$p" 2>/dev/null || true
      done
      sleep 1
    fi

    echo "Starting fresh ${EMULATOR_START_SCRIPT} instance..."
    # Run the emulator start script and tee output to the log file and stdout so 'docker logs' shows it
    exec /bin/bash -c "\"$EMULATOR_START_SCRIPT\" 2>&1 | tee -a \"$EMULATOR_START_LOG\""
  else
    # Run the emulator start script and tee output to the log file and stdout so 'docker logs' shows it
    exec /bin/bash -c "\"$EMULATOR_START_SCRIPT\" 2>&1 | tee -a \"$EMULATOR_START_LOG\""
  fi
fi

tail -F "$EMULATOR_START_LOG" 2>/dev/null
