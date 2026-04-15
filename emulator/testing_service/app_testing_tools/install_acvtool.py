#!/usr/bin/env python3
"""
Install ACVTool into the container and ensure an "acv" command is available.

This script is intended to be executed at Docker build time. It will:
 - attempt to install the `acvtool` Python package via pip
 - verify that an `acv` CLI is available on PATH
 - if not present, create a small wrapper script at /usr/local/bin/acv that
   forwards arguments to `python3 -m acvtool` so the command is available.

The script exits with code 0 on success, non-zero on fatal failure.
"""
import shutil
import subprocess
import sys
import os
import json
import platform

from pathlib import Path

ACV_PKG = 'acvtool'
WRAPPER_PATH = '/usr/local/bin/acv'
PYEXEC = sys.executable or 'python3'


def run(cmd, check=True):
    print('RUN:', cmd)
    res = subprocess.run(cmd, shell=isinstance(cmd, str), text=True)
    if check and res.returncode != 0:
        raise SystemExit(f"Command failed (exit {res.returncode}): {cmd}")
    return res


def ensure_installed():
    # Install acvtool system-wide. Use --break-system-packages to override
    # Debian's externally-managed environment (PEP 668) policy when necessary.
    try:
        run([PYEXEC, '-m', 'pip', 'install', '--no-cache-dir', '--break-system-packages', ACV_PKG])
        return
    except SystemExit:
        print('System pip install with --break-system-packages failed, retrying without --no-cache-dir')
        try:
            run([PYEXEC, '-m', 'pip', 'install', '--break-system-packages', ACV_PKG])
            return
        except SystemExit:
            print('Failed to install acvtool via system pip (even with --break-system-packages)')
            raise


def ensure_cli():
    # Check if acv is available on PATH
    if shutil.which('acv'):
        print('acv command already available on PATH')
        return True

    # Create a simple executable wrapper that calls `python3 -m acvtool`.
    # This covers cases where pip did not create an entrypoint script.
    wrapper = """#!/bin/sh
# Wrapper to invoke acvtool module as CLI
exec {py} -m acvtool "$@"
""".format(py=PYEXEC)

    try:
        Path(WRAPPER_PATH).write_text(wrapper)
        os.chmod(WRAPPER_PATH, 0o755)
        print(f'Wrote wrapper to {WRAPPER_PATH}')
    except Exception as e:
        print('Failed to write wrapper script:', e)
        return False

    # Re-check
    if shutil.which('acv'):
        print('acv wrapper now available')
        return True
    print('acv still not found after creating wrapper')
    return False


def main():
    print('Installing ACVTool...')
    try:
        ensure_installed()
    except SystemExit as e:
        print('ERROR: acvtool installation failed:', e)
        sys.exit(1)

    ok = ensure_cli()
    if not ok:
        print('ERROR: acv command not available after installation')
        sys.exit(2)

    # Final check: run `acv --version` or `acv -h` to ensure executable runs
    try:
        res = subprocess.run(['acv', '--version'], capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            # Try help as fallback
            res = subprocess.run(['acv', '-h'], capture_output=True, text=True, timeout=10)
        print('acv exec output (truncated):')
        print((res.stdout or res.stderr or '')[:1000])
    except Exception as e:
        print('Failed to execute acv for verification:', e)
        sys.exit(3)

    print('ACVTool installed and "acv" command available')

    # Create /root/acvtool/config.json with required tool paths
    try:
        cfg_dir = Path('/root/acvtool')
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg = {
            "AAPT": "/android/sdk/build-tools/36.1.0/aapt2",
            "ZIPALIGN": "/android/sdk/build-tools/36.1.0/zipalign",
            "ADB": "/android/sdk/platform-tools/adb",
            "APKSIGNER": "/android/sdk/build-tools/36.1.0/apksigner",
            "ACVPATCHER": "/root/ACVPatcher"
        }
        cfg_path = cfg_dir / 'config.json'
        cfg_path.write_text(json.dumps(cfg, indent=4))
        print(f'Wrote ACVTool config to {cfg_path}')
    except Exception as e:
        print('Failed to write /root/acvtool/config.json:', e)
        sys.exit(4)

    # Download ACVPatcher binary zip and extract to /root
    arch = platform.machine().lower()
    syst = platform.system().lower()
    # Choose appropriate asset name based on platform/arch
    if syst == 'linux':
            asset = 'ACVPatcher-osx-arm64.zip'
    elif syst == 'darwin':
            asset = 'ACVPatcher-osx-amd64.zip'
    else:
        asset = 'ACVPatcher-linux.zip'

    acvpatcher_url = f'https://github.com/pilgun/acvpatcher/releases/download/1.0.8/{asset}'
    tmp_zip = f'/tmp/{asset}'
    try:
        print('Downloading ACVPatcher from', acvpatcher_url)
        import urllib.request, zipfile
        urllib.request.urlretrieve(acvpatcher_url, tmp_zip)
        with zipfile.ZipFile(tmp_zip, 'r') as z:
            z.extractall('/root')
        # Make ACVPatcher executable if present
        acv_path = Path('/root/ACVPatcher')
        if acv_path.exists():
            os.chmod(str(acv_path), 0o755)
            print('ACVPatcher extracted and made executable at /root/ACVPatcher')
        else:
            print('ACVPatcher binary not found in extracted archive')
            sys.exit(5)
    except Exception as e:
        print('Failed to download/extract ACVPatcher:', e)
        sys.exit(5)


if __name__ == '__main__':
    main()

