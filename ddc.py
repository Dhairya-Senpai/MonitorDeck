"""Windows DDC/CI access via dxva2.dll and user32.dll.

All Windows-specific and ctypes code lives here. The GUI and CLI must talk to
monitors only through this module.

DDC/CI is slow and unreliable, so every call retries and the module serialises
I/O behind a lock with a short settle delay between calls. Physical monitor
handles are always released through DestroyPhysicalMonitors.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from contextlib import contextmanager
from ctypes import byref, windll, wintypes
from dataclasses import dataclass, field
from typing import Iterator, Optional

log = logging.getLogger(__name__)

SETTLE_SECONDS = 0.05
"""Minimum gap between two DDC/CI calls on any monitor."""

MAX_ATTEMPTS = 3
"""How many times a read or write is attempted before it is reported failed."""

MONITORINFOF_PRIMARY = 0x1

user32 = windll.user32
dxva2 = windll.dxva2

_io_lock = threading.RLock()
_last_call = 0.0


class PHYSICAL_MONITOR(ctypes.Structure):
    """dxva2 PHYSICAL_MONITOR."""

    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
    ]


class MONITORINFOEXW(ctypes.Structure):
    """user32 MONITORINFOEXW."""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HMONITOR,
    wintypes.HDC,
    ctypes.POINTER(wintypes.RECT),
    wintypes.LPARAM,
)

# Explicit signatures: handles are 64-bit and ctypes would otherwise pass them
# as 32-bit ints.
user32.EnumDisplayMonitors.argtypes = [
    wintypes.HDC, ctypes.c_void_p, MONITORENUMPROC, wintypes.LPARAM]
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HMONITOR, ctypes.POINTER(wintypes.DWORD)]
dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HMONITOR, wintypes.DWORD, ctypes.c_void_p]
dxva2.DestroyPhysicalMonitors.argtypes = [wintypes.DWORD, ctypes.c_void_p]
dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [
    wintypes.HANDLE, wintypes.BYTE, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
dxva2.SetVCPFeature.argtypes = [wintypes.HANDLE, wintypes.BYTE, wintypes.DWORD]
dxva2.GetCapabilitiesStringLength.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
dxva2.CapabilitiesRequestAndCapabilitiesReply.argtypes = [
    wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD]


class DDCError(Exception):
    """A DDC/CI call failed after all retries."""


@dataclass
class VCPValue:
    """One reply to a VCP read."""

    code: int
    current: int
    maximum: int
    vcp_type: int

    @property
    def is_momentary(self) -> bool:
        """True for momentary controls (MC_MOMENTARY), e.g. degauss.

        Whether a control is continuous (a slider) or discrete (a set of
        values) comes from the capability string, not from this flag.
        """
        return self.vcp_type == 0


def _settle() -> None:
    """Sleep so consecutive DDC/CI calls are at least SETTLE_SECONDS apart."""
    global _last_call
    elapsed = time.monotonic() - _last_call
    if elapsed < SETTLE_SECONDS:
        time.sleep(SETTLE_SECONDS - elapsed)
    _last_call = time.monotonic()


@dataclass
class Monitor:
    """One physical monitor reachable over DDC/CI.

    The handle stays open until close() is called, so the app can keep monitors
    open for its lifetime instead of re-enumerating for every read.
    """

    handle: int
    description: str
    device: str
    is_primary: bool
    _capabilities: Optional[str] = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    def read_vcp(self, code: int) -> Optional[VCPValue]:
        """Read a VCP code. Returns None when the monitor does not answer."""
        self._check_open()
        vcp_type = wintypes.DWORD()
        current = wintypes.DWORD()
        maximum = wintypes.DWORD()
        with _io_lock:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                _settle()
                ok = dxva2.GetVCPFeatureAndVCPFeatureReply(
                    self.handle, code, byref(vcp_type),
                    byref(current), byref(maximum),
                )
                if ok:
                    return VCPValue(code, current.value, maximum.value,
                                    vcp_type.value)
                log.debug("read 0x%02X failed on %s (attempt %d/%d)",
                          code, self.description, attempt, MAX_ATTEMPTS)
                time.sleep(SETTLE_SECONDS * attempt)
        log.info("VCP 0x%02X not readable on %s", code, self.description)
        return None

    def write_vcp(self, code: int, value: int, *, confirmed: bool = False,
                  known: bool = True) -> None:
        """Write a VCP code after checking it against the monitor's own limits.

        Raises DDCError if the code is unreadable, the value is out of range,
        an unknown code was not explicitly confirmed, or the write fails.
        """
        self._check_open()
        if not known and not confirmed:
            raise DDCError(
                f"write to unknown/vendor code 0x{code:02X} needs confirmation")

        reading = self.read_vcp(code)
        if reading is None:
            raise DDCError(f"VCP 0x{code:02X} is not supported on "
                           f"{self.description}; refusing to write")
        if not 0 <= value <= reading.maximum:
            raise DDCError(f"value {value} out of range 0..{reading.maximum} "
                           f"for VCP 0x{code:02X}")

        with _io_lock:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                _settle()
                if dxva2.SetVCPFeature(self.handle, code, value):
                    log.info("set 0x%02X = %d on %s", code, value,
                             self.description)
                    return
                log.debug("write 0x%02X failed on %s (attempt %d/%d)",
                          code, self.description, attempt, MAX_ATTEMPTS)
                time.sleep(SETTLE_SECONDS * attempt)
        raise DDCError(f"failed to set VCP 0x{code:02X} to {value} on "
                       f"{self.description}")

    def read_capabilities(self, *, refresh: bool = False) -> str:
        """Return the raw MCCS capability string, cached after the first read.

        Reading it can take several seconds, which is why it is cached.
        """
        self._check_open()
        if self._capabilities is not None and not refresh:
            return self._capabilities

        length = wintypes.DWORD()
        with _io_lock:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                _settle()
                if (dxva2.GetCapabilitiesStringLength(self.handle, byref(length))
                        and length.value):
                    break
                log.debug("capability length failed on %s (attempt %d/%d)",
                          self.description, attempt, MAX_ATTEMPTS)
                time.sleep(SETTLE_SECONDS * attempt)
            else:
                raise DDCError(
                    f"{self.description} did not report a capability string")

            buffer = ctypes.create_string_buffer(length.value)
            for attempt in range(1, MAX_ATTEMPTS + 1):
                _settle()
                if dxva2.CapabilitiesRequestAndCapabilitiesReply(
                        self.handle, buffer, length.value):
                    break
                log.debug("capability read failed on %s (attempt %d/%d)",
                          self.description, attempt, MAX_ATTEMPTS)
                time.sleep(SETTLE_SECONDS * attempt)
            else:
                raise DDCError(
                    f"could not read capability string from {self.description}")

        self._capabilities = buffer.value.decode("ascii", errors="replace")
        log.info("capability string from %s: %d chars",
                 self.description, len(self._capabilities))
        return self._capabilities

    def supports_ddc(self) -> bool:
        """True if the monitor answers a brightness read (laptop panels do not)."""
        return self.read_vcp(0x10) is not None

    def close(self) -> None:
        """Release the physical monitor handle."""
        if self._closed:
            return
        self._closed = True
        arr = (PHYSICAL_MONITOR * 1)()
        arr[0].hPhysicalMonitor = self.handle
        arr[0].szPhysicalMonitorDescription = self.description[:127]
        dxva2.DestroyPhysicalMonitors(1, byref(arr))

    def _check_open(self) -> None:
        if self._closed:
            raise DDCError(f"monitor handle for {self.description} is closed")


def _enum_hmonitors() -> list[tuple[int, str, bool]]:
    """Return (HMONITOR, device name, is primary) for every attached display."""
    found: list[tuple[int, str, bool]] = []

    def callback(hmonitor: int, _hdc: int, _rect: object, _data: int) -> bool:
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        device, primary = "?", False
        if user32.GetMonitorInfoW(hmonitor, byref(info)):
            device = info.szDevice
            primary = bool(info.dwFlags & MONITORINFOF_PRIMARY)
        found.append((hmonitor, device, primary))
        return True

    if not user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0):
        raise DDCError("EnumDisplayMonitors failed")
    return found


def list_monitors() -> list[Monitor]:
    """Open every physical monitor. The caller must close each one."""
    monitors: list[Monitor] = []
    for hmonitor, device, primary in _enum_hmonitors():
        count = wintypes.DWORD()
        if (not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
                hmonitor, byref(count)) or not count.value):
            log.info("no physical monitors behind %s", device)
            continue
        arr = (PHYSICAL_MONITOR * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmonitor, count.value, arr):
            log.warning("GetPhysicalMonitorsFromHMONITOR failed for %s", device)
            continue
        for entry in arr:
            monitors.append(Monitor(
                handle=entry.hPhysicalMonitor,
                description=entry.szPhysicalMonitorDescription,
                device=device,
                is_primary=primary,
            ))
    return monitors


def close_monitors(monitors: list[Monitor]) -> None:
    """Close every monitor in the list, ignoring ones already closed."""
    for monitor in monitors:
        monitor.close()


@contextmanager
def open_monitors(ddc_only: bool = False) -> Iterator[list[Monitor]]:
    """Context manager yielding open monitors and always releasing the handles.

    With ddc_only=True, monitors that do not answer a brightness read (such as
    the laptop's built-in panel) are closed and left out of the list.
    """
    monitors = list_monitors()
    try:
        if ddc_only:
            usable, unusable = [], []
            for monitor in monitors:
                (usable if monitor.supports_ddc() else unusable).append(monitor)
            close_monitors(unusable)
            monitors = usable
        yield monitors
    finally:
        close_monitors(monitors)
