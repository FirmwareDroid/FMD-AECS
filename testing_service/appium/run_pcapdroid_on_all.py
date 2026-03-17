#!/usr/bin/env python3
"""Run PCAPdroid on all connected adb devices via Appium and start the VPN capture.

Usage:
  python3 run_pcapdroid_on_all.py --appium-url http://localhost:4723/wd/hub \
      --app-package com.example.pcapdroid --app-activity .MainActivity

Notes:
- Appium server must be running and reachable at --appium-url.
- Provide the correct --app-package and --app-activity for PCAPdroid on your devices.
- The script will try a few common button texts/ids to locate the "Start VPN" control; adjust with
  --start-locators if needed.
- Run as a user who can access adb and connect to devices.
"""

from __future__ import annotations
import argparse
import json
import logging
import subprocess
import sys
import time
from typing import List, Dict

from appium import webdriver
from appium.webdriver.common.appiumby import AppiumBy
from appium.options.android import UiAutomator2Options
# TouchAction may not be installed/available with some appium client versions; import defensively via importlib
import importlib
try:
    _mod = importlib.import_module('appium.webdriver.common.touch_action')
    TouchAction = getattr(_mod, 'TouchAction', None)
except Exception:
    TouchAction = None
import urllib.request
import urllib.error

# Start: add crash_watcher import (optional)
# Make import robust when this script is executed from the appium/ subfolder
crash_watcher = None
try:
    # Prefer package-style import when running from project root
    from testing_service import crash_watcher as _cw
    crash_watcher = _cw
    logging.info("Crash watcher imported via testing_service.crash_watcher")
except Exception:
    try:
        # Fallback to top-level module import
        import crash_watcher as _cw
        crash_watcher = _cw
        logging.info("Crash watcher imported as top-level module")
    except Exception:
        # As a last resort, try to add the parent directory of this file to sys.path and import
        try:
            import os
            sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
            import crash_watcher as _cw
            crash_watcher = _cw
            logging.info("Crash watcher imported after prepending parent dir to sys.path")
        except Exception:
            logging.debug("Could not import crash_watcher module; crash watcher disabled", exc_info=True)
            crash_watcher = None

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"


def run_cmd(cmd: List[str], check: bool = True, capture: bool = True):
    completed = subprocess.run(cmd, stdout=subprocess.PIPE if capture else None,
                               stderr=subprocess.PIPE if capture else None, text=True)
    out = completed.stdout if capture else None
    err = completed.stderr if capture else None
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, cmd, output=out, stderr=err)
    return completed.returncode, out, err


def list_connected_devices(adb_cmd: str = "adb") -> List[str]:
    rc, out, err = run_cmd([adb_cmd, "devices"], check=True)
    devices: List[str] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def set_toggle_by_label(driver, label, max_scroll_attempts=5, wait_time=0.5):
    """
    Scrolls the settings page to find the toggle by label and clicks it.
    Args:
        driver: Appium driver instance
        label: Text label to search for
        max_scroll_attempts: Maximum scroll attempts
        wait_time: Wait time after click
    Returns:
        True if toggle was found and clicked, False otherwise
    """
    is_success = False
    for attempt in range(max_scroll_attempts):
        handle_crash_dialog(driver, timeout=2.0)
        try:
            # Try to find the element by text
            elem = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().textContains("{label}")')
            elem.click()
            #time.sleep(wait_time)#
            is_success =  True
            break
        except Exception:
            # Scroll to try to find the element
            try:
                scroll_cmd = f'new UiScrollable(new UiSelector().scrollable(true)).scrollIntoView(new UiSelector().textContains("{label}"))'
                elem = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, scroll_cmd)
                elem.click()
                #time.sleep(wait_time)
                is_success = True
                break
            except Exception:
                time.sleep(0.5)
    return is_success


