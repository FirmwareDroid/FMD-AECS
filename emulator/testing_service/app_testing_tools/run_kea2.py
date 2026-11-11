#!/usr/bin/env python3
"""
Kea2 testing tool wrapper.

Runs Kea2 (property-based + Fastbot3 backend) on an Android app.
Kea2 is installed via pip (kea2-python) and must be initialised once with
`kea2 init` before first use (handled automatically by install_tools.py).

Usage:
    python3 run_kea2.py -p <package> [options]

Examples:
    python3 run_kea2.py -p com.example.app --running-minutes 30
    python3 run_kea2.py -p com.example.app --serial emulator-5554
"""

import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
import shutil
import subprocess
import sys
import os
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

def _find_project_root(marker='common.py'):
    p = _HERE
    while True:
        candidate = os.path.join(p, marker)
        if os.path.exists(candidate):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None

_PROJECT_ROOT = _find_project_root() or os.path.normpath(os.path.join(_HERE, '..', '..', '..'))
if _PROJECT_ROOT and _PROJECT_ROOT not in sys.path:
    sys.path.append(_PROJECT_ROOT)

from common import get_first_connected_device
from test_results import append_run



BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEA2_WORKDIR = os.path.join(BASE_DIR, 'tools', 'kea2')

# Default output directory for start_apps summaries
DEFAULT_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
os.makedirs(DEFAULT_OUT_DIR, exist_ok=True)
log_file_path = os.path.join(DEFAULT_OUT_DIR, 'kea2_start.log')
root_logger = logging.getLogger()
already_have = False
for h in list(root_logger.handlers):
    try:
        if isinstance(h, logging.FileHandler) and os.path.abspath(getattr(h, 'baseFilename', '')) == os.path.abspath(log_file_path):
            already_have = True
            break
    except Exception:
        continue
