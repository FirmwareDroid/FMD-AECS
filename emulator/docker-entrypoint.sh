#!/bin/sh
set -e

# Docker entrypoint for emulator container
# Starts sshd and fail2ban, then after a short delay starts emulator_start.sh in the background.


# Allow overriding the delay (seconds) via EMULATOR_START_DELAY env var
DELAY=${EMULATOR_START_DELAY:-5}

# Ensure run dirs
mkdir -p /var/run/sshd

# Ensure container tmp area for host-mounted Android unknown files exists and is readable
# Docker users may mount a host volume onto /tmp/android-unknown; make sure the directory
# exists and is world-readable so both the host user and container users can access it.
mkdir -p /tmp/android-unknown
# Set permissive permissions so mounted host files are accessible; host mount may override
# permissions, but when the directory is created here ensure it is readable/executable by all.
chmod -R 0777 /tmp/android-unknown || true


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
# Path to the emulator setup helper script (starts socat, pulseaudio, log tails)
EMULATOR_SETUP_SCRIPT="${EMULATOR_SETUP_SCRIPT:-/android/testing_service/emulator_setup.sh}"
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

  # Start optional external setup helper in background (if present)
  if [ -f "$EMULATOR_SETUP_SCRIPT" ]; then
    echo "Starting emulator setup helper: $EMULATOR_SETUP_SCRIPT"
    chmod +x "$EMULATOR_SETUP_SCRIPT" || true
    # run in background, send its output to the same log file
    /bin/bash -c "\"$EMULATOR_SETUP_SCRIPT\" 2>&1 | tee -a \"$EMULATOR_START_LOG\"" &
    sleep 0.5
  fi

  # Start adb scrcpy watcher (if available) in background so devices get the
  # scrcpy server pushed and started automatically. The script is installed
  # into /usr/local/bin by the Dockerfile. Logs are appended to
  # /var/log/adb_scrcpy_watcher.log by default.
  ADB_SCRCPY_WATCHER_SCRIPT="${ADB_SCRCPY_WATCHER_SCRIPT:-/usr/local/bin/adb_push_and_start_scrcpy.py}"
  ADB_SCRCPY_WATCHER_LOG="${ADB_SCRCPY_WATCHER_LOG:-/android/log/adb_scrcpy_watcher.log}"
  if command -v python3 >/dev/null 2>&1 && [ -f "$ADB_SCRCPY_WATCHER_SCRIPT" ]; then
    echo "Starting adb scrcpy watcher: $ADB_SCRCPY_WATCHER_SCRIPT -> $ADB_SCRCPY_WATCHER_LOG"
    mkdir -p "$(dirname "$ADB_SCRCPY_WATCHER_LOG")"
    chmod +x "$ADB_SCRCPY_WATCHER_SCRIPT" || true
    LOCAL_JAR_PATH="/android/scrcpy/scrcpy-server.jar"
    /bin/bash -c "python3 -u \"$ADB_SCRCPY_WATCHER_SCRIPT\" --local-jar \"$LOCAL_JAR_PATH\" 2>&1 | tee -a \"$ADB_SCRCPY_WATCHER_LOG\"" &
    sleep 0.5
  else
    echo "adb scrcpy watcher not found or python3 missing: $ADB_SCRCPY_WATCHER_SCRIPT"
  fi

  # Launch the Python-based launcher which starts and watches the emulator
  # helper script. The launcher will restart the emulator_start.sh on
  # failures and applies cooldowns / limits.
  if command -v python3 >/dev/null 2>&1 && [ -f "/android/testing_service/emulator_launcher.py" ]; then
    echo "Starting emulator launcher (python) -> /android/testing_service/emulator_launcher.py"
    chmod +x "/android/testing_service/emulator_launcher.py" || true
    exec python3 -u "/android/testing_service/emulator_launcher.py" --script "$EMULATOR_START_SCRIPT" --log "$EMULATOR_START_LOG"
  else
    # Fallback: run the shell script directly if python3 or launcher missing
    echo "Warning: python3 or emulator_launcher.py not found, running emulator_start.sh directly (no auto-restart)"
    exec /bin/bash -c "\"$EMULATOR_START_SCRIPT\" 2>&1 | tee -a \"$EMULATOR_START_LOG\""
  fi
fi
