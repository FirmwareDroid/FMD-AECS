#!/usr/bin/env python3
"""AutoDroid wrapper

Attempts to run AutoDroid from the installed editable repo or via a module entrypoint.
Accepts package and serial arguments.
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

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
    sys.path.insert(0, _PROJECT_ROOT)

from common import get_first_connected_device
from test_results import append_run

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def run_autodroid(package, serial=None):
    if not serial:
        serial = get_first_connected_device()
        if serial:
            logger.info('Using first connected adb device: %s', serial)
        else:
            logger.error('No adb devices connected; provide --serial or connect a device')
            return 2

    env = os.environ.copy()
    env['ANDROID_SERIAL'] = serial

    def _find_tools_venv_python():
        venv_dir = os.path.join(_HERE, 'tools', 'venv')
        for pyname in ('python3', 'python'):
            candidate = os.path.join(venv_dir, 'bin', pyname)
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return sys.executable

    python_exe = _find_tools_venv_python()
    logger.info('Using python interpreter: %s', python_exe)

    # Try well-known entrypoints
    candidates = []
    # Prefer invoking via the tools venv python (or current interpreter)
    candidates.append([python_exe, '-m', 'autodroid', '--package', package])
    # If there's a standalone script on PATH, try it as well
    if shutil.which('autodroid'):
        candidates.append(['autodroid', '--package', package])
    # Check cloned repo script and run it with the venv python and cwd set to repo
    repo_script = os.path.join(_HERE, 'tools', 'AutoDroid', 'run.py')
    repo_dir = os.path.join(_HERE, 'tools', 'AutoDroid')
    if os.path.exists(repo_script):
        candidates.append([python_exe, repo_script, '--package', package,])

    def _ensure_autodroid(python_exe, repo_dir=None):
        """Ensure the `autodroid` module is importable in python_exe.

        Try installing from repo requirements, PyPI, or editable install into
        the given interpreter. Returns True if import succeeds.
        """
        try:
            check = subprocess.run([python_exe, '-c', 'import autodroid'], capture_output=True)
            if check.returncode == 0:
                return True
        except Exception:
            pass

        # Try installing from repo requirements if available
        if repo_dir:
            req = os.path.join(repo_dir, 'requirements.txt')
            if os.path.exists(req):
                logger.info('Installing AutoDroid requirements into %s: %s', python_exe, req)
                subprocess.run([python_exe, '-m', 'pip', 'install', '--no-cache-dir', '--disable-pip-version-check', '-r', req])
                check = subprocess.run([python_exe, '-c', 'import autodroid'], capture_output=True)
                if check.returncode == 0:
                    return True

        # Try installing from PyPI
        logger.info('Attempting to pip install autodroid into %s', python_exe)
        subprocess.run([python_exe, '-m', 'pip', 'install', '--no-cache-dir', '--disable-pip-version-check', 'autodroid'])
        check = subprocess.run([python_exe, '-c', 'import autodroid'], capture_output=True)
        if check.returncode == 0:
            return True

        # Fallback: try editable install from repo
        if repo_dir and os.path.exists(repo_dir):
            logger.info('Attempting editable install of AutoDroid from repo into %s', python_exe)
            subprocess.run([python_exe, '-m', 'pip', 'install', '--no-cache-dir', '--disable-pip-version-check', '-e', repo_dir])
            check = subprocess.run([python_exe, '-c', 'import autodroid'], capture_output=True)
            if check.returncode == 0:
                return True

        logger.warning('autodroid module not available in %s', python_exe)
        return False

    for cmd in candidates:
        try:
            logger.info('Trying command: %s', ' '.join(cmd))
            # If running the repo script or module, ensure autodroid is importable
            try:
                needs_autodroid = False
                if '-m' in cmd and 'autodroid' in cmd:
                    needs_autodroid = True
                if os.path.exists(repo_script) and any(repo_script in str(x) for x in cmd):
                    needs_autodroid = True
                if needs_autodroid:
                    ok = _ensure_autodroid(python_exe, repo_dir=repo_dir)
                    if not ok:
                        logger.warning('autodroid does not appear to be installed in %s; the run may fail', python_exe)
            except Exception:
                logger.exception('Error while ensuring autodroid availability')

            # If running the repo script, set cwd so relative imports work
            cwd = repo_dir if (os.path.exists(repo_dir) and repo_script in cmd) else None
            res = subprocess.run(cmd, env=env, text=True, cwd=cwd)
            retcode = res.returncode

            # write tool summary
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
                failures.append({'package': package, 'reason': 'autodroid_failed', 'detail': f'return_code={retcode}'})
            try:
                append_run('autodroid', summary, failures, out_dir=os.path.join(_HERE, 'output'))
            except Exception:
                logger.exception('Failed to write autodroid summary')

            return retcode
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.warning('Command failed: %s', e)

    logger.error('Could not find an AutoDroid entrypoint to run. Ensure AutoDroid is installed or run install_tools.py')
    return 3


def main():
    parser = argparse.ArgumentParser(description='Run AutoDroid against an installed package')
    parser.add_argument('-p', '--package', required=True, help='Package name to test')
    parser.add_argument('--serial', default=None, help='Device serial (optional)')
    args = parser.parse_args()
    sys.exit(run_autodroid(args.package, args.serial))


if __name__ == '__main__':
    main()

