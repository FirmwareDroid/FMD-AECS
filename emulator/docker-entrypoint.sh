#!/bin/sh
set -e

# Docker entrypoint for emulator container
# Starts sshd and fail2ban, then after a short delay starts emulator_start.sh in the background.

# Allow overriding the delay (seconds) via EMULATOR_START_DELAY env var
DELAY=${EMULATOR_START_DELAY:-5}

# Ensure run dirs
mkdir -p /var/run/sshd

# Start sshd (best-effort). If it fails, log but continue so container can still be used.
if command -v /usr/sbin/sshd >/dev/null 2>&1; then
  echo "Starting sshd..."
  /usr/sbin/sshd || echo "Warning: sshd failed to start"
else
  echo "sshd not installed"
fi

# Start fail2ban if available
if command -v service >/dev/null 2>&1; then
  echo "Starting fail2ban (if configured)..."
  ervice fail2ban start || echo "Warning: fail2ban did not start or is not configured"
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

# Start emulator script in background and capture logs
if [ -f /android/emulator_start.sh ]; then
  echo "Starting emulator_start.sh in background (logs -> /var/log/emulator_start.log)"
  # ensure log dir exists
  mkdir -p /var/log
  /android/emulator_start.sh > /var/log/emulator_start.log 2>&1 &
else
  echo "No /android/emulator_start.sh found; skipping emulator start"
fi

# If user provided a command, exec it. Otherwise keep container alive by tailing the logs.
if [ "$#" -gt 0 ]; then
  exec "$@"
else
  # keep container alive; provide ability to see logs
  tail -F /var/log/emulator_start.log 2>/dev/null || tail -f /dev/null
fi

