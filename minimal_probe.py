"""Step 1: list monitors, read brightness, set it briefly, restore it.

Minimal standalone script (print is fine here, per the project workflow).
Run:  python minimal_probe.py          # read + brief write test
      python minimal_probe.py --read   # read only, never writes
"""

import ctypes
import sys
import time
from ctypes import byref, windll, wintypes

VCP_BRIGHTNESS = 0x10
SETTLE = 0.05          # ~50 ms between DDC/CI calls
RETRIES = 3

user32 = windll.user32
dxva2 = windll.dxva2


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


# Declare argument types: handles are 64-bit and would be truncated otherwise.
dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HMONITOR, ctypes.POINTER(wintypes.DWORD)]
dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HMONITOR, wintypes.DWORD, ctypes.c_void_p]
dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [
    wintypes.HANDLE, wintypes.BYTE, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
dxva2.SetVCPFeature.argtypes = [wintypes.HANDLE, wintypes.BYTE, wintypes.DWORD]
dxva2.DestroyPhysicalMonitors.argtypes = [wintypes.DWORD, ctypes.c_void_p]
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
    ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
)


def list_hmonitors():
    """Return [(HMONITOR, device name, is_primary)] for every display."""
    found = []

    def callback(hmonitor, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        name = "?"
        primary = False
        if user32.GetMonitorInfoW(hmonitor, byref(info)):
            name = info.szDevice
            primary = bool(info.dwFlags & 1)  # MONITORINFOF_PRIMARY
        found.append((hmonitor, name, primary))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
    return found


def physical_monitors(hmonitor):
    """Return the PHYSICAL_MONITOR array behind one HMONITOR (caller destroys)."""
    count = wintypes.DWORD()
    if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmonitor, byref(count)):
        return None
    arr = (PHYSICAL_MONITOR * count.value)()
    if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmonitor, count.value, arr):
        return None
    return arr


def get_vcp(handle, code):
    """Read a VCP code. Returns (current, maximum) or None if unsupported."""
    vcp_type = wintypes.DWORD()
    current = wintypes.DWORD()
    maximum = wintypes.DWORD()
    for attempt in range(RETRIES):
        ok = dxva2.GetVCPFeatureAndVCPFeatureReply(
            handle, wintypes.BYTE(code), byref(vcp_type),
            byref(current), byref(maximum),
        )
        if ok:
            return current.value, maximum.value
        time.sleep(SETTLE * (attempt + 1))
    return None


def set_vcp(handle, code, value):
    """Write a VCP code. Returns True on success."""
    for attempt in range(RETRIES):
        if dxva2.SetVCPFeature(handle, wintypes.BYTE(code), wintypes.DWORD(value)):
            return True
        time.sleep(SETTLE * (attempt + 1))
    return False


def test_brightness(handle, label, allow_write):
    """Read brightness; optionally nudge it and restore the original value."""
    reading = get_vcp(handle, VCP_BRIGHTNESS)
    time.sleep(SETTLE)
    if reading is None:
        print(f"    brightness (0x10): no DDC/CI reply -> unsupported, skipping")
        return
    current, maximum = reading
    print(f"    brightness (0x10): current={current} max={maximum}")

    if not allow_write:
        return

    target = current - 10 if current - 10 >= 0 else min(current + 10, maximum)
    print(f"    write test: {current} -> {target}")
    if not set_vcp(handle, VCP_BRIGHTNESS, target):
        print("    write FAILED")
        return
    time.sleep(SETTLE)

    back = get_vcp(handle, VCP_BRIGHTNESS)
    time.sleep(SETTLE)
    print(f"    read back:  {back[0] if back else 'no reply'}")

    if set_vcp(handle, VCP_BRIGHTNESS, current):
        print(f"    restored:   {current}")
    else:
        print(f"    RESTORE FAILED - monitor may still be at {target}")


def main():
    allow_write = "--read" not in sys.argv
    monitors = list_hmonitors()
    print(f"EnumDisplayMonitors found {len(monitors)} display(s)\n")

    for hmonitor, device, primary in monitors:
        tag = " (primary)" if primary else ""
        print(f"{device}{tag}")
        arr = physical_monitors(hmonitor)
        if arr is None:
            print("    no physical monitors reported\n")
            continue
        try:
            for pm in arr:
                print(f'  "{pm.szPhysicalMonitorDescription}"')
                test_brightness(pm.hPhysicalMonitor, device, allow_write)
        finally:
            dxva2.DestroyPhysicalMonitors(len(arr), byref(arr))
        print()


if __name__ == "__main__":
    main()
