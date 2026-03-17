#!/usr/bin/env python3
"""
Logcat collector (UID-based)

Usage:
  python collect_logcat.py --package com.example.app --device emulator-5554 --output out.json [--clear]

This script will:
- Optionally clear device logcat before collecting (--clear)
- Query the package UID using `adb shell dumpsys package <pkg> | grep userId`
- Dump the full logcat with `adb shell logcat -d`
- Write a JSON object to --output with fields: {"package":..., "uid":<int or null>, "logcat": "<full log as one string>"}

"""

import argparse
import subprocess
import json
import datetime
import sys
import re
import os
from typing import Optional, List

# Default output directory for collect_logcat: ./testing_service/out
DEFAULT_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'out')


def run_adb(args: List[str], capture_output=True, text=True) -> subprocess.CompletedProcess:
    cmd = ['adb'] + args
    return subprocess.run(cmd, stdout=(subprocess.PIPE if capture_output else None),
                          stderr=(subprocess.PIPE if capture_output else None), text=text)


def get_package_uid(device: Optional[str], package: str) -> Optional[int]:
    """Return the numeric UID for the package or None if not found."""
    adb_args = ['-s', device] if device else []
    try:
        proc = run_adb((adb_args or []) + ['shell', 'dumpsys', 'package', package])
    except FileNotFoundError:
        raise RuntimeError('adb not found on PATH')
    if proc.returncode != 0:
        # dumpsys can return non-zero for missing package
        return None
    out = proc.stdout or ''
    # Find lines like 'userId=1000' (may appear multiple times)
    m = re.search(r'userId=(\d+)', out)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def clear_logcat(device: Optional[str]):
    """Clear logcat buffers on the device. Uses '-b all -c' to clear all buffers when available."""
    adb_args = ['-s', device] if device else []
    # Use '-b all -c' to clear all buffers (main, system, crash, events) on modern devices.


    proc = run_adb((adb_args or []) + ['logcat', '-b', 'all' , '-c'])
    if proc.returncode != 0:
        # Fall back to plain '-c' if '-b all -c' is unsupported
        proc2 = run_adb((adb_args or []) + ['logcat', '-c'])
        if proc2.returncode != 0:
            raise RuntimeError(f'logcat clear failed: {proc.stderr.strip() or proc2.stderr.strip()}')


def dump_uid_logcat(uid: Optional[int], device: Optional[str]):
    adb_args = ['-s', device] if device else []
    proc = run_adb((adb_args or []) + ['logcat', '-d', '-b', 'all', '-v', 'uid', '-D', "--uid", uid])
    if proc.returncode != 0:
        # include stderr for debugging
        raise RuntimeError(f'logcat failed: {proc.stderr.strip()}')
    return proc.stdout or ''


def dump_full_logcat(device: Optional[str]) -> str:
    adb_args = ['-s', device] if device else []
    proc = run_adb((adb_args or []) + ['logcat', '-d', '-b', 'all', '-v', 'uid', '-D'])
    if proc.returncode != 0:
        # include stderr for debugging
        raise RuntimeError(f'logcat failed: {proc.stderr.strip()}')
    return proc.stdout or ''


