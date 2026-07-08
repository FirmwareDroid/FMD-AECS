# ACVTool Report Generation Workflow

This document explains the workflow for generating ACVTool coverage reports and how it maps to the Docker container implementation.

## Overview

ACVTool generates code coverage reports by combining:
1. **Pickle files**: Serialized Android APK/DEX code structure (from static analysis)
2. **EC files**: Execution coverage data collected at runtime
3. **Report generation**: Merges pickles + EC to create coverage report

## Directory Structure

Expected structure in `emulator_out`:

```
emulator_out/
├── <firmware_id>_v<sdk>_.../ (e.g., 68af5a9765e2ad36cfb14a36_v12_sdk_phone64_arm64_userdebug_r9_dev)
│   ├── acv_snaps/
│   │   ├── <package_1>/
│   │   │   ├── ec_files/
│   │   │   │   ├── coverage_000.ec
│   │   │   │   └── coverage_001.ec
│   │   │   └── .acv_wd/ (working directory, created during processing)
  │   │   │       ├── pickles/
  │   │   │       ├── ec/
│   │   │       └── report/
│   │   └── <package_2>/
│   │       └── ...
│   └── acv_reports/ (output location)
│       ├── <package_1>/
│       │   ├── index.html
│       │   └── ...
│       └── <package_2>/
│           └── ...
```

## Workflow Steps

### Step 1: Discover Firmware and Packages

The runner scans `emulator_out/` and identifies all firmware/package combinations with coverage data.

```bash
./run_reports.sh --dry-run
# Output: Found 1359 tasks across 11 firmware(s)
```

### Step 2: Prepare Working Directory (automatically done)

For each package, the runner creates:

```
<firmware>/acv_snaps/<package>/.acv_wd/
├── pickles/    ← pickle files staged here
├── ec/                 ← EC files copied here
└── report/             ← output generated here
```

When you run the reporter:

```bash
run_parallel.py
```

It automatically:
1. Creates the `.acv_wd` directory structure on the host
2. Mounts `emulator_out/` into the Docker container as `/work/input`
3. Passes `ACV_WD=/work/input/<firmware>/acv_snaps/<package>/.acv_wd` to the container

### Step 3: Stage Files

**EC Files**: Automatically copied from `<package>/ec_files/` → `.acv_wd/ec/`

**Pickle Files**: You need to provide these. Options:

1. **Download from Nexus** (if available):
   ```bash
   prepare_working_dir.sh ./data/01_journal_extension/emulator_out $firmware_id $package --download-pickles
   ```

2. **Manual**: Copy `.pickle` files to `.acv_wd/pickles/`

3. **From your notebook workflow**: Extract from zip files downloaded via the notebook

### Step 4: Run ACVTool Report

Inside the Docker container, the entrypoint.sh does:

```bash
# 1. Ensure working directory structure exists
mkdir -p $ACV_WD/pickles
mkdir -p $ACV_WD/ec
mkdir -p $ACV_WD/report

# 2. Register staged pickles and run report
acv cover-pickles <package_name> --wd $ACV_WD
acv report <package_name> --wd $ACV_WD
```

### Step 5: Collect Output

Reports are generated in `.acv_wd/report/` and should be moved to `acv_reports/`:

```
emulator_out/
└── <firmware>/
    └── acv_reports/
        └── <package>/
            ├── index.html
            ├── report.xml
            └── ...
```

## Running Reports

### Option A: Thread-based Runner (Recommended)

```bash
# Dry-run (no containers started)
./run_reports.sh --dry-run

# Run with 4 parallel workers
./run_reports.sh

# Run with 8 workers
./run_reports.sh --max-workers 8

# Save results
./run_reports.sh --output results.json
```

This runs each container once per package and automatically:
- Creates working directories
- Mounts emulator_out
- Passes correct ACV_WD to container
- Handles cleanup

### Option B: Docker Compose

```bash
# Generate docker-compose.yml
python3 generate_compose.py

# Start all containers
docker-compose up -d

# Monitor
docker-compose logs -f

# Clean up
docker-compose down
```

## Notebook vs Docker Workflow Comparison

### Notebook Workflow (from `05_CoverageAnalysis.ipynb`)

