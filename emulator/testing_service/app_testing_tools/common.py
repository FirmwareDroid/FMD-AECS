import subprocess


def get_first_connected_device():
    """Return the first adb device serial in 'device' state or None if none found."""
    try:
        res = subprocess.run(['adb', 'devices'], capture_output=True, text=True, timeout=5)
        lines = [l.strip() for l in (res.stdout or '').splitlines()]
        for line in lines[1:]:
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == 'device':
                return parts[0]
    except Exception:
        return None
    return None


def get_adb_cmd(serial=None):
    """Return an adb command list. If serial is provided, include '-s <serial>'.

    If serial is None and multiple devices are connected, this will pick the
    first device reported by `adb devices` in state 'device' and return
    ['adb', '-s', <serial>]. If no devices are present, returns ['adb'].
    """
    if serial:
        return ['adb', '-s', serial]
    first = get_first_connected_device()
    if first:
        return ['adb', '-s', first]
    return ['adb']

