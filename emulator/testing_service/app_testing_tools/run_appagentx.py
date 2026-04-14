#!/usr/bin/env python3
"""
AppAgentX testing tool wrapper.

Launches the AppAgentX Gradio demo for LLM-based autonomous Android app
testing (evolutionary GUI agent).

Prerequisites:
  - AppAgentX cloned by install_tools.py (tools/AppAgentX)
  - config.py configured with:
      * LLM API key (OpenAI, DeepSeek, or compatible)
      * Neo4j database connection details
      * Pinecone vector-store API key
  - Backend Docker services for screen recognition (see AppAgentX/backend/README.md)
  - ADB-connected device or emulator

Usage:
    python3 run_appagentx.py
"""

import logging
import os
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APPAGENTX_DIR = os.path.join(BASE_DIR, 'tools', 'AppAgentX')
DEMO_SCRIPT = os.path.join(APPAGENTX_DIR, 'demo.py')
CONFIG_PY = os.path.join(APPAGENTX_DIR, 'config.py')


def main():
    if not os.path.isdir(APPAGENTX_DIR):
        logger.error("AppAgentX not found at %s. Run install_tools.py first.", APPAGENTX_DIR)
        sys.exit(1)

    if not os.path.exists(DEMO_SCRIPT):
        logger.error("AppAgentX demo.py not found at %s.", DEMO_SCRIPT)
        sys.exit(1)

    if not os.path.exists(CONFIG_PY):
        logger.error(
            "AppAgentX config.py not found at %s.\n"
            "Please configure it with your LLM API key, Neo4j credentials, "
            "and Pinecone API key before running.",
            CONFIG_PY,
        )
        sys.exit(1)

    logger.warning(
        "AppAgentX requires the following external services to be running:\n"
        "  1. Neo4j database (configure connection in config.py)\n"
        "  2. Pinecone vector store (API key in config.py)\n"
        "  3. Backend Docker services for screen recognition "
        "(see tools/AppAgentX/backend/README.md)\n"
        "  4. LLM API access (OpenAI / DeepSeek – key in config.py)\n"
        "Ensure these are configured before proceeding."
    )

    cmd = [sys.executable, DEMO_SCRIPT]
    logger.info("Launching AppAgentX Gradio demo…")
    sys.exit(subprocess.run(cmd, cwd=APPAGENTX_DIR, text=True).returncode)


if __name__ == '__main__':
    main()
