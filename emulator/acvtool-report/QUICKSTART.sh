#!/usr/bin/env bash
# Quick start guide for parallel ACVTool report generation

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cat <<'EOF'

╔════════════════════════════════════════════════════════════════════════════╗
║           ACVTool Parallel Report Runner - Quick Start Guide               ║
╚════════════════════════════════════════════════════════════════════════════╝

PREREQUISITES:
  1. Build the Docker image:
     $ docker build -t acvtool-report:2.3.6 ./docker/acvtool-report

  2. Ensure your emulator_out directory exists:
     $ ls -la ./data/01_journal_extension/emulator_out/

═══════════════════════════════════════════════════════════════════════════════

OPTION 1: PYTHON THREAD-BASED RUNNER (Recommended for most use cases)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This runs multiple Docker containers in parallel using a thread pool.

  # Preview what would run (dry-run):
  $ ./run_reports.sh --dry-run

  # Run with 4 parallel workers (default):
  $ ./run_reports.sh

  # Run with 8 workers (adjust based on CPU/memory):
  $ ./run_reports.sh --max-workers 8

  # Save results to JSON:
  $ ./run_reports.sh --output results.json

  # Custom path to emulator_out:
  $ ./run_reports.sh --emulator-out /path/to/emulator_out

═══════════════════════════════════════════════════════════════════════════════

OPTION 2: DOCKER COMPOSE (Process manager approach)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This generates a docker-compose.yml and manages all containers as a stack.

  # Generate docker-compose.yml:
  $ python3 generate_compose.py --output docker-compose.yml

  # Start all containers in background:
  $ docker-compose up -d

  # Watch logs from all containers:
  $ docker-compose logs -f

  # Check status:
  $ docker-compose ps

  # Stop all containers:
  $ docker-compose down

═══════════════════════════════════════════════════════════════════════════════

COMPARISON:

  Thread-based Runner (run_parallel.py):
    ✓ Simple, single command
    ✓ Built-in progress reporting
    ✓ JSON output for analysis
    ✓ Automatic cleanup
    ✗ Less visibility into individual containers

  Docker Compose:
    ✓ Standard Docker tool
    ✓ Full container lifecycle management
    ✓ Per-container logs accessible
    ✗ Need to generate yml first
    ✗ Manual cleanup of failed/stuck containers

═══════════════════════════════════════════════════════════════════════════════

RESOURCE TUNING:

  For Docker Compose, edit docker-compose.yml to add resource limits:

    deploy:
      resources:
        limits:
          cpus: '2'           # CPU cap per container
          memory: 4G          # Memory cap per container

  For run_parallel.py, adjust --max-workers based on:
    - Available CPU cores (usually: CPU_cores / 2)
    - Available RAM (usually: RAM_GB / 4)

  Example on 8-core, 16GB system:
    $ ./run_reports.sh --max-workers 8

═══════════════════════════════════════════════════════════════════════════════

TROUBLESHOOTING:

  Container fails with "FileNotFoundError":
    → Check that emulator_out directory structure matches:
      emulator_out/
        <firmware_id>_v<sdk>.../
          acv_snaps/
            <package_name>/
              ec_files/

  Container timeout (1 hour):
    → Increase timeout in run_parallel.py (line ~140)
    → Run fewer workers to give each more time

  Out of disk space:
    → Clean up failed containers: docker system prune -a
    → Check volume usage: du -sh data/01_journal_extension/

═══════════════════════════════════════════════════════════════════════════════

NEXT STEPS:

  1. Choose your approach (Thread-based or Compose)
  2. Do a dry-run first: ./run_reports.sh --dry-run
  3. Monitor progress: docker ps
  4. Check reports in: data/01_journal_extension/emulator_out/<fw>/acv_reports/

═══════════════════════════════════════════════════════════════════════════════

For more details, see the README.md in this directory.

EOF

