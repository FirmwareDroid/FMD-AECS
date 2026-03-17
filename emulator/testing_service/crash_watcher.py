"""Crash dialog watcher and helper utilities.

This module provides utilities to detect and dismiss Android crash/ANR dialogs using
`uiautomator dump` and `adb shell input tap` or fallback keyevents. It also provides
an optional background watcher thread that periodically scans for crash dialogs and
attempts to dismiss them.

Usage:
  import crash_watcher
  crash_watcher.handle_crash_dialog(device="emulator-5554")  # run once
  crash_watcher.start_crash_watcher(device="emulator-5554", interval=5)  # run in background
  crash_watcher.stop_crash_watcher()

Notes:
- This uses `adb exec-out uiautomator dump /dev/tty` to get the current window hierarchy
  as XML. Some devices may not support exec-out for uiautomator; in that case the
  function falls back to simple BACK key events to try to dismiss dialogs.
- The detection searches for common crash/ANR phrases and for known button texts such
  as 'Close app', 'Force close', 'OK', 'Wait', 'Report'. It will tap the center of the
  matching node's bounds.
"""

import subprocess
import logging
import threading
import time
import re
from typing import Optional, Tuple

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

_ADB = 'adb'
_watcher_thread = None
_watcher_stop_event = None

# Common button labels to try pressing when a dialog is found
_BUTTON_LABELS = [
    'Close app', 'Force close', 'OK', 'Wait', 'Report', 'Send', 'Dismiss', 'Close', 'Force stop'
]

# Common ANR/crash indicator texts
_ANR_CRASH_TEXTS = [
    "Application Not Responding",
    "isn't responding",
    "has stopped",
    "Unfortunately",
    "force close",
    "app isn't responding",
]


def _run_adb(cmd_args, device: Optional[str] = None, timeout: int = 8) -> Tuple[int, str, str]:
    cmd = [_ADB]
    if device:
        cmd += ['-s', device]
    cmd += cmd_args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or '', proc.stderr or ''
    except subprocess.TimeoutExpired:
        logging.debug('ADB command timed out: %s', ' '.join(cmd))
        return 124, '', 'timeout'
    except FileNotFoundError:
        logging.warning('adb not found in PATH')
        return 127, '', 'adb-not-found'


def _parse_bounds(bounds_str: str) -> Optional[Tuple[int, int, int, int]]:
    # bounds like: [0,144][1080,1776]
    m = re.findall(r"\[(\d+),(\d+)\]", bounds_str)
    if not m or len(m) < 2:
        return None
    try:
        x1, y1 = int(m[0][0]), int(m[0][1])
        x2, y2 = int(m[1][0]), int(m[1][1])
        return x1, y1, x2, y2
    except Exception:
        return None


def _tap_center(bounds: Tuple[int, int, int, int], device: Optional[str] = None) -> None:
    x1, y1, x2, y2 = bounds
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    logging.info('Tapping at %d,%d to dismiss dialog', cx, cy)
    _run_adb(['shell', 'input', 'tap', str(cx), str(cy)], device=device)


def handle_crash_dialog(device: Optional[str] = None) -> bool:
    """Check for crash/ANR dialogs and attempt to dismiss them.

    Returns True if a dialog was detected and an action was taken (tap/back); False otherwise.
    """
    # Try to dump UI hierarchy to stdout using uiautomator
    rc, out, err = _run_adb(['exec-out', 'uiautomator', 'dump', '/dev/tty'], device=device, timeout=6)
    xml = out
    if rc != 0 or not xml:
        # Fallback: try non-exec dump to /sdcard and pull (slower)
        rc2, o2, e2 = _run_adb(['shell', 'uiautomator', 'dump', '/sdcard/window_dump.xml'], device=device, timeout=8)
        if rc2 == 0:
            # pull the file
            rc3, o3, e3 = _run_adb(['exec-out', 'cat', '/sdcard/window_dump.xml'], device=device, timeout=6)
            if rc3 == 0:
                xml = o3
    if not xml:
        # Unable to get UI dump; fallback to sending BACK key to try dismissing
        logging.debug('No UI dump available; sending BACK as fallback')
        _run_adb(['shell', 'input', 'keyevent', '4'], device=device)
        return True

    # Simple search for ANR/crash indicator strings
    lower = xml.lower()
    found_indicator = any(t.lower() in lower for t in _ANR_CRASH_TEXTS)
    if not found_indicator:
        return False

    logging.info('Crash/ANR dialog text found in UI dump; attempting to dismiss')

    # Try to find a clickable node with one of the button labels
    # Basic parse: look for text="..." and bounds="[...]"
    # We'll search for occurrences of button labels in the xml and extract nearby bounds
    for label in _BUTTON_LABELS:
        # Search case-insensitive for text="label"
        pattern = re.compile(r'text\s*=\s*"(' + re.escape(label) + r')".*?bounds\s*=\s*"(\[.*?\])"', re.IGNORECASE | re.DOTALL)
        for m in pattern.finditer(xml):
            bounds_str = m.group(2)
            b = _parse_bounds(bounds_str)
            if b:
                _tap_center(b, device=device)
                return True

    # If no labeled button found, try generic clickable node parsing
    pattern2 = re.compile(r'clickable=\"(true)\".*?bounds=\"(\[.*?\])\"', re.IGNORECASE | re.DOTALL)
    for m in pattern2.finditer(xml):
        bounds_str = m.group(2)
        b = _parse_bounds(bounds_str)
        if b:
            _tap_center(b, device=device)
            return True

    # As a last resort, press BACK and HOME
    logging.info('No dialog button found; sending BACK then HOME to dismiss')
    _run_adb(['shell', 'input', 'keyevent', '4'], device=device)
    time.sleep(0.3)
    _run_adb(['shell', 'input', 'keyevent', '3'], device=device)
    return True


def _watcher_loop(device: Optional[str], interval: float, stop_event: threading.Event):
    logging.info('Starting crash watcher thread (interval=%.1fs)', interval)
    while not stop_event.is_set():
        try:
            handled = handle_crash_dialog(device=device)
            if handled:
                logging.info('Crash watcher dismissed a dialog')
        except Exception:
            logging.exception('Error in crash watcher')
        # wait with early exit
        stop_event.wait(interval)
    logging.info('Crash watcher thread exiting')


def start_crash_watcher(device: Optional[str] = None, interval: float = 5.0) -> None:
    """Start a background thread that periodically checks and dismisses crash dialogs.

    It's safe to call multiple times; subsequent calls will be ignored until stopped.
    """
    global _watcher_thread, _watcher_stop_event
    if _watcher_thread and _watcher_thread.is_alive():
        logging.debug('Crash watcher already running')
        return
    _watcher_stop_event = threading.Event()
    _watcher_thread = threading.Thread(target=_watcher_loop, args=(device, interval, _watcher_stop_event), daemon=True)
    _watcher_thread.start()


def stop_crash_watcher(timeout: float = 5.0) -> None:
    global _watcher_thread, _watcher_stop_event
    if not _watcher_thread:
        return
    _watcher_stop_event.set()
    _watcher_thread.join(timeout=timeout)
    _watcher_thread = None
    _watcher_stop_event = None

