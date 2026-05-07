#!/usr/bin/env python3
"""
TimeMachine testing tool – compatibility notice.

TimeMachine (https://github.com/the-themis-benchmarks/TimeMachine) requires
VirtualBox to create and manage Android-x86 virtual machines.  VirtualBox
kernel modules CANNOT be loaded inside Docker containers, so TimeMachine is
not functional in this containerised environment.

The repository is cloned to `app_testing_tools/tools/TimeMachine` for
reference only.  To use TimeMachine:
  1. Run it on a bare-metal Linux host with VirtualBox 5.0.18 or 5.1.38.
  2. Build the Docker image: docker build -t droidtest/timemachine:1.0 .
  3. Follow the upstream README instructions.
"""

import logging
import sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def main():
    logger.error(
        "TimeMachine cannot run inside a Docker container.\n\n"
        "Reason: TimeMachine relies on VirtualBox to manage Android-x86 VMs.\n"
        "        VirtualBox kernel modules cannot be loaded inside Docker.\n\n"
        "To use TimeMachine:\n"
        "  1. Provision a bare-metal Linux host with VirtualBox 5.0.18 or 5.1.38.\n"
        "  2. Clone the repository (already at app_testing_tools/tools/TimeMachine).\n"
        "  3. Build the TimeMachine Docker image and follow its README:\n"
        "       docker build -t droidtest/timemachine:1.0 .\n"
        "  4. Run tests per the TimeMachine documentation.\n\n"
        "See: https://github.com/the-themis-benchmarks/TimeMachine"
    )
    sys.exit(1)


if __name__ == '__main__':
    main()