def resolve_appium_endpoint(base_url: str, timeout: float = 2.0) -> str:
    """Probe Appium server and return a working base URL for sessions.

    Appium v3 expects the WebDriver endpoints at the server root (e.g. http://host:4723),
    while older Appium used /wd/hub. This function tries /status on both locations and
    returns the appropriate base URL (without trailing slash).
    """
    if not base_url:
        return base_url
    # normalize: if user passed a URL containing '/wd/hub', strip it to probe the server root first
    base = base_url.rstrip('/')
    if base.endswith('/wd/hub'):
        base_root = base[: -len('/wd/hub')]
    else:
        base_root = base
    base_root = base_root.rstrip('/')
    # Try probing the server root (/) first since Appium v3 often responds with 404 on `/` but is alive.
    candidates = [(f"{base_root}", "/"), (f"{base_root}", "/status"), (f"{base_root}/wd/hub", "/status")]
    probe_timeout = max(timeout, 5.0)
    for candidate_base, path in candidates:
        url = candidate_base.rstrip('/') + path
        try:
            req = urllib.request.Request(url, method='GET')
            with urllib.request.urlopen(req, timeout=probe_timeout) as resp:
                code = resp.getcode()
                # For root path we accept 200..499 as server is responding (404 is common for '/').
                if path == '/' and 200 <= code < 600:
                    logging.debug("Appium root responded at %s (code=%s) - using base %s", url, code, candidate_base)
                    return candidate_base.rstrip('/')
                if 200 <= code < 300:
                    logging.debug("Appium status OK at %s (using base %s)", url, candidate_base)
                    return candidate_base.rstrip('/')
        except urllib.error.HTTPError as e:
            # HTTP error like 404 on /status; accept root 404 above, otherwise try next candidate
            logging.debug("Appium probe HTTP error for %s: %s", url, e)
            continue
        except Exception as e:
            logging.debug("Appium probe failed for %s: %s", url, e)
            continue
    # fallback: return original base (no change)
    logging.warning("Could not detect Appium endpoint variant, falling back to provided URL: %s", base)
    return base


def start_appium_session_for_device(appium_url: str, udid: str, app_package: str, app_activity: str, timeout: int = 30):
    opts = UiAutomator2Options()
    opts.platform_name = "Android"
    opts.automation_name = "uiautomator2"
    opts.udid = udid
    opts.device_name = udid
    # set appPackage/appActivity only if provided
    if app_package:
        opts.app_package = app_package
    if app_activity:
        # ensure activity string doesn't include package prefix if user passed combined form
        if app_activity.startswith(".") or "/" in app_activity or "." in app_activity:
            opts.app_activity = app_activity
        else:
            opts.app_activity = app_activity
    opts.no_reset = True
    opts.new_command_timeout = 60

    logging.info("Resolving Appium endpoint for %s", appium_url)
    resolved_base = resolve_appium_endpoint(appium_url)
    logging.info("Creating Appium session for %s using Appium base %s (app %s/%s)", udid, resolved_base, app_package, app_activity)
    # normalize to avoid '/wd/hub' being included (Appium v3 expects root URL)
    if resolved_base.endswith('/wd/hub'):
        resolved_base = resolved_base.replace('/wd/hub', '').rstrip('/')
    if resolved_base.endswith('/'):
        resolved_base = resolved_base.rstrip('/')

    last_exc = None
    driver = None
    # attempt primary
    try:
        logging.debug("Attempting Appium Remote with URL: %s", resolved_base)
        driver = webdriver.Remote(command_executor=resolved_base, options=opts)
    except Exception as e:
        logging.warning("Appium session creation failed with base %s: %s", resolved_base, e)
        last_exc = e
    # attempt alternate with '/wd/hub' appended (for older Appium servers) if primary failed
    if driver is None:
        try:
            alt = resolved_base + '/wd/hub'
            logging.debug("Retrying Appium Remote with alt URL: %s", alt)
            driver = webdriver.Remote(command_executor=alt, options=opts)
        except Exception as e:
            logging.error("Appium session creation failed with both URLs: %s and %s", resolved_base, resolved_base + '/wd/hub')
            # raise the last exception to the caller
            raise last_exc or e

    # wait for activity (driver should be set here)
    end = time.time() + timeout
    active = None
    while time.time() < end:
        try:
            active = driver.current_activity
            logging.debug("Device %s current_activity=%s", udid, active)
            break
        except Exception:
            time.sleep(0.5)
    return driver


def try_click_start(driver, locators: List[Dict], timeout: int = 10) -> bool:
    """Try list of locators until one succeeds. Locator dict has keys: strategy, value."""
    end = time.time() + timeout
    while time.time() < end:
        for loc in locators:
            try:
                strategy = loc.get("strategy")
                value = loc.get("value")
                if strategy == "id":
                    el = driver.find_element(AppiumBy.ID, value)
                elif strategy == "accessibility_id":
                    el = driver.find_element(AppiumBy.ACCESSIBILITY_ID, value)
                elif strategy == "uiautomator":
                    el = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, value)
                else:
                    # default to text search using XPath
                    el = driver.find_element(AppiumBy.XPATH, value)
                if el:
                    logging.info("Found start element using %s=%s - clicking", strategy, value)
                    el.click()
                    return True
            except Exception:
                # ignore and try next locator
                continue
        time.sleep(0.5)
    return False