def write_json_output(output_path: str, package: Optional[str], uid: Optional[int], logcat: str):
    """Append or write a JSON payload to output_path.

    Behavior:
      - If file does not exist: create it containing a single JSON object (pretty-printed).
      - If file exists and contains a JSON array: append the new object to the array (atomic replace).
      - If file exists and contains a single JSON object: convert to a JSON array [old, new] (atomic replace).
      - If file exists but is not valid JSON: append a newline-delimited JSON (NDJSON) record.
    """
    if package is None:
        payload = {
            'logcat': logcat,
            'collected_at': datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
    else:
        payload = {
            'package': package,
            'uid': uid,
            'logcat': logcat,
            'collected_at': datetime.datetime.now(datetime.timezone.utc).isoformat()
        }

    # If the file doesn't exist, create it with a single JSON object
    if not os.path.exists(output_path):
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            return
        except Exception as e:
            raise RuntimeError(f'Failed to write output file {output_path}: {e}')

    # File exists: try to read and parse it
    try:
        with open(output_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        raise RuntimeError(f'Failed to read existing output file {output_path}: {e}')

    content_stripped = content.strip()
    if not content_stripped:
        # Empty file, overwrite with the payload
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            return
        except Exception as e:
            raise RuntimeError(f'Failed to write output file {output_path}: {e}')

    # Try to parse as JSON
    try:
        existing = json.loads(content_stripped)
    except json.JSONDecodeError:
        # Not valid JSON - append as NDJSON (newline-delimited JSON)
        try:
            with open(output_path, 'a', encoding='utf-8') as f:
                # ensure newline separation
                if not content.endswith('\n'):
                    f.write('\n')
                f.write(json.dumps(payload, ensure_ascii=False))
            return
        except Exception as e:
            raise RuntimeError(f'Failed to append NDJSON to {output_path}: {e}')

    # If existing is a list, append and write atomically
    if isinstance(existing, list):
        existing.append(payload)
        tmp_path = output_path + '.tmp'
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, output_path)
            return
        except Exception as e:
            # Cleanup tmp if present
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise RuntimeError(f'Failed to update JSON array in {output_path}: {e}')

    # If existing is an object/dict, convert to array [old, new]
    if isinstance(existing, dict):
        new_list = [existing, payload]
        tmp_path = output_path + '.tmp'
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(new_list, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, output_path)
            return
        except Exception as e:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise RuntimeError(f'Failed to convert existing JSON object to array in {output_path}: {e}')

    # Fallback: append as NDJSON
    try:
        with open(output_path, 'a', encoding='utf-8') as f:
            if not content.endswith('\n'):
                f.write('\n')
            f.write(json.dumps(payload, ensure_ascii=False))
        return
    except Exception as e:
        raise RuntimeError(f'Failed to append fallback NDJSON to {output_path}: {e}')


def main():
    parser = argparse.ArgumentParser(description='Collect full logcat logs for an Android package and store as JSON')
    parser.add_argument('--package', '-p', required=False, help='Android package name (e.g., com.example.app)')
    parser.add_argument('--device', '-s', required=False, help='ADB device serial (e.g., emulator-5554)')
    parser.add_argument('--output', '-o', required=False, help='Output JSON file path')
    parser.add_argument('--clear', action='store_true', help='Clear device logcat before collecting')
    parser.add_argument('--flush', action='store_true', help='Clear device logcat after collecting (flush buffers)')
    parser.add_argument('--full-dump', action='store_true', help='Create a full logcat dump')
    args = parser.parse_args()
    print(f"Start Logcat Collector")
    try:
        # If flush is specified and neither package nor output is given, perform flush-only mode
        if args.flush and not args.package and not args.output:
            print(f'Flushing (clearing) device logcat on device {args.device or "default"}...')
            clear_logcat(args.device)
            print('Flush complete.')
            return

        if args.full_dump:
            print('Dumping full logcat...')
            full_log = dump_full_logcat(args.device)
            # default output path inside DEFAULT_OUT_DIR
            out_dir = DEFAULT_OUT_DIR
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, 'logcat_full_dump.json')
            # write as JSON payload (package=None indicates a generic dump)
            write_json_output(out_path, package=None, uid=None, logcat=full_log)
            print(f'Wrote logs to {out_path} (entries length: {len(full_log)} characters)')
            return

        # Otherwise, require package; output defaults to DEFAULT_OUT_DIR/<package>.json
        if not args.package:
            parser.error('The --package argument is required for collection. Use --flush alone to clear device logcat without providing package/output.')
        # Determine output path
        out_path = args.output
        if not out_path:
            # default output filename in DEFAULT_OUT_DIR
            os.makedirs(DEFAULT_OUT_DIR, exist_ok=True)
            out_path = os.path.join(DEFAULT_OUT_DIR, f'{args.package}.json')
        else:
            # ensure parent dir exists
            os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)

        print(f'Querying UID for package {args.package} on device {args.device or "default"}...')
        uid = get_package_uid(args.device, args.package)
        if uid is not None:
            print(f'Found UID: {uid}')
            full_log = dump_full_logcat(args.device)
        else:
            full_log = dump_uid_logcat(uid, args.device)
            print('UID not found for package; continuing to dump full logcat')
        write_json_output(out_path, args.package, uid, full_log)
        
        if args.clear:
            print('Clearing device logcat before collection...')
            clear_logcat(args.device)



        if args.flush:
            try:
                print('Flushing (clearing) device logcat after collection...')
                clear_logcat(args.device)
                print('Flush complete.')
            except Exception as e:
                print(f'Warning: failed to flush logcat after collection: {e}', file=sys.stderr)
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()

