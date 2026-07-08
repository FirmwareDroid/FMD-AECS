# ACVTool Report Container

Small, production-ready container image for running `acvtool` report generation in parallel workers.

**This implementation matches the workflow from `05_CoverageAnalysis.ipynb`**, ensuring consistency between local and containerized report generation.

It includes:
- `acvtool==2.3.6`
- `adb` and `aapt` from Debian packages (system tools)
- `acvpatcher` v1.0.8 binary (from official release)
- Container entrypoint with automatic working directory setup

## Build

```bash
docker build -t acvtool-report:2.3.6 ./docker/acvtool-report
```

If you are building on Apple Silicon and need amd64 compatibility in container runs:

```bash
docker build --platform linux/amd64 -t acvtool-report:2.3.6 ./docker/acvtool-report
```

## Documentation & Quick References

For detailed information, see:
- **[WORKFLOW.md](WORKFLOW.md)** – Complete workflow explanation, pickle file handling, troubleshooting
- **[QUICKSTART.sh](QUICKSTART.sh)** – Interactive guide (run: `bash QUICKSTART.sh`)
- **[prepare_working_dir.sh](prepare_working_dir.sh)** – Helper to stage pickle and EC files

## How It Works

The runner automatically:
1. Discovers firmware/packages with EC files in `emulator_out/`
2. Creates working directory structure (`.acv_wd/pickles/`, `ec/`, `report/`)
3. Stages EC files from package directories
4. Registers staged pickles with `acv cover-pickles` and runs `acv report` in parallel containers
5. Reports saved to `.acv_wd/report/` (then copied to `acv_reports/` if desired)

## Single Container Usage

```bash
docker run --rm \
  -v "$PWD/data/01_journal_extension/emulator_out:/work/input" \
  -e ACV_WD=/work/input/<firmware_id>/acv_snaps/<package_name>/.acv_wd \
  acvtool-report:2.3.6 \
  report <package_name>
```

## Pickle Files (Important)

ACVTool requires **pickle files** (code coverage profiles) to generate reports. EC files are automatically staged.

### Where to Get Pickles

1. **From Nexus** (if available):
   ```bash
   prepare_working_dir.sh \
     ./data/01_journal_extension/emulator_out \
     <firmware_id> \
     <package> \
     --download-pickles
   ```

2. **From your notebook**: Copy pickle files generated in `05_CoverageAnalysis.ipynb`

3. **Manual**: Copy `.pickle` files to `<firmware>/acv_snaps/<package>/.acv_wd/pickles/`

### Bulk Fetch for All Firmwares

Use this before `docker compose up` to populate `<firmware>/acv_snaps/pickle_files/`:

```bash
python3 docker/acvtool-report/fetch_pickles.py --dry-run
python3 docker/acvtool-report/fetch_pickles.py
python3 docker/acvtool-report/fetch_pickles.py --max-workers 8 --progress-interval 5
python3 docker/acvtool-report/fetch_pickles.py --dry-run --firmware 68af5a97 --samples 3
```

## Collect Reports

After containers complete, move all generated reports from `.acv_wd/report/` to centralized `acv_reports/`:

```bash
# Dry-run: preview what would be collected
python3 docker/acvtool-report/collect_reports.py --dry-run

# Collect all reports
python3 docker/acvtool-report/collect_reports.py

# Collect only specific firmware (substring match)
python3 docker/acvtool-report/collect_reports.py --firmware 68af5a97

# Skip packages that already have reports
python3 docker/acvtool-report/collect_reports.py --skip-existing
```

## One-Command Sequential Pipeline

Run the full flow firmware-by-firmware:
1) fetch pickles for one firmware,
2) generate compose file for that firmware,
3) run report containers,
4) collect reports into `<firmware>/acv_reports/`,
5) delete pickle zip + extracted `pickle_files`.

```bash
# Dry-run for one sample firmware
python3 docker/acvtool-report/run_firmware_pipeline.py --firmware 68af5a97 --samples 1 --dry-run

# Real run for one firmware
python3 docker/acvtool-report/run_firmware_pipeline.py --firmware 68af5a97 --samples 1

# Real run for first 3 matched firmware folders, continue on errors
python3 docker/acvtool-report/run_firmware_pipeline.py --firmware 68af --samples 3 --continue-on-error
```

See [WORKFLOW.md](WORKFLOW.md#pickle-file-handling) for more details.

## Parallel Batch Runner (Recommended)

### Option 1: Pythonic Runner (Thread-based)

Auto-discovers firmware folders and packages, spawns up to 4 parallel containers by default:

```bash
# Dry-run to see what would be executed
./run_reports.sh --dry-run

# Run with default 4 workers
./run_reports.sh

# Run with 8 parallel workers
./run_reports.sh --max-workers 8

# Save results to a JSON file
./run_reports.sh --output results.json

# Custom emulator_out path
./run_reports.sh --emulator-out /path/to/emulator_out

# Show help
./run_reports.sh --help
```

### Option 2: docker-compose (Process Manager)

Generate a `docker-compose.yml` and manage all containers as a single stack:

```bash
# Generate docker-compose.yml from discovered firmware/packages
python3 generate_compose.py --output docker-compose.yml

# Start all containers in background
docker-compose up -d

# Watch logs from all containers
docker-compose logs -f

# Check container status
docker-compose ps

# Stop all containers
docker-compose down
```

## Raw acv Commands

Access the underlying `acv` CLI directly inside the container:

```bash
docker run --rm acvtool-report:2.3.6 acv --help
docker run --rm acvtool-report:2.3.6 acv report --help
```

## Environment Variables

- `ACV_WD`: Path to acvtool working directory inside container (optional; has a sensible default).

## Files in This Directory

- `Dockerfile` – Container image definition.
- `entrypoint.sh` – Entry point with automatic working directory setup.
- `requirements.txt` – Python package dependencies.
- `run_parallel.py` – Python thread-based parallel executor.
- `run_reports.sh` – Wrapper shell script for `run_parallel.py`.
- `generate_compose.py` – Generate `docker-compose.yml` from directory scan.
- `prepare_working_dir.sh` – Helper to seed pickle and EC files.
- `docker-compose.yml.template` – Manual compose template reference.
- **`WORKFLOW.md`** – Complete workflow documentation and best practices.
- **`QUICKSTART.sh`** – Interactive quick start guide.

## Expected Directory Structure

```
emulator_out/
  <firmware_id>_v<sdk>_.../ 
    acv_snaps/
      <package_1>/
        ec_files/
          coverage_*.ec
      <package_2>/
        ec_files/
          coverage_*.ec
    acv_reports/         (output location)
      <package_1>/
        (reports generated here)
```

## Tips

- **Scaling:** Adjust `--max-workers` based on available CPU/memory. Start with 4 and increase cautiously.
- **Resource Limits:** Edit `docker-compose.yml` to add CPU/memory caps per service if needed.
- **Timeouts:** Each container has a 1-hour execution limit; extend as needed in `run_parallel.py`.
- **Output:** Reports are written to `acv_reports/` directories next to `acv_snaps/`.
- **Debugging:** Use `docker logs <container-name>` to inspect individual container output.

