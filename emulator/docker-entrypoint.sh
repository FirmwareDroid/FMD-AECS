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
  # Attempt to start fail2ban, retry once after removing stale socket
  echo "Attempting to start fail2ban..."
  # Ensure any existing socket file is removed before start to avoid bind errors
  if [ -S /var/run/fail2ban/fail2ban.sock ]; then
    echo "Removing existing fail2ban socket before start: /var/run/fail2ban/fail2ban.sock"
    rm -f /var/run/fail2ban/fail2ban.sock || true
  fi
  if service fail2ban start >/tmp/fail2ban_start.out 2>&1; then
    echo "fail2ban started"
  else
    echo "Warning: initial attempt to start fail2ban failed — retrying after cleanup"
    # Dump start output for debugging
    echo "--- fail2ban start output (first attempt) ---"
    sed -n '1,200p' /tmp/fail2ban_start.out || true
    # Try quick cleanup of socket and retry
    rm -f /var/run/fail2ban/fail2ban.sock 2>/dev/null || true
    if service fail2ban start >/tmp/fail2ban_start_retry.out 2>&1; then
      echo "fail2ban started on retry"
    else
      echo "Warning: fail2ban did not start or is not configured"
      echo "--- fail2ban start output (retry) ---"
      sed -n '1,200p' /tmp/fail2ban_start_retry.out || true
    fi
  fi
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
  # If multiple arguments were provided treat them as an argv list and exec directly
  if [ "$#" -gt 1 ]; then
    exec "$@"
  else
    # Single argument might be a compound shell command (with &&, ;, | etc).
    # Use /bin/sh -c to evaluate it, which matches common Docker CLI usage when passing a string.
    exec /bin/sh -c "$1"
  fi
else
  # keep container alive; provide ability to see logs
  tail -F /var/log/emulator_start.log 2>/dev/null || tail -f /dev/null
fi

