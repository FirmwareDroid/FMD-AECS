#!/usr/bin/env python3
"""DroidBot wrapper

Simple wrapper to invoke the installed `droidbot` command or the module
from the cloned repository. Accepts a package and a runtime in minutes.
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, '..', '..', '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from common import get_first_connected_device
from test_results import append_run

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def run_droidbot(package, minutes=5, serial=None, extra_args=None):
    if not serial:
        serial = get_first_connected_device()
        if serial:
            logger.info('Using first connected adb device: %s', serial)
        else:
            logger.error('No adb devices connected; provide --serial or connect a device')
            return 2

    # If the user passed a package name (not a local .apk path), try to pull
    # the installed APK from the device and pass the pulled file to DroidBot.
    apk_temp_path = None
    package_or_apk = package
    try:
        looks_like_apk = package.lower().endswith('.apk') or os.path.exists(package)
    except Exception:
        looks_like_apk = False

    if not looks_like_apk:
        # Resolve the APK path on device via `pm path` and pull it locally.
        pm_cmd = ['adb']
        if serial:
            pm_cmd += ['-s', serial]
        pm_cmd += ['shell', 'pm', 'path', package]
        logger.info('Querying device for apk path: %s', ' '.join(pm_cmd))
        res = subprocess.run(pm_cmd, capture_output=True, text=True)
        if res.returncode != 0 or not res.stdout:
            logger.error('Could not determine apk path for package %s: %s', package, (res.stderr or res.stdout).strip())
            return 4
        first_line = res.stdout.strip().splitlines()[0]
        if first_line.startswith('package:'):
            device_apk_path = first_line.split(':', 1)[1].strip()
        else:
            device_apk_path = first_line.strip()

        if not device_apk_path:
            logger.error('pm path returned no apk path for %s', package)
            return 4

        tmpdir = tempfile.mkdtemp(prefix='droidbot_apk_')
        local_apk = os.path.join(tmpdir, os.path.basename(device_apk_path))
        pull_cmd = ['adb']
        if serial:
            pull_cmd += ['-s', serial]
        pull_cmd += ['pull', device_apk_path, local_apk]
        logger.info('Pulling apk from device: %s', ' '.join(pull_cmd))
        res2 = subprocess.run(pull_cmd, capture_output=True, text=True)
        if res2.returncode != 0:
            logger.error('Failed to pull apk from device for %s: %s', package, (res2.stderr or res2.stdout).strip())
            shutil.rmtree(tmpdir, ignore_errors=True)
            return 5
        apk_temp_path = local_apk
        package_or_apk = apk_temp_path

    def _find_tools_venv_python():
        # Prefer the tools venv if present (app_testing_tools/tools/venv)
        venv_paths = [
            os.path.join(_HERE, 'tools', 'venv', 'bin', 'python3'),
            os.path.join(_HERE, 'tools', 'venv', 'bin', 'python'),
        ]
        for p in venv_paths:
            if os.path.exists(p):
                return p
        return sys.executable

    def _ensure_androguard(python_exe, repo_dir=None):
        """Ensure androguard is importable in the given python interpreter.

        If missing, try installing from repo requirements.txt (if available)
        or `pip install androguard`. Return True if androguard is available.
        """
        try:
            check = subprocess.run([python_exe, '-c', 'import androguard'], capture_output=True)
            if check.returncode == 0:
                return True
        except Exception:
            pass

        # Try installing from repo requirements if present
        if repo_dir:
            req = os.path.join(repo_dir, 'requirements.txt')
            if os.path.exists(req):
                logger.info('Installing droidbot requirements into %s: %s', python_exe, req)
                install_cmd = [python_exe, '-m', 'pip', 'install', '--no-cache-dir', '--disable-pip-version-check', '-r', req]
                subprocess.run(install_cmd)
                # re-check
                check = subprocess.run([python_exe, '-c', 'import androguard'], capture_output=True)
                if check.returncode == 0:
                    return True

        # Fallback: try to pip install androguard directly
        logger.info('Attempting to pip install androguard into %s', python_exe)
        try:
            subprocess.run([python_exe, '-m', 'pip', 'install', '--no-cache-dir', '--disable-pip-version-check', '--prefer-binary', 'androguard'], check=False)
        except Exception:
            pass

        # final check
        check = subprocess.run([python_exe, '-c', 'import androguard'], capture_output=True)
        if check.returncode == 0:
            return True
        logger.warning('androguard is not available in %s', python_exe)
        return False

    # Prefer to run using the tools venv python when available
    python_exe = _find_tools_venv_python()

    # Build candidate commands as (cmd, cwd) tuples. If a cloned repo exists,
    # prefer running from that directory (some droidbot entry scripts rely on
    # relative imports).
    cmd_candidates = []
    repo_dir = os.path.join(_HERE, 'tools', 'droidbot')
    repo_start = os.path.join(repo_dir, 'start.py')

    if os.path.isdir(repo_dir):
        # Prefer running the repo's start.py with the repo as cwd
        if os.path.exists(repo_start):
            # For the repository start.py the expected flags are '-a' (apk/package)
            # and '-timeout' for duration. Only add them if the user did not
            # provide equivalent flags in extra_args.
            extras = list(extra_args or [])
            lower = [x.lower() for x in extras]
            repo_cmd = [python_exe, repo_start]
            if not any(f in lower for f in ('-a', '--apk', '-p')):
                repo_cmd += ['-a', package_or_apk]
            if not any('timeout' in x or x in ('--time', '-t') for x in lower):
                repo_cmd += ['-timeout', str(minutes * 60)]
            repo_cmd += extras
            cmd_candidates.append((repo_cmd, repo_dir))

    # If there's a droidbot script on PATH, try running it but with cwd set to
    # the repo if available so relative imports resolve.
    if shutil.which('droidbot'):
        cwd = repo_dir if os.path.isdir(repo_dir) else None
        extras = list(extra_args or [])
        lower = [x.lower() for x in extras]
        cli_cmd = ['droidbot']
        if not any(f in lower for f in ('-p', '-a', '--apk')):
            cli_cmd += ['-p', package_or_apk]
        if not any('time' in x or x in ('-t', '-timeout') for x in lower):
            cli_cmd += ['--time', str(minutes * 60)]
        cli_cmd += extras
        cmd_candidates.append((cli_cmd, cwd))

    # fallback to python -m droidbot if installed as a module. Use '-a' and
    # '--time' by default unless the caller provided explicit flags.
    extras = list(extra_args or [])
    lower = [x.lower() for x in extras]
    mod_cmd = [python_exe, '-m', 'droidbot']
    if not any(f in lower for f in ('-a', '--apk', '-p')):
        mod_cmd += ['-a', package_or_apk]
    if not any('time' in x or x in ('-t', '-timeout') for x in lower):
        mod_cmd += ['--time', str(minutes * 60)]
    mod_cmd += extras
    cmd_candidates.append((mod_cmd, None))

    env = os.environ.copy()
    env['ANDROID_SERIAL'] = serial
    python_exe = _find_tools_venv_python()

    # Run candidates; ensure we cleanup any pulled apk afterwards.
    try:
        for cmd, cwd in cmd_candidates:
            try:
                logger.info('Trying command: %s (cwd=%s)', ' '.join(cmd), cwd)
                # If we are about to run the cloned repo start.py or the module
                # backend, ensure androguard is present in the interpreter used.
                try:
                    needs_androguard = False
                    if any(repo_start in str(x) for x in cmd):
                        needs_androguard = True
                    if '-m' in cmd and 'droidbot' in cmd:
                        needs_androguard = True
                    if needs_androguard:
                        ok = _ensure_androguard(python_exe, repo_dir=repo_dir)
                        if not ok:
                            logger.warning('androguard not available; the droidbot run may fail')

                except Exception:
                    logger.exception('Error while ensuring androguard availability')

                # Execute the candidate command
                res = subprocess.run(cmd, env=env, text=True, cwd=cwd)
                retcode = res.returncode

                # Prepare and write summary for this droidbot run
                summary = {
                    'total_packages': 1,
                    'started': 1 if retcode == 0 else 0,
                    'failed': 0 if retcode == 0 else 1,
                    'skipped': 0,
                    'started_by_script': 1 if retcode == 0 else 0,
                    'started_packages': [package] if retcode == 0 else [],
                    'failed_packages': [] if retcode == 0 else [package],
                }
                failures = []
                if retcode != 0:
                    failures.append({'package': package, 'reason': 'droidbot_failed', 'detail': f'return_code={retcode}'})
                try:
                    append_run('droidbot', summary, failures, out_dir=os.path.join(_HERE, 'output'))
                except Exception:
                    logger.exception('Failed to write droidbot summary')

                return retcode
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning('Command failed: %s', e)
        logger.error('Could not find a runnable droidbot command. Ensure DroidBot is installed or run install_tools.py')
        return 3
    finally:
        # Remove any temporary APK we pulled from the device.
        if apk_temp_path:
            try:
                tmpdir = os.path.dirname(apk_temp_path)
                if os.path.exists(apk_temp_path):
                    os.remove(apk_temp_path)
                shutil.rmtree(tmpdir, ignore_errors=True)
                logger.info('Removed temporary apk and directory: %s', tmpdir)
            except Exception:
                logger.exception('Failed to remove temporary apk: %s', apk_temp_path)


def main():
    parser = argparse.ArgumentParser(description='Run DroidBot against an installed package')
    parser.add_argument('-p', '--package', required=True, help='Package name to test')
    parser.add_argument('--minutes', type=int, default=5, help='Run duration in minutes')
    parser.add_argument('--serial', default=None, help='Device serial (optional)')
    args, remaining = parser.parse_known_args()
    # Pass through any remaining args directly to droidbot start script
    sys.exit(run_droidbot(args.package, args.minutes, args.serial, extra_args=remaining))


if __name__ == '__main__':
    main()