def handle_permission_dialogs(driver, timeout: int = 10) -> None:
    # try common text buttons
    buttons = ["Allow", "OK", "Confirm", "Continue", "Start"]
    end = time.time() + timeout
    while time.time() < end:
        for b in buttons:
            try:
                el = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{b}")')
                if el:
                    logging.info("Clicking permission button '%s'", b)
                    el.click()
                    time.sleep(0.3)
            except Exception:
                continue
        time.sleep(0.3)


def handle_pcapdroid_initial_prompts(driver, timeout: int = 6) -> None:
    """Handle PCAPdroid initial fragment that shows the title 'Viewing full screen' and buttons
    'Got it' then 'SKIP'. This will try a few selector variants and tolerate missing elements.
    """
    end = time.time() + timeout
    clicked_any = False
    while time.time() < end:
        try:
            # If the fragment title is present, proceed to look for buttons
            try:
                title = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Viewing full screen")')
            except Exception:
                title = None

            # Try to click 'Got it' (common variants)
            got_it_candidates = ["Got it", "GOT IT", "Got it!", "Got It"]
            for txt in got_it_candidates:
                try:
                    btn = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{txt}")')
                    if btn:
                        logging.info("PCAPdroid prompt: clicking '%s'", txt)
                        btn.click()
                        clicked_any = True
                        time.sleep(0.4)
                        break
                except Exception:
                    continue

            # Also attempt XPath contains as fallback
            if not clicked_any:
                try:
                    btn = driver.find_element(AppiumBy.XPATH, '//*[contains(@text, "Got it") or contains(@text, "GOT IT") or contains(@text, "Got it!")]')
                    if btn:
                        logging.info("PCAPdroid prompt (xpath): clicking Got it")
                        btn.click()
                        clicked_any = True
                        time.sleep(0.4)
                except Exception:
                    pass

            # After Got it, there's a 'SKIP' button to dismiss another screen
            # Try a few variants
            skip_candidates = ["SKIP", "Skip", "Skip >", "SKIP >"]
            for s in skip_candidates:
                try:
                    skip_btn = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{s}")')
                    if skip_btn:
                        logging.info("PCAPdroid prompt: clicking '%s'", s)
                        skip_btn.click()
                        time.sleep(0.3)
                        return
                except Exception:
                    continue

            # XPath fallback for SKIP
            try:
                skip_btn = driver.find_element(AppiumBy.XPATH, '//*[contains(@text, "Skip") or contains(@text, "SKIP")]')
                if skip_btn:
                    logging.info("PCAPdroid prompt (xpath): clicking Skip")
                    skip_btn.click()
                    time.sleep(0.3)
                    return
            except Exception:
                pass

            # If neither found yet, break if title not present (no fragment)
            if title is None and not clicked_any:
                # likely nothing to do
                return
        except Exception as e:
            logging.debug("handle_pcapdroid_initial_prompts: exception while probing: %s", e)
        time.sleep(0.4)
    return


def handle_crash_dialog(driver, timeout: float = 2.0) -> bool:
    """Detect and dismiss crash/ANR dialogs by clicking 'Close app' (or similar) if they appear.

    Returns True if a dialog was found and dismissed.
    """
    end = time.time() + timeout
    # common button texts to close a crash dialog
    close_texts = ["Close app", "Close", "OK", "Dismiss", "Force close", "Close application"]
    # Some dialogs may include 'has stopped' or 'keeps stopping' in the message
    message_keywords = ["stopped", "has stopped", "keeps stopping", "isn't responding", "has stopped unexpectedly", "Unfortunately"]
    while time.time() < end:
        try:
            # First try to find the explicit close button by text
            for t in close_texts:
                try:
                    btn = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{t}")')
                    if btn:
                        logging.info("Crash dialog detected: clicking '%s'", t)
                        btn.click()
                        time.sleep(0.5)
                        return True
                except Exception:
                    continue

            # fallback: search for any element that likely represents the dialog 'Close app' by contains
            try:
                xpath_btn = driver.find_element(AppiumBy.XPATH, '//*[contains(@text, "Close app") or contains(@text, "Close") or contains(@text, "Force close") or contains(@text, "OK")]')
                if xpath_btn:
                    logging.info("Crash dialog detected (xpath fallback): clicking")
                    xpath_btn.click()
                    time.sleep(0.5)
                    return True
            except Exception:
                pass

            # Another strategy: detect the dialog message text and then click the second button (common layout)
            for kw in message_keywords:
                try:
                    # find any element that contains the keyword in its text
                    msg = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().textContains("{kw}")')
                    if msg:
                        # attempt to click 'Close app' by text or fallback to first clickable sibling
                        for t in close_texts:
                            try:
                                btn = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{t}")')
                                if btn:
                                    logging.info("Crash dialog message detected ('%s'): clicking '%s'", kw, t)
                                    btn.click()
                                    time.sleep(0.5)
                                    return True
                            except Exception:
                                continue
                        # xpath fallback to click any button element under dialog
                        try:
                            btn_any = driver.find_element(AppiumBy.XPATH, '//*[(@clickable="true" or @class) and (contains(@resource-id, "button") or contains(@text, "Close") or contains(@text, "OK"))]')
                            if btn_any:
                                logging.info("Crash dialog message detected ('%s'): clicking fallback button", kw)
                                btn_any.click()
                                time.sleep(0.5)
                                return True
                        except Exception:
                            pass
                except Exception:
                    continue
        except Exception as e:
            logging.debug("handle_crash_dialog: probe exception: %s", e)
        time.sleep(0.25)
    return False


