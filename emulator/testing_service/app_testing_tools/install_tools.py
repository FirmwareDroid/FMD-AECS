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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(BASE_DIR, 'tools')


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
    base_cmd = [sys.executable, '-m', 'pip', 'install', '--no-cache-dir']
    for flags in (['--break-system-packages'], []):
        try:
            run_cmd(base_cmd + flags + extra + packages)
            return True
        except subprocess.CalledProcessError:
            pass
    logger.error("pip install failed for: %s", packages)
    return False


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

    # Install lightweight Humanoid deps
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
    if pip_install(['droidrun']):
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


def install_kea2():
    logger.info("=== Installing Kea2 ===")
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