```python
# 1. Download pickles from Nexus
zip_path = download_from_nexus(f"acvtool_{firmware_id}.zip")
extract_to = fw_dir / "acv_snaps" / "pickle_files"

# 2. Find pickles for each package
pickle_index = {}
for pickle in extract_to.rglob("*.pickle"):
    package_key = parse_package_name(pickle)
    pickle_index[package_key].append(pickle)

# 3. For each package, stage files and generate report
for package in packages:
    # Stage
    for pickle in pickle_index[package]:
        shutil.copy(pickle, STAGE_PICKLES)
    for ec_file in package_dir/ec_files:
        shutil.copy(ec_file, STAGE_EC_FILES)
    
    # Report
    subprocess.run(["acv", "report", package, "--wd", WORKING_DIR])
```

### Docker Runner Workflow

```bash
# 1. Discover firmware/packages
for firmware in emulator_out/*; do
    for package in emulator_out/$firmware/acv_snaps/*; do
        
        # 2. Create and prepare working directory
        wd="emulator_out/$firmware/acv_snaps/$package/.acv_wd"
        mkdir -p "$wd"/pickles
        mkdir -p "$wd"/ec
        mkdir -p "$wd"/report
        
        # 3. Copy EC files (pickles must be staged separately)
        cp $package/ec_files/*.ec "$wd"/ec/
        
        # 4. Run container
        docker run \
          -v emulator_out:/work/input \
          -e ACV_WD=/work/input/$firmware/acv_snaps/$package/.acv_wd \
          acvtool-report:2.3.6 \
          report $package
    done
done
```

## Key Differences

| Aspect | Notebook | Docker |
|--------|----------|--------|
| **Pickle Source** | Nexus download | Manual or NFS mount |
| **Working Dir** | Centralized (`~/acvtool_working_dir`) | Per-package (`.acv_wd`) |
| **Concurrency** | Sequential | Parallel (configurable) |
| **File Staging** | Python script | Automatic (runner) |
| **Isolation** | System-wide | Per-container |
| **Cleanup** | Manual | Automatic |

## Pickle File Handling

### Important Notes

1. **Pickles are package-specific**: Each package needs its own pickle file(s) with matching structure.

2. **Pickle naming**: `<package_name>.pickle` or similar pattern

3. **Pickle source**: Usually downloaded from Nexus or generated during initial acvtool instrumentation

4. **If pickles missing**: 
   - Container will still run but may generate incomplete reports
   - Check logs: `docker logs <container_name>`

5. **Staging pickles** (optional helper):
   ```bash
   prepare_working_dir.sh \
     ./data/01_journal_extension/emulator_out \
     68af5a9765e2ad36cfb14a36_v12_sdk_phone64_arm64_userdebug_r9_dev \
     com.android.calculator \
     --download-pickles
   ```

## Troubleshooting

- ### Container fails: "No pickle found"
- Ensure `.acv_wd/pickles/` contains pickle file(s) for the package
- Use `prepare_working_dir.sh` or manually copy pickles before running

### Container fails: "EC file not found"
- Check `<package>/ec_files/` contains `.ec` files
- Runner should auto-stage; verify with: `ls <package>/.acv_wd/ec/`

### Reports not generated
- Check container logs: `docker logs <container_name>`
- Verify working dir structure: `ls -la <package>/.acv_wd/`
- Ensure both pickles and EC files are present

### Out of disk space
- Clean Docker: `docker system prune -a`
- Remove old `.acv_wd` directories: `find emulator_out -type d -name ".acv_wd" -exec rm -rf {} +`

## Performance Tuning

### Thread-based Runner

- **--max-workers 4**: Conservative (good for 2-4 core systems)
- **--max-workers 8**: Balanced (good for 8 core systems with 16GB RAM)
- **--max-workers 16**: Aggressive (for high-end systems)

Each worker runs one container; container CPU/memory not capped.

### Docker Compose

Edit `docker-compose.yml` to add per-container resource limits:

```yaml
deploy:
  resources:
    limits:
      cpus: '2'    # CPU cores
      memory: 4G   # Memory
```

## Output Files

Reports generated per container:

```
emulator_out/
└── <firmware>/
    └── acv_snaps/
        └── <package>/
            └── .acv_wd/
                └── report/          ← Generated reports here
                    ├── index.html   (main entry point)
                    ├── report.xml
                    ├── report.json
                    └── (other report formats)
```

To archive or move reports:

```bash
# Copy all reports to emulator_out/acv_reports/
for fw in emulator_out/*; do
    for pkg in $fw/acv_snaps/*; do
        if [ -d "$pkg/.acv_wd/report" ]; then
            mkdir -p $fw/acv_reports/$(basename $pkg)
            cp -r $pkg/.acv_wd/report/* $fw/acv_reports/$(basename $pkg)/
        fi
    done
done
```