def start_pcapdroid_on_device_with_settings(appium_url: str,
                                           device_serial: str,
                                           app_package: str,
                                           app_activity: str,
                                           start_locators: List[Dict],
                                           settings_retries: int = 10,
                                           abort_on_settings_fail: bool = True,
                                           socks_ip: str = "",
                                           http_port: int = 54320) -> Dict:
    """Wrapper that ensures settings configuration is attempted and, depending on flags, will abort the test for the device if not successful."""
    # Start device and run normal flow
    result = {"device": device_serial, "ok": False, "messages": []}
    driver = None
    dumping_mode_ok = False  # Initialize to avoid unassigned reference
    try:
        driver = start_appium_session_for_device(appium_url, device_serial, app_package, app_activity)
        result['messages'].append(f"App started, current_activity={driver.current_activity}")
        # initial dismissals
        try:
            if handle_crash_dialog(driver, timeout=2.0):
                result['messages'].append("Dismissed crash dialog after app start")
        except Exception as e:
            logging.debug("Error while handling crash dialog after start: %s", e)
        handle_permission_dialogs(driver, timeout=3)
        try:
            handle_pcapdroid_initial_prompts(driver, timeout=6)
        except Exception as e:
            logging.debug("Error while handling pcapdroid initial prompts: %s", e)


        # Set Dumping mode to 'HTTP server'
        for attempt in range(1, max(1, settings_retries) + 1):
            try:
                    dumping_mode_ok = set_dumping_mode(driver)
                    if not dumping_mode_ok:
                        time.sleep(1)
                        logging.error("configure_pcapdroid_settings: failed to set Dumping mode to 'HTTP server'")
                    else:
                        break
                    if handle_crash_dialog(driver, timeout=2.0):
                        result['messages'].append("Dismissed crash dialog after app start")
            except Exception as e:
                logging.error('configure_pcapdroid_settings: error setting Dumping mode: %s', e)
                time.sleep(1)

        if not dumping_mode_ok and abort_on_settings_fail:
            msg = f"Could not configure dump mode to HTTP server; aborting per configuration"
            logging.error(msg)
            result['messages'].append(msg)
            result['ok'] = False
            return result

        # Now try to open settings with retries - this is mandatory by default
        cfg_ok = False
        for attempt in range(1, max(1, settings_retries) + 1):
            try:
                logging.info('configure_pcapdroid_settings: attempt %d/%d', attempt, settings_retries)
                cfg_ok = run_open_settings(driver)
                if cfg_ok:
                    logging.info('opening settings page: succeeded on attempt %d', attempt)
                    break
                logging.warning('opening settings page: attempt %d returned False', attempt)
            except Exception as e:
                logging.debug('opening settings page: exception on attempt %d: %s', attempt, e)
            time.sleep(0.8)

        if not cfg_ok and abort_on_settings_fail:
            msg = f"Could not open/configure settings after {settings_retries} attempts; aborting per configuration"
            logging.error(msg)
            result['messages'].append(msg)
            result['ok'] = False
            return result

        cfg_ok = configure_pcapdroid_settings(driver, socks_ip, http_port)
        if cfg_ok:
            logging.info('configure_pcapdroid_settings: succeeded', )
        else:
            logging.debug('configure_pcapdroid_settings: settings configuration failed')

        # If not configured but abort disabled, record a warning and continue
        if not cfg_ok and abort_on_settings_fail:
            msg = f"Could not open/configure settings after {settings_retries} attempts; continuing without configuration"
            logging.warning(msg)
            result['messages'].append(msg)
            return result

        # one more attempt to dismiss crash dialog after prompts
        try:
            if handle_crash_dialog(driver, timeout=1.0):
                result['messages'].append("Dismissed crash dialog after prompts")
        except Exception as e:
            logging.debug("Error while handling crash dialog after prompts: %s", e)

        try:
            driver.back()
            time.sleep(0.4)
        except Exception:
            pass

        # try to click start button "READY"
        # clicked = try_click_start(driver, start_locators, timeout=12)
        for attempt in range(1, max(1, settings_retries) + 1):
            try:
                if handle_crash_dialog(driver, timeout=1.0):
                    result['messages'].append("Dismissed crash dialog after starting capture")
                el = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("Ready")')
                el.click()
                if handle_crash_dialog(driver, timeout=1.0):
                    result['messages'].append("Dismissed crash dialog after starting capture")
                handle_permission_dialogs(driver, timeout=6)
                handle_permission_dialogs(driver, timeout=6)
                result['messages'].append("Started capture")
                result['ok'] = True
                break
            except Exception as e:
                result['ok'] = False


    except Exception as e:
        logging.error(f"Error while starting PCAPdroid on {device_serial}: {e}")
        result['messages'].append(str(e))
        result['ok'] = False
    finally:
        if driver:
            try:
                logging.info(f"Closing app {app_package} on device {device_serial}")
                driver.press_keycode(3)
            except Exception as close_err:
                logging.warning(f"Failed to close app {app_package} on device {device_serial}: {close_err}")
            try:
                driver.quit()
            except Exception as quit_err:
                logging.warning(f"Failed to quit Appium session for device {device_serial}: {quit_err}")
    return result

