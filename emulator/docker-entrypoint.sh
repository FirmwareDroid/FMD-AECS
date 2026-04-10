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

# Start emulator script in foreground (so it becomes PID 1)
if [ -f "$EMULATOR_START_SCRIPT" ]; then
  echo "Starting ${EMULATOR_START_SCRIPT} in foreground (logs -> ${EMULATOR_START_LOG})"
  # ensure log dir exists
  mkdir -p "$(dirname "$EMULATOR_START_LOG")"
  chmod +x "$EMULATOR_START_SCRIPT" || true

  # Simply start the emulator start script and pipe output to the configured log.
  # Do not attempt to detect or kill existing instances; entrypoint's job is only
  # to start the service once.
  exec /bin/bash -c "\"$EMULATOR_START_SCRIPT\" 2>&1 | tee -a \"$EMULATOR_START_LOG\""
fi
