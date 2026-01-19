#!/bin/sh
set -e

# Docker entrypoint for emulator container
# Starts sshd and fail2ban, then after a short delay starts emulator_start.sh in the background.

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

# Ensure emulator script is executable
if [ -f /android/emulator_start.sh ]; then
  chmod +x /android/emulator_start.sh || true
fi

# Delay to allow container networking and other services to settle
if [ "$DELAY" -gt 0 ] 2>/dev/null; then
  echo "Waiting $DELAY second(s) before starting emulator script..."
  sleep "$DELAY"
fi

# Start emulator script in foreground (so it becomes PID 1) if present
if [ -f /android/emulator_start.sh ]; then
  echo "Starting emulator_start.sh in foreground (logs -> /var/log/emulator_start.log)"
  # ensure log dir exists
  mkdir -p /var/log
  chmod +x /android/emulator_start.sh || true
  # Run the emulator start script and tee output to the log file and stdout so 'docker logs' shows it
  exec /bin/sh -c "/android/emulator_start.sh 2>&1 | tee -a /var/log/emulator_start.log"
else
  echo "No /android/emulator_start.sh found; falling back to tailing a log to keep container alive"
  # keep container alive; provide ability to see logs
  tail -F /var/log/emulator_start.log 2>/dev/null || tail -f /dev/null
fi