def set_vpn_ip_addresses(driver, label="addresses", selection="IPv4 and IPv6", max_attempts=20):
    """
    Scroll to the VPN IP addresses label and select the desired option.
    Uses multiple heuristics for robust detection.
    """
    attempts = 0
    is_success = False
    while attempts < max_attempts:
        try:
            # elem = driver.find_element(AppiumBy.XPATH, f'//*[contains(@text, "{label}")]')
            scroll_cmd = f'new UiScrollable(new UiSelector().scrollable(true)).scrollIntoView(new UiSelector().textContains("{label}"))'
            elem = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, scroll_cmd)
            elem.click()
            time.sleep(2)
            selection_elem = driver.find_element(AppiumBy.XPATH, f'//*[contains(@text, "{selection}")]')
            selection_elem.click()
            found_label = True
            time.sleep(2)
            is_success = True
            break
        except Exception as e:
            attempts += 1
            time.sleep(2)
    return is_success


def open_settings_page(driver, timeout: float = 5.0) -> bool:
    """Open the settings page via the top navigation settings icon or overflow menu.

    Returns True if settings page seems open.
    """
    end = time.time() + timeout

    def verify_settings_open() -> bool:
        # Verify by presence of known settings labels
        try:
            driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().text("Full payload")')
            return True
        except Exception:
            pass
        try:
            driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("VPN IP")')
            return True
        except Exception:
            pass
        try:
            driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Settings")')
            return True
        except Exception:
            return False

    # 1) Try direct settings icon (content-desc / id)
    icon_selectors = [
        (AppiumBy.ACCESSIBILITY_ID, 'Settings'),
        (AppiumBy.ACCESSIBILITY_ID, 'settings'),
        (AppiumBy.ID, 'com.emanuelef.remote_capture:id/action_settings'),
        (AppiumBy.XPATH, '//android.widget.ImageView[contains(@content-desc, "Settings") or contains(@resource-id, "settings") ]'),
    ]
    for strategy, value in icon_selectors:
        try:
            el = driver.find_element(strategy, value)
            if el:
                logging.info('Attempting to open settings via selector %s=%s', strategy, value)
                try:
                    el.click()
                except Exception:
                    try:
                        driver.execute_script('mobile: click', {'element': el.id})
                    except Exception:
                        pass
                time.sleep(0.6)
                if verify_settings_open():
                    logging.info('Settings page verified after direct icon click')
                    return True
                else:
                    logging.debug('Direct icon click did not open settings (likely opened other page). Trying overflow menu')
                    try:
                        driver.back()
                        time.sleep(0.3)
                    except Exception:
                        pass
        except Exception:
            continue

    # 2) Try overflow / more options -> select 'Settings' menu item
    overflow_selectors = [
        (AppiumBy.ACCESSIBILITY_ID, 'More options'),
        (AppiumBy.ACCESSIBILITY_ID, 'More'),
        (AppiumBy.ID, 'android:id/action_bar_overflow'),
        (AppiumBy.XPATH, '//android.widget.ImageButton[contains(@content-desc, "More") or contains(@resource-id, "overflow") or contains(@content-desc, "More options")]'),
    ]
    for strategy, value in overflow_selectors:
        try:
            ov = driver.find_element(strategy, value)
            if ov:
                logging.info('Clicking overflow menu via %s=%s', strategy, value)
                try:
                    ov.click()
                except Exception:
                    try:
                        driver.execute_script('mobile: click', {'element': ov.id})
                    except Exception:
                        pass
                time.sleep(0.4)
                # look for a menu item named 'Settings'
                menu_texts = ['Settings', 'settings', 'App settings']
                for mt in menu_texts:
                    try:
                        menu_item = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{mt}")')
                        if menu_item:
                            logging.info('Selecting Settings from overflow menu by text: %s', mt)
                            menu_item.click()
                            time.sleep(0.6)
                            if verify_settings_open():
                                logging.info('Settings page verified after overflow menu')
                                return True
                            else:
                                logging.debug('Selected Settings but verification failed')
                    except Exception:
                        continue
        except Exception:
            continue

    logging.debug('open_settings_page: could not open settings via known selectors')
    return False