if not already_have:
    fh = RotatingFileHandler(log_file_path, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    root_logger.addHandler(fh)


def _find_kea2_bin() -> str:
    """Resolve the kea2 executable from PATH or the local tools venv."""
    kea2_bin = shutil.which('kea2')
    if kea2_bin:
        return kea2_bin
    venv_kea2 = os.path.join(BASE_DIR, 'tools', 'venv', 'bin', 'kea2')
    if os.path.isfile(venv_kea2) and os.access(venv_kea2, os.X_OK):
        return venv_kea2
    return None


def _record_kea2_summary(package, success: bool, ret: int, reason: str = None, detail: str = None):
    summary = {
        'total_packages': 1,
        'started': 1 if success else 0,
        'failed': 0 if success else 1,
        'skipped': 0,
        'started_by_script': 1 if success else 0,
        'started_packages': [package] if success else [],
        'failed_packages': [] if success else [package],
        'kea2_exit_code': ret,
    }
    failures = []
    if not success:
        failures.append({
            'package': package,
            'reason': reason or 'kea2_failed',
            'detail': detail or f'return_code={ret}',
        })
    try:
        append_run('kea2', summary, failures, out_dir=os.path.join(BASE_DIR, 'out'))
    except Exception:
        logger.exception('Failed to write kea2 summary')


def run_kea2(package, running_minutes=60, serial=None, output_dir=None, apk_path=None):
    """Run Kea2 on the given package and return the exit code.

    This wrapper behaviour mirrors other tool wrappers in this project:
    - Auto-select first connected device when --serial omitted
    - Optionally pull the installed APK from the device when --apk omitted
    - Set ANDROID_SERIAL in the environment for the run
    - Write a small run summary via test_results.append_run
    """
    kea2_bin = _find_kea2_bin()
    if not kea2_bin:
        logger.error("kea2 not found in PATH or tools/venv. Install with: pip install kea2-python")
        _record_kea2_summary(package, False, 1, reason='kea2_not_installed', detail='kea2 binary not found')
        return 1

    os.makedirs(KEA2_WORKDIR, exist_ok=True)
    out = output_dir or os.path.join(KEA2_WORKDIR, 'output')
    os.makedirs(out, exist_ok=True)

    tmpdir = None

    # If no serial provided, pick the first connected adb device (if any)
    if not serial:
        first = get_first_connected_device()
        if first:
            serial = first
            logger.info('Using first connected adb device: %s', serial)

    if not serial:
        logger.error('No adb devices connected; provide --serial or connect a device')
        _record_kea2_summary(package, False, 2, reason='no_device', detail='No adb device connected')
        return 2

    # Best-effort: try to pull the installed APK from device so it is available
    # locally if needed, but do NOT abort the Kea2 run if pulling fails (e.g., on
    # split APKs or protected vendor partitions) since `kea2 run -p <package>`
    # operates directly on the installed package on the device.
    apk_local = apk_path
    try:
        looks_like_apk = False
        try:
            looks_like_apk = str(package).lower().endswith('.apk') or bool(apk_path and os.path.exists(apk_path))
        except Exception:
            looks_like_apk = False

        if not apk_local and not looks_like_apk:
            pm_cmd = ['adb', '-s', serial, 'shell', 'pm', 'path', package]
            logger.info('Querying device for apk path: %s', ' '.join(pm_cmd))
            res = subprocess.run(pm_cmd, capture_output=True, text=True, timeout=30)
            if res.returncode == 0 and res.stdout:
                first_line = res.stdout.strip().splitlines()[0]
                device_apk_path = first_line.split(':', 1)[1].strip() if first_line.startswith('package:') else first_line.strip()
                if device_apk_path:
                    tmpdir = os.path.join(KEA2_WORKDIR, 'tmp', f'kea2_apk_{package}')
                    os.makedirs(tmpdir, exist_ok=True)
                    local_apk = os.path.join(tmpdir, os.path.basename(device_apk_path))
                    pull_cmd = ['adb', '-s', serial, 'pull', device_apk_path, local_apk]
                    logger.info('Pulling apk from device: %s', ' '.join(pull_cmd))
                    res2 = subprocess.run(pull_cmd, capture_output=True, text=True, timeout=60)
                    if res2.returncode == 0:
                        apk_local = local_apk
                    else:
                        logger.warning('Could not pull apk for %s (continuing without local apk): %s', package, (res2.stderr or res2.stdout).strip())
                        shutil.rmtree(tmpdir, ignore_errors=True)
                        tmpdir = None
            else:
                logger.warning('pm path did not return an APK path for %s (continuing with package name): %s', package, (res.stderr or res.stdout).strip())

        # Build the kea2 command. Kea2 primarily accepts -p <package>; keep other
        # options such as running-minutes.
        cmd = [kea2_bin, 'run', '-p', package, '--running-minutes', str(running_minutes)]
        if serial:
            cmd += ['-s', serial]

        env = os.environ.copy()
        venv_bin = os.path.dirname(kea2_bin)
        if venv_bin and venv_bin not in env.get('PATH', ''):
            env['PATH'] = f"{venv_bin}:{env.get('PATH', '')}"
        if serial:
            env['ANDROID_SERIAL'] = serial

        def ensure_kea2_initialized(cwd, max_attempts=2):
            for attempt in range(1, max_attempts + 1):
                logger.info('Ensuring kea2 project initialized (attempt %d/%d)', attempt, max_attempts)
                try:
                    p = subprocess.run([kea2_bin, 'init'], cwd=cwd, env=env, text=True, capture_output=True, timeout=60)
                except Exception:
                    logger.exception('Failed to invoke `kea2 init`')
                    return False
                init_out = (p.stdout or '') + '\n' + (p.stderr or '')
                if p.returncode == 0:
                    logger.info('kea2 init succeeded')
                    return True
                low = init_out.lower()
                if 'already initialized' in low or 'already exists' in low or 'project already initialized' in low:
                    logger.info('kea2 already initialized: %s', init_out.splitlines()[0] if init_out else '')
                    return True
                logger.warning('kea2 init returned non-zero: %s', init_out.strip())
            return False

        try:
            ensure_kea2_initialized(KEA2_WORKDIR, max_attempts=2)
        except Exception:
            logger.exception('Unexpected error while ensuring kea2 init')

        logger.info('Running Kea2 on package: %s (minutes=%d)', package, running_minutes)
        kea2_timeout = max(120, int(running_minutes) * 60 + 120)
        timed_out = False
        try:
            res = subprocess.run(cmd, cwd=KEA2_WORKDIR, env=env, text=True, capture_output=True, timeout=kea2_timeout)
            ret = res.returncode
            combined = (res.stdout or '') + '\n' + (res.stderr or '')
        except subprocess.TimeoutExpired as exc:
            logger.warning('Kea2 timed out after %ds on %s; treating as completed run', kea2_timeout, package)
            timed_out = True
            ret = 0
            combined = (exc.stdout or '') + '\n' + (exc.stderr or '') if isinstance(exc.stdout, str) else ''

        # If kea2 reports an explicit "not initialized" error, attempt init and retry once
        if not timed_out and ret != 0 and 'kea2 project not initialized' in combined.lower():
            logger.warning('kea2 reported not-initialized; attempting `kea2 init` and retrying run')
            if ensure_kea2_initialized(KEA2_WORKDIR, max_attempts=2):
                logger.info('Retrying kea2 run after init...')
                try:
                    res2 = subprocess.run(cmd, cwd=KEA2_WORKDIR, env=env, text=True, capture_output=True, timeout=kea2_timeout)
                    ret = res2.returncode
                    combined = (res2.stdout or '') + '\n' + (res2.stderr or '')
                except subprocess.TimeoutExpired:
                    logger.warning('Kea2 retry timed out after %ds on %s', kea2_timeout, package)
                    ret = 0
            else:
                logger.error('kea2 init attempts failed; not retrying run')

        if combined.strip():
            logger.info('Kea2 output for %s (exit=%d):\n%s', package, ret, combined[-2000:])

        # Kea2 exit codes:
        # 0 = completed with no bugs found
        # 1 = completed and found property violation
        # 2 = completed and found crash/ANR
        # 3 = completed and found both property violation and crash/ANR
        # >=4 or <0 = runtime / execution error
        kea2_completed = ret in (0, 1, 2, 3)
        _record_kea2_summary(
            package,
            success=kea2_completed,
            ret=ret,
            reason=None if kea2_completed else 'kea2_failed',
            detail=f'return_code={ret}',
        )

        return 0 if kea2_completed else ret
    finally:
        # Clean up any temporary apk we pulled from the device
        if tmpdir:
            try:
                if apk_local and os.path.exists(apk_local):
                    os.remove(apk_local)
                shutil.rmtree(tmpdir, ignore_errors=True)
                logger.info('Removed temporary apk and directory: %s', tmpdir)
            except Exception:
                logger.exception('Failed to remove temporary apk: %s', apk_local)


def main():
    parser = argparse.ArgumentParser(description='Run Kea2 property-based testing on an Android app')
    parser.add_argument('-p', '--package', required=True, help='Package name to test')
    parser.add_argument('--running-minutes', type=int, default=5, help='Test duration in minutes (default: 5)')
    parser.add_argument('--serial', default=os.environ.get('ANDROID_SERIAL'),
                        help='Device serial (default: ANDROID_SERIAL env var)')
    parser.add_argument('--output-dir', default=None, help='Directory to write output (default: tools/kea2/output)')
    args = parser.parse_args()

    sys.exit(run_kea2(args.package, args.running_minutes, args.serial, args.output_dir))


if __name__ == '__main__':
    main()
