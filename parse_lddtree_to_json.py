import logging
import subprocess
import re
import sys
import os
import json
from pathlib import Path

def parse_lddtree_output(lddtree_output: str):
    """
    Parses the plain text output of lddtree and returns a list of native libraries used.
    """
    libs = set()
    libs_not_found = set()
    pattern = re.compile(r'=>\s+(\S+)')  # matches '=> /path/to/lib.so'

    for line in lddtree_output.splitlines():
        match = pattern.search(line)
        if match:
            lib_path = match.group(1)
            if lib_path not in ('not', 'found') and not lib_path.startswith('('):
                libs.add(lib_path)
            else:
                libs_not_found.add(lib_path)

    return sorted(libs), sorted(libs_not_found)


def run_lddtree(binary_path: str, extra_env=None, cwd=None):
    """
    Runs lddtree on the given binary and returns the parsed list of libraries.
    """
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    if cwd is None:
        cwd = os.path.dirname(binary_path)

    try:
        result = subprocess.run(
            ['lddtree',
             binary_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            env=env,
            cwd=cwd
        )
    except subprocess.CalledProcessError as e:
        logging.error(f"Error running lddtree: {e.stderr}")
        raise e

    return parse_lddtree_output(result.stdout)


def main():
    """
    Example usage:
    CLI: LD_LIBRARY_PATH=./lib64:./lib64/bootstrap/ lddtree /home/suth/FMD-AECS/out/current_test/ALL_FILES/system/system/bin/app_process64
    """
    if len(sys.argv) < 2:
        print("Usage: python parse_lddtree_to_json.py <binary> [lib_dir1 lib_dir2 ...]")
        sys.exit(1)

    binary = sys.argv[1]
    extra_paths = sys.argv[2:]

    if not Path(binary).exists():
        print(f"Error: file '{binary}' not found.")
        sys.exit(1)

    # Construct LD_LIBRARY_PATH
    env = {"LD_LIBRARY_PATH": ":".join(extra_paths)} if extra_paths else None

    libs = run_lddtree(binary, extra_env=env)



if __name__ == '__main__':
    main()
