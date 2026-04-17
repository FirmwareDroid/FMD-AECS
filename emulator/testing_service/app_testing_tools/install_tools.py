#!/usr/bin/env python3
"""
Install all app testing tools into the Docker container.

Tools installed:
  - Ape          https://github.com/the-themis-benchmarks/ape-bin
  - Combodroid   https://github.com/the-themis-benchmarks/combodroid
  - Humanoid     https://github.com/the-themis-benchmarks/Humanoid  (TF 1.12 best-effort)
  - Q-Testing    https://github.com/the-themis-benchmarks/Q-testing  (binary not available for arm64)
  - TimeMachine  https://github.com/the-themis-benchmarks/TimeMachine (requires VirtualBox)
  - Droidrun     https://github.com/droidrun/droidrun               (installed via pip)
  - AppAgentX    https://github.com/Westlake-AGI-Lab/AppAgentX
  - Fastbot2.0   https://github.com/bytedance/Fastbot_Android
  - Kea2         https://github.com/ecnusse/Kea2                    (installed via pip)
"""

import logging
import os
import subprocess
import sys
import shutil
import platform
import glob

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(BASE_DIR, 'tools')
TOOLS_REQUIREMENTS = os.path.join(BASE_DIR, '../requirements.txt')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_cmd(cmd, cwd=None, check=True):
    """Run a command (list or shell string) and return the CompletedProcess."""
    logger.info("Running: %s", cmd if isinstance(cmd, str) else ' '.join(str(a) for a in cmd))
    result = subprocess.run(
        cmd,
        shell=isinstance(cmd, str),
        cwd=cwd,
        text=True,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result


def clone_or_skip(url, dest, depth=1):
    """Clone *url* into *dest* (shallow).  Skip gracefully if already present."""
    if os.path.isdir(os.path.join(dest, '.git')):
        logger.info("Repository already present at %s – skipping clone.", dest)
        return True
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        run_cmd(['git', 'clone', '--depth', str(depth), url, dest])
        return True
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to clone %s: %s", url, exc)
        return False


def pip_install(packages, extra_args=None):
    """Install Python packages, falling back without --break-system-packages."""
    extra = list(extra_args or [])
    # Prefer to install into the tools venv if present. Fall back to the current
    # interpreter when no venv exists.
    venv_python = None
    venv_dir = os.path.join(TOOLS_DIR, 'venv')
    if os.path.isdir(venv_dir):
        for pyname in ('python3', 'python'):
            candidate = os.path.join(venv_dir, 'bin', pyname)
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                venv_python = candidate
                break

    base_python = venv_python or sys.executable

    attempts = []
    attempts.append([base_python, '-m', 'pip', 'install', '--no-cache-dir'] + extra + packages)
    attempts.append([base_python, '-m', 'pip', 'install', '--no-cache-dir', '--prefer-binary'] + extra + packages)
    attempts.append([base_python, '-m', 'pip', 'install', '--no-cache-dir', '--disable-pip-version-check'] + extra + packages)

    for cmd in attempts:
        try:
            run_cmd(cmd)
            return True
        except subprocess.CalledProcessError:
            # try next, but continue
            logger.debug('pip install attempt failed, trying fallback: %s', ' '.join(str(x) for x in cmd))
            continue

    logger.error("pip install failed for: %s (using %s)", packages, base_python)
    return False


def ensure_cmake_and_build_tools():
    """Ensure cmake and basic build tools (make, gcc) are available.

    Attempts to install via apt-get when available, or brew on macOS. Returns True
    if cmake is available after this call, False otherwise.
    """
    if shutil.which('cmake'):
        logger.info('cmake already available: %s', shutil.which('cmake'))
        return True

    logger.info('cmake not found on PATH. Attempting to install cmake and build tools...')

    # Prefer apt-get in container environments
    if shutil.which('apt-get'):
        try:
            run_cmd('apt-get update')
            run_cmd(['apt-get', 'install', '-y', 'cmake', 'build-essential'])
            if shutil.which('cmake'):
                logger.info('cmake installed via apt-get: %s', shutil.which('cmake'))
                return True
        except subprocess.CalledProcessError as exc:
            logger.warning('apt-get install of cmake/build-essential failed: %s', exc)

    # macOS: try brew
    if platform.system() == 'Darwin' and shutil.which('brew'):
        try:
            run_cmd(['brew', 'update'])
            run_cmd(['brew', 'install', 'cmake'])
            if shutil.which('cmake'):
                logger.info('cmake installed via brew: %s', shutil.which('cmake'))
                return True
        except subprocess.CalledProcessError as exc:
            logger.warning('brew install of cmake failed: %s', exc)

    logger.warning('Could not install cmake automatically. Please install cmake and build tools (make, gcc) manually.')
    return False


def create_tools_venv(venv_dir=None):
    """Create a venv under TOOLS_DIR/venv and ensure pip/setuptools/wheel are up-to-date.

    Returns the path to the python executable inside the venv, or None on failure.
    """
    if venv_dir is None:
        venv_dir = os.path.join(TOOLS_DIR, 'venv')
    if os.path.isdir(venv_dir):
        # already exists
        for pyname in ('python3', 'python'):
            candidate = os.path.join(venv_dir, 'bin', pyname)
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                logger.info('Tools venv already exists: %s', venv_dir)
                return candidate
        logger.warning('venv dir exists but no python executable found inside: %s', venv_dir)
        return None

    logger.info('Creating tools venv at %s', venv_dir)
    try:
        run_cmd([sys.executable, '-m', 'venv', venv_dir])
    except subprocess.CalledProcessError as exc:
        logger.warning('Failed to create venv at %s: %s', venv_dir, exc)
        return None

    # upgrade pip/setuptools/wheel inside the venv
    python_exe = None
    for pyname in ('python3', 'python'):
        candidate = os.path.join(venv_dir, 'bin', pyname)
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            python_exe = candidate
            break

    if not python_exe:
        logger.warning('Failed to locate python inside created venv: %s', venv_dir)
        return None

    try:
        run_cmd([python_exe, '-m', 'pip', 'install', '--upgrade', 'pip', 'setuptools', 'wheel'])
        logger.info('Upgraded pip/setuptools/wheel in venv')
    except subprocess.CalledProcessError as exc:
        logger.warning('Failed to upgrade pip in venv: %s', exc)

    return python_exe


def detect_android_ndk():
    """Attempt to locate an Android NDK installation.

    Returns the path to the NDK root (where build/cmake/android.toolchain.cmake exists), or None.
    Checks common environment variables and Android SDK locations.
    """
    # Check common env vars first
    candidates = []
    for var in ('NDK_ROOT', 'ANDROID_NDK_HOME', 'ANDROID_NDK_ROOT'):
        val = os.environ.get(var)
        if val:
            candidates.append(val)

    # Check Android SDK locations
    sdk_roots = [os.environ.get('ANDROID_SDK_ROOT'), os.environ.get('ANDROID_HOME'), os.path.expanduser('~/Android/Sdk'), '/opt/android-sdk', '/opt/android-sdk-linux']
    for sdk in sdk_roots:
        if not sdk:
            continue
        # common ndk locations inside SDK
        ndk_dir = os.path.join(sdk, 'ndk')
        if os.path.isdir(ndk_dir):
            # pick the latest versioned dir inside ndk/
            try:
                versions = [d for d in os.listdir(ndk_dir) if os.path.isdir(os.path.join(ndk_dir, d))]
                if versions:
                    versions_sorted = sorted(versions)
                    candidates.append(os.path.join(ndk_dir, versions_sorted[-1]))
            except Exception:
                pass
        ndk_bundle = os.path.join(sdk, 'ndk-bundle')
        if os.path.isdir(ndk_bundle):
            candidates.append(ndk_bundle)

    # Also check a few common root locations
    for p in ['/opt/android-ndk', '/usr/local/android-ndk', '/usr/local/android-ndk-r21', '/usr/local/android-ndk-r23b']:
        if os.path.isdir(p):
            candidates.append(p)

    # Normalize and dedupe
    seen = set()
    for c in candidates:
        try:
            cabs = os.path.abspath(os.path.expanduser(c))
        except Exception:
            continue
        if cabs in seen:
            continue
        seen.add(cabs)
        toolchain = os.path.join(cabs, 'build', 'cmake', 'android.toolchain.cmake')
        if os.path.exists(toolchain):
            return cabs

    return None


# ---------------------------------------------------------------------------
# Individual tool installers
# ---------------------------------------------------------------------------

def install_ape():
    logger.info("=== Installing Ape ===")
    ape_dir = os.path.join(TOOLS_DIR, 'ape-bin')
    if clone_or_skip('https://github.com/the-themis-benchmarks/ape-bin', ape_dir):
        ape_script = os.path.join(ape_dir, 'ape')
        if os.path.exists(ape_script):
            os.chmod(ape_script, 0o755)
        logger.info("✓ Ape installed at %s. Use run_ape.py to deploy and run.", ape_dir)
    else:
        logger.error("✗ Ape installation failed.")


def install_combodroid():
    logger.info("=== Installing ComboDroid ===")
    combo_dir = os.path.join(TOOLS_DIR, 'combodroid')
    if clone_or_skip('https://github.com/the-themis-benchmarks/combodroid', combo_dir):
        logger.info("✓ ComboDroid installed at %s. Use run_combodroid.py to run.", combo_dir)
    else:
        logger.error("✗ ComboDroid installation failed.")


def install_humanoid():
    logger.info("=== Installing Humanoid ===")
    humanoid_dir = os.path.join(TOOLS_DIR, 'Humanoid')
    droidbot_dir = os.path.join(TOOLS_DIR, 'droidbot')

    ok = clone_or_skip('https://github.com/the-themis-benchmarks/Humanoid', humanoid_dir)
    ok = clone_or_skip('https://github.com/the-themis-benchmarks/droidbot', droidbot_dir) and ok

    if not ok:
        logger.error("✗ Humanoid/DroidBot clone failed.")
        return

    # Install DroidBot (Themis branch)
    if not pip_install(['-e', droidbot_dir]):
        logger.warning("DroidBot install failed – Humanoid may not work.")

    # Install lightweight Humanoid deps. Prefer using a pinned requirements file
    # to avoid pip resolver backtracking if it exists next to this script.
    if os.path.exists(TOOLS_REQUIREMENTS):
        logger.info('Found pinned tools requirements at %s; installing from it', TOOLS_REQUIREMENTS)
        pip_install(['-r', TOOLS_REQUIREMENTS])
    else:
        pip_install(['matplotlib', 'scipy', 'pyflann-py3'])

    # TensorFlow 1.12 requires Python 3.5-3.7; it will almost certainly fail on
    # Python 3.8+ / arm64, so we attempt it as best-effort only.
    if not pip_install(['tensorflow==1.12.0']):
        logger.warning(
            "⚠ TensorFlow 1.12 could not be installed (expected on Python 3.8+ / arm64). "
            "Humanoid inference will be unavailable without a compatible TensorFlow."
        )

    logger.info("✓ Humanoid installed. Use run_humanoid.py to run.")


def install_qtesting():
    logger.info("=== Installing Q-Testing ===")
    qtesting_dir = os.path.join(TOOLS_DIR, 'Q-testing')
    if clone_or_skip('https://github.com/the-themis-benchmarks/Q-testing', qtesting_dir):
        logger.warning(
            "⚠ Q-Testing: the pre-built binary is distributed via OneDrive and cannot be "
            "downloaded automatically. The binary targets x86/x86_64 Linux and is NOT "
            "compatible with arm64. Configuration files have been cloned for reference."
        )
    else:
        logger.error("✗ Q-Testing clone failed.")


def install_timemachine():
    logger.info("=== Installing TimeMachine ===")
    tm_dir = os.path.join(TOOLS_DIR, 'TimeMachine')
    if clone_or_skip('https://github.com/the-themis-benchmarks/TimeMachine', tm_dir):
        logger.warning(
            "⚠ TimeMachine requires VirtualBox to manage Android-x86 VMs. "
            "VirtualBox cannot run inside Docker containers. "
            "The repository is cloned for reference only."
        )
    else:
        logger.error("✗ TimeMachine clone failed.")


def install_droidrun():
    logger.info("=== Installing Droidrun ===")
    # If a pinned tools requirements file exists, install from that to pin
    # transitive dependencies and avoid lengthy resolver backtracking.
    if os.path.exists(TOOLS_REQUIREMENTS):
        ok = pip_install(['-r', TOOLS_REQUIREMENTS])
    else:
        ok = pip_install(['droidrun'])
    if ok:
        logger.info("✓ Droidrun installed via pip.")
    else:
        logger.error("✗ Droidrun pip install failed.")


def install_appagentx():
    logger.info("=== Installing AppAgentX ===")
    appagentx_dir = os.path.join(TOOLS_DIR, 'AppAgentX')
    if not clone_or_skip('https://github.com/Westlake-AGI-Lab/AppAgentX', appagentx_dir):
        logger.error("✗ AppAgentX clone failed.")
        return

    req_file = os.path.join(appagentx_dir, 'requirements.txt')
    if os.path.exists(req_file):
        if not pip_install(['-r', req_file]):
            logger.warning("AppAgentX dependency install had errors (some optional deps may be missing).")

    logger.info(
        "✓ AppAgentX installed. "
        "Requires Neo4j, Pinecone API key, and an LLM API key to run. "
        "Configure config.py before use. Use run_appagentx.py to launch."
    )


def install_fastbot():
    logger.info("=== Installing Fastbot2.0 ===")
    fastbot_dir = os.path.join(TOOLS_DIR, 'Fastbot_Android')
    if not clone_or_skip('https://github.com/bytedance/Fastbot_Android', fastbot_dir):
        logger.error("✗ Fastbot_Android clone failed.")
        return
    # Try to build native libraries if the repository provides a build script.
    # Many Fastbot variants include a build_native.sh script to produce libfastbot_native.so
    build_script_candidates = [
        os.path.join(fastbot_dir, 'build_native.sh'),
        os.path.join(fastbot_dir, 'scripts', 'build_native.sh'),
        os.path.join(fastbot_dir, 'build_native', 'build_native.sh'),
    ]
    build_ran = False
    for script in build_script_candidates:
        if os.path.exists(script):
            try:
                logger.info('Found Fastbot native build script: %s. Attempting to run it...', script)
                # Ensure executable bit and run via bash for portability
                try:
                    os.chmod(script, 0o755)
                except Exception:
                    pass
                # Ensure cmake and basic build tools are present before running build script
                ok_build_tools = ensure_cmake_and_build_tools()
                # Attempt to detect Android NDK automatically if env vars not set
                ndk_root = os.environ.get('NDK_ROOT') or os.environ.get('ANDROID_NDK_HOME') or os.environ.get('ANDROID_NDK_ROOT')
                if not ndk_root:
                    ndk_root = detect_android_ndk()
                    if ndk_root:
                        logger.info('Auto-detected Android NDK at: %s', ndk_root)
                        os.environ['NDK_ROOT'] = ndk_root

                # Verify the expected CMake toolchain file exists
                toolchain_path = None
                if ndk_root:
                    toolchain_path = os.path.join(ndk_root, 'build', 'cmake', 'android.toolchain.cmake')
                if not ndk_root or not toolchain_path or not os.path.exists(toolchain_path):
                    logger.warning('Android toolchain not found at expected location: %s. Native build will be skipped.', toolchain_path)
                    build_ran = True
                elif not ok_build_tools:
                    logger.warning('Skipping native build because required build tools are missing.')
                    build_ran = True
                else:
                    run_cmd(['bash', script], cwd=fastbot_dir)
                    build_ran = True
                build_ran = True
                logger.info('Fastbot native build script completed (attempted %s)', script)
            except subprocess.CalledProcessError as exc:
                logger.warning('Fastbot native build script failed: %s', exc)
            break

    if not build_ran:
        logger.info('No Fastbot native build script found; skipping native build step.')

    required = [
        os.path.join(fastbot_dir, 'monkeyq.jar'),
        os.path.join(fastbot_dir, 'fastbot-thirdpart.jar'),
        os.path.join(fastbot_dir, 'framework.jar'),
        os.path.join(fastbot_dir, 'libs', 'arm64-v8a'),
    ]
    missing = [p for p in required if not os.path.exists(p)]
    if missing:
        logger.warning("Fastbot: missing expected files/dirs: %s", missing)
    else:
        logger.info("✓ Fastbot2.0 JARs and arm64-v8a .so files are present.")

    logger.info("✓ Fastbot2.0 installed. Use run_fastbot.py to deploy and run on device.")

    # If native libs are present, attempt to push the arm64 .so to a connected device
    try:
        # Find the native library file
        so_candidates = glob.glob(os.path.join(fastbot_dir, 'libs', '**', 'libfastbot_native.so'), recursive=True)
        if so_candidates:
            so_path = so_candidates[0]
            logger.info('Found Fastbot native library at %s. Attempting to push to device.', so_path)

            # Build adb base command, optionally selecting a serial if provided
            adb_cmd = ['adb']
            serial = os.environ.get('ANDROID_SERIAL') or os.environ.get('ADB_SERIAL')
            if serial:
                adb_cmd.extend(['-s', serial])

            if not shutil.which('adb'):
                logger.warning('adb not found on PATH; skipping pushing Fastbot native library to device')
            else:
                # Create destination directory on device
                try:
                    run_cmd(adb_cmd + ['shell', 'mkdir', '-p', '/data/local/tmp/arm64-v8a/'])
                except subprocess.CalledProcessError:
                    logger.warning('Failed to create remote directory /data/local/tmp/arm64-v8a on device')

                # Push the .so file
                dst = '/data/local/tmp/arm64-v8a/libfastbot_native.so'
                try:
                    run_cmd(adb_cmd + ['push', so_path, dst])
                    logger.info('Pushed %s to device:%s', so_path, dst)
                except subprocess.CalledProcessError as exc:
                    logger.warning('Failed to push Fastbot native lib to device: %s', exc)
        else:
            logger.debug('No libfastbot_native.so found to push to device')
    except Exception:
        logger.exception('Error while attempting to push Fastbot native library to device')


def install_kea2():
    logger.info("=== Installing Kea2 ===")
    # Prefer installing from a pinned requirements file if present

    if not pip_install(['kea2-python']):
        logger.error("✗ Kea2 pip install failed.")
        return

    logger.info("✓ Kea2 installed via pip.")

    kea2_workdir = os.path.join(TOOLS_DIR, 'kea2')
    os.makedirs(kea2_workdir, exist_ok=True)
    try:
        run_cmd(['kea2', 'init'], cwd=kea2_workdir)
        logger.info("✓ Kea2 initialized in %s.", kea2_workdir)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        logger.warning("Kea2 init failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    logger.info("Starting app testing tools installation…")
    os.makedirs(TOOLS_DIR, exist_ok=True)
    # Create a dedicated venv for tools under TOOLS_DIR/venv and prefer using it
    venv_dir = os.path.join(TOOLS_DIR, 'venv')
    try:
        venv_python = create_tools_venv(venv_dir=venv_dir)
        if venv_python:
            logger.info('Using tools venv python: %s', venv_python)
        else:
            logger.warning('Tools venv not available; will fallback to system Python for pip installs')
    except Exception:
        logger.exception('Error while creating/initializing tools venv; continuing with system Python')

    install_ape()
    install_combodroid()
    install_humanoid()
    install_qtesting()
    install_timemachine()
    install_droidrun()
    install_appagentx()
    install_fastbot()
    install_kea2()

    logger.info("App testing tools installation complete. Tools directory: %s", TOOLS_DIR)


if __name__ == '__main__':
    main()