def set_dumping_mode(driver, mode_label="HTTP", logger=None, timeout=10):
    """
    Set the dumping mode by clicking the 'No dump' label on the main activity and selecting the desired mode from the pop-up list.
    Args:
        driver: Appium webdriver instance.
        mode_label: The label of the dumping mode to select (default: 'HTTP server').
        logger: Logger instance for logging (optional).
        timeout: Timeout in seconds for finding elements.
    Returns:
        True if the dumping mode was set successfully, False otherwise.
    """
    try:
        # Find and click the 'No dump' label on the main activity
        if logger:
            logger.info(f"set_dumping_mode: looking for 'No dump' label on main activity")
        no_dump_elem = None
        try:
            no_dump_elem = driver.find_element(by=AppiumBy.ANDROID_UIAUTOMATOR, value='new UiSelector().textContains("No dump")')
        except Exception as e:
            if logger:
                logger.error(f"set_dumping_mode: could not find 'No dump' label: {e}")
            # leave result as False
        else:
            if no_dump_elem:
                try:
                    no_dump_elem.click()
                    if logger:
                        logger.info(f"set_dumping_mode: clicked 'No dump' label, waiting for mode selection pop-up")
                except Exception as e:
                    if logger:
                        logger.error(f"set_dumping_mode: failed to click 'No dump' label: {e}")
            else:
                if logger:
                    logger.error(f"set_dumping_mode: 'No dump' label not found on main activity")

        # Wait for the pop-up and select the desired mode
        time.sleep(0.5)
        mode_elem = None
        try:
            mode_elem = driver.find_element(by=AppiumBy.ANDROID_UIAUTOMATOR, value=f'new UiSelector().textContains("{mode_label}")')
        except Exception as e:
            if logger:
                logger.error(f"set_dumping_mode: could not find mode '{mode_label}' in pop-up: {e}")
            mode_elem = None

        if mode_elem:
            try:
                mode_elem.click()
                if logger:
                    logger.info(f"set_dumping_mode: selected dumping mode '{mode_label}'")
                result = True
            except Exception as e:
                if logger:
                    logger.error(f"set_dumping_mode: failed to click mode element '{mode_label}': {e}")
                result = False
        else:
            if logger:
                logger.error(f"set_dumping_mode: mode '{mode_label}' not found in pop-up")
            result = False
    except Exception as e:
        if logger:
            logger.error(f"set_dumping_mode: unexpected error: {e}")
        result = False
    return result


def set_socks_5_proxy_setting(driver, socks_ip, max_retries=10):
    is_success = False
    is_toggled = False
    first_clicked = False
    for i in range(max_retries):
        try:
                handle_crash_dialog(driver, timeout=2.0)
                socks_elem = driver.find_element(by=AppiumBy.ANDROID_UIAUTOMATOR, value='new UiSelector().textContains("SOCKS5")')
                socks_elem.click()
                first_clicked = True
                time.sleep(0.6)
                handle_crash_dialog(driver, timeout=2.0)
                if not is_toggled:
                    is_toggled = set_toggle_by_label(driver, label="SOCKS5", max_scroll_attempts=10, wait_time=1.5)
                for x in range(max_retries):
                    handle_crash_dialog(driver, timeout=2.0)
                    host_elem = driver.find_element(by=AppiumBy.ANDROID_UIAUTOMATOR, value='new UiSelector().textContains("host")')
                    host_elem.click()
                    set_text_field(driver, value=socks_ip)
                    is_success = True
                    break
                break
        except Exception as e:
            is_success = False
            time.sleep(1)
            if first_clicked:
                driver.back()
                first_clicked = False


    return is_success



