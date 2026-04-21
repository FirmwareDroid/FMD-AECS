#!/usr/bin/env python3
"""
Collect device identifiers and basic info from a connected Android device via adb.

Produces a JSON file under the project's `out/` directory containing fields such as:
- serial
- imeis (list)
- android_id
- geo_location (lat, lon, provider, timestamp if available)
- unique_identifiers (serials, fingerprint, model, manufacturer, mac)

The script is best-effort and will populate fields that can be read without elevated
permissions. Fields that cannot be determined will be set to null.

Usage:
    python3 collect_device_info.py [--serial SERIAL] [--outdir OUTDIR]

Default OUTDIR is `<project-root>/out`.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import datetime
import sys
import hashlib
import base64
import time
import urllib.parse

# Project base dir (same logic used elsewhere in this repo)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
for _ in range(4):
    BASE_DIR = os.path.dirname(BASE_DIR)
OUT_DIR_DEFAULT = os.path.join(BASE_DIR, 'out')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def run_adb(cmd_args, serial=None, timeout=15):
    cmd = ['adb']
    if serial:
        cmd.extend(['-s', serial])
    cmd.extend(cmd_args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or '').strip()
        err = (p.stderr or '').strip()
        if p.returncode != 0:
            logger.debug('adb cmd failed: %s ; stderr=%s', ' '.join(cmd), err)
        return p.returncode, out, err
    except Exception as e:
        logger.exception('Failed to run adb command: %s', e)
        return -1, '', str(e)


def get_first_connected_device():
    rc, out, err = run_adb(['devices'])
    if rc != 0:
        return None
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith('List of devices'):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == 'device':
            return parts[0]
    return None


def get_prop(prop, serial=None):
    rc, out, err = run_adb(['shell', 'getprop', prop], serial=serial)
    if rc == 0 and out:
        return out.strip()
    return None


def try_get_imei_via_service(serial=None):
    """Try to obtain IMEI using `service call iphonesubinfo 1` and parse result.

    This is best-effort. Different Android versions return different formats.
    We attempt to extract ASCII digits from the returned Parcel hex/strings.
    """
    rc, out, err = run_adb(['shell', 'service', 'call', 'iphonesubinfo', '1'], serial=serial)
    if rc != 0 or not out:
        return None
    # Look for quoted ASCII in output
    # Example output may contain hex tokens like 0x0031 0x0032 corresponding to UTF-16 chars
    hex_tokens = re.findall(r'0x[0-9a-fA-F]{4}', out)
    if hex_tokens:
        # Convert 16-bit hex tokens to characters (assume UTF-16 BE/LE uncertain). Try both.
        try:
            bytes_le = b''.join(int(h, 16).to_bytes(2, 'little') for h in hex_tokens)
            s_le = bytes_le.decode('utf-16le', errors='ignore')
            digits = re.sub(r'\D', '', s_le)
            if digits:
                return digits
        except Exception:
            pass
        try:
            bytes_be = b''.join(int(h, 16).to_bytes(2, 'big') for h in hex_tokens)
            s_be = bytes_be.decode('utf-16be', errors='ignore')
            digits = re.sub(r'\D', '', s_be)
            if digits:
                return digits
        except Exception:
            pass
    # Fallback: look for any contiguous digits in output
    m = re.search(r'(\d{6,20})', out)
    if m:
        return m.group(1)
    return None


def get_imeis(serial=None):
    """Return a list of IMEIs found (may be empty)."""
    imeis = []
    # 1) Try service call
    imei = try_get_imei_via_service(serial)
    if imei:
        imeis.append(imei)
    # 2) Try common getprop locations
    for prop in ('persist.radio.imei', 'gsm.device_id', 'ril.gsm.imei', 'persist.radio.imei0', 'persist.radio.imei1'):
        val = get_prop(prop, serial)
        if val and re.search(r'\d{6,15}', val):
            cleaned = re.sub(r'\D', '', val)
            if cleaned and cleaned not in imeis:
                imeis.append(cleaned)
    # 3) Try dumpsys telephony.registry or telephony and extract deviceId
    rc, out, err = run_adb(['shell', 'dumpsys', 'telephony.registry'], serial=serial)
    if rc == 0 and out:
        digits = re.findall(r'\b(\d{6,20})\b', out)
        for d in digits:
            if d not in imeis:
                imeis.append(d)
    # Deduplicate
    return imeis


def get_android_id(serial=None):
    rc, out, err = run_adb(['shell', 'settings', 'get', 'secure', 'android_id'], serial=serial)
    if rc == 0 and out:
        return out.strip()
    # Fallback: check getprop or dumpsys
    val = get_prop('ro.boot.serialno', serial)
    if val:
        return val
    return None


def get_geo_location(serial=None):
    # Inspect dumpsys location for last known location entries
    rc, out, err = run_adb(['shell', 'dumpsys', 'location'], serial=serial)
    if rc != 0 or not out:
        return None
    text = out
    # Try to find patterns like "Location[provider gps ... lat=12.34 lon=56.78 ...]"
    m = re.search(r'lat=\s*([\-0-9\.]+)\D+lon=\s*([\-0-9\.]+)', text)
    if not m:
        # Alternative: look for "last location" style
        m = re.search(r'Last Known Location.*?\{[^}]*lat=([\-0-9\.]+)[^}]*lon=([\-0-9\.]+)', text, re.S)
    if m:
        try:
            lat = float(m.group(1))
            lon = float(m.group(2))
            return {'lat': lat, 'lon': lon}
        except Exception:
            return None
    return None


def get_unique_identifiers(serial=None):
    ids = {}
    ids['adb_serial_env'] = os.environ.get('ANDROID_SERIAL') or os.environ.get('ADB_SERIAL')
    ids['device_serial_prop'] = get_prop('ro.serialno', serial)
    ids['boot_serial'] = get_prop('ro.boot.serialno', serial)
    ids['build_fingerprint'] = get_prop('ro.build.fingerprint', serial)
    ids['model'] = get_prop('ro.product.model', serial)
    ids['manufacturer'] = get_prop('ro.product.manufacturer', serial)
    # wifi mac (best-effort)
    rc, out, err = run_adb(['shell', 'cat', '/sys/class/net/wlan0/address'], serial=serial)
    if rc == 0 and out:
        ids['wifi_mac'] = out.strip()
    else:
        ids['wifi_mac'] = None
    # bluetooth mac (best-effort)
    rc, out, err = run_adb(['shell', 'cat', '/sys/class/bluetooth/hci0/address'], serial=serial)
    if rc == 0 and out:
        ids['bluetooth_mac'] = out.strip()
    else:
        ids['bluetooth_mac'] = None
    return ids


def get_imsi(serial=None):
    # Try to extract IMSI from dumpsys telephony or telephony.registry
    rc, out, err = run_adb(['shell', 'dumpsys', 'telephony.registry'], serial=serial)
    if rc == 0 and out:
        # Look for subscriber id or IMSI-like digits
        m = re.search(r'(?:subscriberId|mSubscriberId|SubscriberId|IMSI)[:=\s]+([0-9]{6,20})', out, re.IGNORECASE)
        if m:
            return m.group(1)
    # Fallback: try service call similar to IMEI approach
    rc, out, err = run_adb(['shell', 'service', 'call', 'iphonesubinfo', '7'], serial=serial)
    if rc == 0 and out:
        m = re.search(r'(\d{6,20})', out)
        if m:
            return m.group(1)
    return None


def get_gaid_oaid(serial=None):
    # GAID/OAID are typically only accessible via app-level APIs or in protected storage.
    # We attempt a few non-privileged heuristics but most emulators/devices will not expose them.
    gaid = None
    oaid = None
    # Try settings (best-effort)
    for key in ('advertising_id', 'gaid', 'advertisingId'):
        rc, out, err = run_adb(['shell', 'settings', 'get', 'secure', key], serial=serial)
        if rc == 0 and out and len(out.strip()) >= 8:
            gaid = out.strip()
            break
    for key in ('oaid', 'open_ad_id'):
        rc, out, err = run_adb(['shell', 'settings', 'get', 'secure', key], serial=serial)
        if rc == 0 and out and len(out.strip()) >= 8:
            oaid = out.strip()
            break
    return gaid, oaid


def get_wifi_info(serial=None):
    # Obtain SSID and BSSID using dumpsys wifi (best-effort)
    rc, out, err = run_adb(['shell', 'dumpsys', 'wifi'], serial=serial)
    ssid = None
    bssid = None
    if rc == 0 and out:
        # Common patterns: SSID: "MyWifi" or ssid="MyWifi"
        m = re.search(r'SSID:\s*\"([^\"]+)\"', out)
        if not m:
            m = re.search(r'ssid:\s*([^\s,\n]+)', out, re.IGNORECASE)
        if m:
            ssid = m.group(1).strip('"')
        m2 = re.search(r'BSSID:\s*([0-9a-fA-F:]{11,17})', out)
        if not m2:
            m2 = re.search(r'bssid=([0-9a-fA-F:]{11,17})', out, re.IGNORECASE)
        if m2:
            bssid = m2.group(1)
    # As fallback, try dumpsys netstats or iwconfig (less likely)
    return ssid, bssid




def get_sensors(serial=None):
    # Query sensorservice to list available sensors
    rc, out, err = run_adb(['shell', 'dumpsys', 'sensorservice'], serial=serial)
    sensors = []
    if rc == 0 and out:
        # Heuristic: find lines that look like sensor entries (contain 'name=')
        for line in out.splitlines():
            line = line.strip()
            if 'name=' in line.lower():
                # try to capture name="..." or name=...
                m = re.search(r'name=\"?([^\",\]]+)\"?', line, re.IGNORECASE)
                if m:
                    sensors.append(m.group(1).strip())
    # Deduplicate
    sensors = list(dict.fromkeys(sensors))
    return sensors


def collect_all(serial=None):
    if not serial:
        serial = get_first_connected_device()
    if not serial:
        logger.error('No adb device found; connect a device or supply --serial')
        sys.exit(2)

    logger.info('Collecting device info for serial: %s', serial)
    result = {}
    result['collected_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat() + 'Z'
    result['serial'] = serial
    try:
        result['imeis'] = get_imeis(serial)
    except Exception:
        logger.exception('Failed to collect IMEIs')
        result['imeis'] = []
    try:
        result['android_id'] = get_android_id(serial)
    except Exception:
        logger.exception('Failed to collect Android ID')
        result['android_id'] = None
    try:
        result['geo_location'] = get_geo_location(serial)
    except Exception:
        logger.exception('Failed to collect geo location')
        result['geo_location'] = None
    try:
        result['unique_identifiers'] = get_unique_identifiers(serial)
    except Exception:
        logger.exception('Failed to collect unique identifiers')
        result['unique_identifiers'] = {}

    try:
        result['imsi'] = get_imsi(serial)
    except Exception:
        logger.exception('Failed to collect IMSI')
        result['imsi'] = None

    try:
        gaid, oaid = get_gaid_oaid(serial)
        result['gaid'] = gaid
        result['oaid'] = oaid
    except Exception:
        logger.exception('Failed to collect GAID/OAID')
        result['gaid'] = None
        result['oaid'] = None

    # If certain advertising identifiers are null, set them to default 'OCTOPUS'
    if not result.get('gaid'):
        result['gaid'] = 'OCTOPUS'
    if not result.get('oaid'):
        result['oaid'] = 'OCTOPUS'
    if not result.get('android_id'):
        result['android_id'] = 'OCTOPUS'

    try:
        ssid, bssid = get_wifi_info(serial)
        result['wifi_ssid'] = ssid
        result['wifi_bssid'] = bssid
    except Exception:
        logger.exception('Failed to collect WiFi SSID/BSSID')
        result['wifi_ssid'] = None
        result['wifi_bssid'] = None

    # installed_apps collection removed intentionally to avoid PII leakage

    try:
        result['sensors'] = get_sensors(serial)
    except Exception:
        logger.exception('Failed to collect sensors list')
        result['sensors'] = []

    return result


def compute_encodings(value):
    """Return a dict with base64 and various hashes for the string representation of value."""
    if value is None:
        return None
    # Convert value to a stable string representation
    if isinstance(value, (dict, list)):
        try:
            s = json.dumps(value, sort_keys=True, ensure_ascii=False)
        except Exception:
            s = str(value)
    else:
        s = str(value)
    b = s.encode('utf-8', errors='ignore')
    enc = {}
    try:
        enc['base64'] = base64.b64encode(b).decode('ascii')
    except Exception:
        enc['base64'] = None
    try:
        # URL-encode the UTF-8 representation
        enc['url'] = urllib.parse.quote(s, safe='')
        # Double URL-encode (useful for nested encoding contexts)
        enc['url_double'] = urllib.parse.quote(enc['url'], safe='')
    except Exception:
        enc['url'] = enc['url_double'] = None
    try:
        # hex representation of the raw bytes
        enc['hex'] = b.hex()
    except Exception:
        enc['hex'] = None
    try:
        enc['md5'] = hashlib.md5(b).hexdigest()
        enc['sha1'] = hashlib.sha1(b).hexdigest()
        enc['sha256'] = hashlib.sha256(b).hexdigest()
        enc['sha512'] = hashlib.sha512(b).hexdigest()
    except Exception:
        enc['md5'] = enc['sha1'] = enc['sha256'] = enc['sha512'] = None
    return enc


def transform_for_output(obj):
    """Recursively transform an object so that every primitive value is accompanied
    by its encodings. Returns a new object suitable for JSON serialization.
    - Strings and numbers -> { 'raw': <value>, 'encodings': { ... } }
    - None stays None
    - Lists -> list of transformed items
    - Dicts -> dict of transformed values
    """
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        enc = compute_encodings(obj)
        return {'raw': obj, 'encodings': enc}
    if isinstance(obj, list):
        return [transform_for_output(i) for i in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out[k] = transform_for_output(v)
        return out
    # Fallback for other types
    try:
        s = str(obj)
        enc = compute_encodings(s)
        return {'raw': s, 'encodings': enc}
    except Exception:
        return None


def apply_overrides(serial, overrides: dict, set_defaults: bool = False):
    """Apply overrides to the device via adb. Supported keys (best-effort):
    - android_id -> settings put secure android_id
    - gaid / advertising_id -> settings put secure advertising_id (best-effort)
    - oaid -> settings put secure oaid (best-effort)

    If set_defaults is True, generate default values starting with 'OCTOPUS_<key>_<ts>'.
    Returns a dict of results per key -> (success: bool, rc, out, err)
    """
    results = {}
    supported = ('android_id', 'gaid', 'advertising_id', 'oaid')
    # generate defaults
    defaults = {}
    if set_defaults:
        ts = int(time.time())
        for k in supported:
            defaults[k] = f'OCTOPUS_{k.upper()}_{ts}'

    for key in supported:
        val = None
        if key in overrides:
            val = overrides[key]
        elif set_defaults and key in defaults:
            val = defaults[key]
        if val is None:
            continue

        logger.info('Applying override for %s -> %s (serial=%s)', key, val, serial)
        if key == 'android_id':
            rc, out, err = run_adb(['shell', 'settings', 'put', 'secure', 'android_id', val], serial=serial)
            success = (rc == 0)
            results[key] = {'success': success, 'rc': rc, 'out': out, 'err': err}
        elif key in ('gaid', 'advertising_id'):
            # try secure then global as a best-effort
            rc1, out1, err1 = run_adb(['shell', 'settings', 'put', 'secure', 'advertising_id', val], serial=serial)
            rc2, out2, err2 = run_adb(['shell', 'settings', 'put', 'global', 'advertising_id', val], serial=serial)
            success = (rc1 == 0) or (rc2 == 0)
            results[key] = {'success': success, 'attempts': [(rc1, out1, err1), (rc2, out2, err2)]}
        elif key == 'oaid':
            rc, out, err = run_adb(['shell', 'settings', 'put', 'secure', 'oaid', val], serial=serial)
            success = (rc == 0)
            results[key] = {'success': success, 'rc': rc, 'out': out, 'err': err}
        else:
            results[key] = {'success': False, 'error': 'unsupported'}

    # Log results
    for k, v in results.items():
        logger.info('Override result for %s: %s', k, v)
    return results


def ensure_emulator_services(serial, lat=47.3769, lon=8.5417):
    """Ensure GPS, Bluetooth and NFC are enabled on the device (best-effort) and set GPS to Zurich."""
    results = {}
    # Enable location mode (3 = high accuracy)
    try:
        rc, out, err = run_adb(['shell', 'settings', 'put', 'secure', 'location_mode', '3'], serial=serial)
        results['location_mode'] = {'rc': rc, 'out': out, 'err': err}
    except Exception as e:
        results['location_mode'] = {'error': str(e)}

    # Try to enable location service via svc (may not exist on all images)
    try:
        rc, out, err = run_adb(['shell', 'svc', 'location', 'enable'], serial=serial)
        results['svc_location'] = {'rc': rc, 'out': out, 'err': err}
    except Exception:
        results['svc_location'] = {'error': 'svc location enable failed'}

    # Enable Bluetooth
    try:
        rc, out, err = run_adb(['shell', 'svc', 'bluetooth', 'enable'], serial=serial)
        results['svc_bluetooth'] = {'rc': rc, 'out': out, 'err': err}
    except Exception:
        # fallback to settings put
        rc, out, err = run_adb(['shell', 'settings', 'put', 'global', 'bluetooth_on', '1'], serial=serial)
        results['settings_bluetooth'] = {'rc': rc, 'out': out, 'err': err}

    # Enable NFC
    try:
        rc, out, err = run_adb(['shell', 'svc', 'nfc', 'enable'], serial=serial)
        results['svc_nfc'] = {'rc': rc, 'out': out, 'err': err}
    except Exception:
        rc, out, err = run_adb(['shell', 'settings', 'put', 'global', 'nfc_on', '1'], serial=serial)
        results['settings_nfc'] = {'rc': rc, 'out': out, 'err': err}

    # Set emulator GPS location to Zurich (longitude, latitude)
    try:
        # adb emu commands require connecting to an emulator; use 'emu' command which works with emulator serial
        rc, out, err = run_adb(['emu', 'geo', 'fix', str(lon), str(lat)], serial=serial)
        results['geo_fix'] = {'rc': rc, 'out': out, 'err': err}
    except Exception:
        results['geo_fix'] = {'error': 'geo fix failed'}

    # Small pause to let services settle
    try:
        time.sleep(0.8)
    except Exception:
        pass

    return results


def adb_root(serial=None):
    """Attempt to restart adbd as root on the target device/emulator (best-effort)."""
    if not serial:
        return {'success': False, 'error': 'no_serial'}
    try:
        rc, out, err = run_adb(['root'], serial=serial)
        res = {'rc': rc, 'out': out, 'err': err}
        if rc == 0:
            logger.info('adb root succeeded on %s', serial)
        else:
            logger.warning('adb root returned non-zero on %s: %s', serial, err or out)
        return res
    except Exception as e:
        logger.exception('adb root attempt failed: %s', e)
        return {'success': False, 'error': str(e)}


def main():
    parser = argparse.ArgumentParser(description='Collect device info from connected adb device and write JSON to out/')
    parser.add_argument('--serial', help='ADB device serial (default: first connected device)')
    parser.add_argument('--outdir', default=OUT_DIR_DEFAULT, help='Directory to write device info JSON (default: project out/)')
    parser.add_argument('--override', '-o', action='append', default=[], help='Override a collected value: key=value. Can be passed multiple times.')
    parser.add_argument('--set-defaults', action='store_true', help='Set default OCTOPUS* values for supported keys on the device before collection')
    args = parser.parse_args()

    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)

    # Ensure adb runs as root on the selected device (best-effort)
    target_serial = args.serial or get_first_connected_device()
    if target_serial:
        try:
            adb_root(target_serial)
        except Exception:
            logger.exception('adb_root failed (continuing)')
    else:
        logger.debug('No serial specified; adb root skipped (no device)')

    # Apply overrides/set-defaults (best-effort) before collecting info
    # Parse overrides into dict
    overrides = {}
    for item in args.override:
        if '=' in item:
            k, v = item.split('=', 1)
            overrides[k.strip()] = v.strip()
        else:
            logger.warning('Ignoring malformed override (expected key=value): %s', item)

    if args.set_defaults or overrides:
        # pick device serial for adb operations
        target_serial = args.serial or get_first_connected_device()
        if not target_serial:
            logger.error('No adb device found for applying overrides')
        else:
            try:
                apply_overrides(target_serial, overrides, set_defaults=args.set_defaults)
            except Exception:
                logger.exception('apply_overrides failed (continuing to collect)')
            # Ensure emulator services and set GPS to Zurich (best-effort)
            try:
                ensure_emulator_services(target_serial)
            except Exception:
                logger.exception('Failed to ensure emulator services (continuing)')

    info = collect_all(serial=args.serial)

    filename = f"device_info_{info.get('serial', 'unknown')}.json"
    path = os.path.join(outdir, filename)
    # Prepare output containing both raw and encoded representations
    out_obj = {
        'raw': info,
        'encoded': transform_for_output(info),
    }
    try:
        with open(path + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(out_obj, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(path + '.tmp', path)
        logger.info('Wrote device info to %s', path)
    except Exception:
        logger.exception('Failed to write device info JSON')
        sys.exit(3)


if __name__ == '__main__':
    main()