def run_open_settings(driver):
    logging.info('configure_pcapdroid_settings: attempting to open settings')
    opened = open_settings_page(driver, timeout=6.0)
    logging.info('configure_pcapdroid_settings: open_settings_page returned %s', opened)
    if not opened:
        logging.warning('configure_pcapdroid_settings: settings page not opened; aborting configuration')
        # Try to dismiss crash dialog if present
        if handle_crash_dialog(driver, timeout=2.0):
            logging.info('configure_pcapdroid_settings: crash dialog detected and dismissed after failed settings open')
        else:
            logging.warning(
                'configure_pcapdroid_settings: settings page not opened and no crash dialog found; skipping configuration')
        return False
    time.sleep(0.6)
    return True


def configure_pcapdroid_settings(driver, socks_ip, http_port) -> bool:
    """Open settings and configure required toggles for PCAPdroid. Returns True if succeeded."""
    try:
        toggles = [
            'Full',
            'PCAPDroid',
            'boot',
            'Restart',
        ]
        toggle_results = {}
        all_ok = True

        # Set HTTP server port
        try:
            if handle_crash_dialog(driver, timeout=2.0):
                logging.info("Dismissed crash dialog after app start")
            ok_vpn = set_http_server_port(driver, port=http_port, max_attempts=10)
            logging.info('configure_pcapdroid_settings: set HTTP Port -> %s', ok_vpn)
        except Exception as e:
            logging.error('configure_pcapdroid_settings: error setting VPN IP addresses: %s', e)
            all_ok = False

        # Set Toggle Buttons
        for t in toggles:
            try:
                if handle_crash_dialog(driver, timeout=2.0):
                    logging.info("Dismissed crash dialog after app start")
                ok = set_toggle_by_label(driver, t)
                toggle_results[t] = bool(ok)
                logging.info('configure_pcapdroid_settings: set toggle %s -> %s', t, ok)
                if not ok:
                    logging.error('configure_pcapdroid_settings: toggle "%s" could not be set to True', t)
                    all_ok = False
            except Exception as e:
                logging.error('configure_pcapdroid_settings: error setting %s: %s', t, e)
                toggle_results[t] = False
                all_ok = False

        # Set SOCKS5 Proxy
        try:
            if handle_crash_dialog(driver, timeout=2.0):
                logging.info("Dismissed crash dialog after app start")
            ok_socks = set_socks_5_proxy_setting(driver, socks_ip)
            logging.info('configure_pcapdroid_settings: set SOCKS5 -> %s', ok_socks)
            if not ok_socks:
                logging.error('configure_pcapdroid_settings: SOCKS5 could not be set')
                all_ok = False
        except Exception as e:
            logging.error('configure_pcapdroid_settings: error setting SOCKS5: %s', e)
            all_ok = False
        try:
            driver.back()
            time.sleep(0.4)
        except Exception:
            pass

        # Set VPN IP addresses to 'IPv4 and IPv6'
        try:
            if handle_crash_dialog(driver, timeout=2.0):
                logging.info("Dismissed crash dialog after app start")
            ok_vpn = set_vpn_ip_addresses(driver, label="VPN IP addresses", selection="IPv4 and IPv6", max_attempts=5)
            logging.info('configure_pcapdroid_settings: set VPN IP addresses -> %s', ok_vpn)
            if not ok_vpn:
                logging.error('configure_pcapdroid_settings: VPN IP addresses could not be set to IPv4 and IPv6')
                all_ok = False
        except Exception as e:
            logging.error('configure_pcapdroid_settings: error setting VPN IP addresses: %s', e)
            all_ok = False
    except Exception as e:
        logging.error('configure_pcapdroid_settings: unexpected error: %s', e)
        return False
    # require that every toggle plus VPN selection succeeded
    if not all_ok:
        logging.error('configure_pcapdroid_settings: not all toggles were set successfully: %s', toggle_results)
        return False
    return True

def set_text_field(driver, value=""):
    text_field = None
    try:
        text_field = driver.find_element(by=AppiumBy.CLASS_NAME, value="android.widget.EditText")
    except Exception:
        # Try fallback by resource id or other heuristics
        text_field = driver.find_element(by=AppiumBy.XPATH, value='//android.widget.EditText')
    if text_field:
        text_field.clear()
        text_field.send_keys(value)
        time.sleep(0.5)
        # Find and click the OK button
        ok_button = None
        try:
            ok_button = driver.find_element(by=AppiumBy.ANDROID_UIAUTOMATOR, value='new UiSelector().text("OK")')
        except Exception:
            ok_button = driver.find_element(by=AppiumBy.XPATH, value='//*[contains(@text, "OK")]')
        if ok_button:
            ok_button.click()
            time.sleep(0.5)
            return True
    return False


def set_http_server_port(driver, port=54320, max_attempts=5):
    """
    Set the HTTP server port in PCAPdroid settings by clicking the label, entering the port, and confirming with OK.
    """
    is_success = False
    try:
        # Scroll to the HTTP server port label
        for i in range(max_attempts):
            handle_crash_dialog(driver, timeout=2.0)
            scroll_cmd = 'new UiScrollable(new UiSelector().scrollable(true)).scrollIntoView(new UiSelector().textContains("HTTP server port"))'
            port_label = driver.find_element(by=AppiumBy.ANDROID_UIAUTOMATOR, value=scroll_cmd)
            port_label.click()
            time.sleep(1)
            set_text_field(driver, value=str(port))
            is_success = True
            break
    except Exception as e:
        print(f"set_http_server_port: error setting port: {e}")
    return is_success


def parse_args():
    p = argparse.ArgumentParser(description="Start PCAPdroid VPN on all connected devices via Appium")
    p.add_argument("--appium-url", type=str, default="http://localhost:4723/wd/hub", help="Appium server URL")
    p.add_argument("--adb", type=str, default="adb", help="ADB command")
    p.add_argument("--app-package",
                   type=str,
                   required=False,
                   help="App package for PCAPdroid on device",
                   default="com.emanuelef.remote_capture")
    p.add_argument("--app-activity",
                   type=str,
                   required=False,
                   help="App activity to launch",
                   default=".activities.MainActivity")
    p.add_argument("--start-locators", type=str, help="JSON array of locators to find Start control.",
                   default=None)
    p.add_argument("--timeout", type=int, default=30, help="Per-device overall timeout seconds")
    p.add_argument("--parallel", action="store_true", help="Run actions in parallel (experimental)")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument('--settings-retries', type=int, default=3, help='Number of attempts to open/configure settings before giving up')
    p.add_argument('--no-settings-abort', action='store_true', help='Do not abort the test for a device if settings cannot be opened/configured (default is to abort)')
    p.add_argument('--http-port', type=int, help='Port to use for HTTP server', default=54320)
    p.add_argument('--socks5-address', type=str, help='The SOCKS5 proxy address to set in PCAPdroid settings')
    return p.parse_args()


def main():
    """Main entrypoint for configuring PCAPdroid on all devices."""

    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=LOG_FORMAT)

    # Start the crash watcher so transient ANR/crash dialogs won't block long-running setup
    crash_watcher_was_started = False
    if crash_watcher and hasattr(crash_watcher, 'start_crash_watcher'):
        try:
            logging.info('Starting crash watcher (background) in run_pcapdroid_on_all')
            crash_watcher.start_crash_watcher(device=None, interval=3.0)
            crash_watcher_was_started = True
        except Exception:
            logging.exception('Failed to start crash watcher')

    try:
        devices = list_connected_devices(adb_cmd=args.adb)
    except Exception as e:
        logging.error("Failed to list adb devices: %s", e)
        sys.exit(1)

    if not devices:
        logging.info("No connected devices found")
        sys.exit(0)

    logging.info("Found devices: %s", devices)

    # build default locators
    if args.start_locators:
        try:
            start_locators = json.loads(args.start_locators)
        except Exception as e:
            logging.error("Invalid --start-locators JSON: %s", e)
            sys.exit(2)
    else:
        # common candidates: text, accessibility id, id
        start_locators = [
            {"strategy": "uiautomator", "value": 'new UiSelector().text("Ready")'},
            {"strategy": "uiautomator", "value": 'new UiSelector().text("Start")'},
            {"strategy": "uiautomator", "value": 'new UiSelector().textContains("VPN")'},
            {"strategy": "xpath", "value": '//*[contains(@text, "Start") or contains(@text, "VPN") or contains(@text, "Capture")]'},
        ]

    results = []
    for serial in devices:
        logging.info("Starting PCAPdroid on %s", serial)
        # enforce settings configuration by default; if args.no_settings_abort is True we will continue on failure
        abort_on_settings_fail = not args.no_settings_abort
        r = start_pcapdroid_on_device_with_settings(args.appium_url,
                                                    serial,
                                                    args.app_package,
                                                    args.app_activity,
                                                    start_locators,
                                                    settings_retries=args.settings_retries,
                                                    abort_on_settings_fail=abort_on_settings_fail,
                                                    http_port=args.http_port,
                                                    socks_ip=args.socks5_address
                                                    )
        results.append(r)

    # Stop crash watcher if we started it
    if crash_watcher_was_started and crash_watcher and hasattr(crash_watcher, 'stop_crash_watcher'):
        try:
            logging.info('Stopping crash watcher (background) in run_pcapdroid_on_all')
            crash_watcher.stop_crash_watcher()
        except Exception:
            logging.exception('Failed to stop crash watcher')

    # print summary
    summary = {"devices": devices, "results": results}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
