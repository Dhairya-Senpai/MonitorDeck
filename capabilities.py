"""Parse the MCCS capability string into supported VCP codes.

The capability string looks roughly like::

    (prot(monitor)type(LCD)model(KG271U)cmds(01 02 03 07 0C E3 F3)
     vcp(02 04 10 12 14(01 05 08 0B) 60(0F 11 12) ...)mccs_ver(2.1))

Nothing here touches the monitor except scan_monitor(), which reads current
values through a ddc.Monitor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ddc import Monitor

log = logging.getLogger(__name__)

VCP_NAMES: dict[int, str] = {
    0x02: "New control value",
    0x04: "Restore factory defaults",
    0x05: "Restore factory brightness/contrast",
    0x06: "Restore factory geometry",
    0x08: "Restore factory colour defaults",
    0x0A: "Restore factory TV defaults",
    0x0B: "Colour temperature increment",
    0x0C: "Colour temperature request",
    0x10: "Brightness",
    0x12: "Contrast",
    0x14: "Colour preset",
    0x16: "Red gain",
    0x18: "Green gain",
    0x1A: "Blue gain",
    0x1E: "Auto setup",
    0x52: "Active control",
    0x59: "Red saturation",
    0x5A: "Yellow saturation",
    0x5B: "Green saturation",
    0x5C: "Cyan saturation",
    0x5D: "Blue saturation",
    0x5E: "Magenta saturation",
    0x60: "Input source",
    0x62: "Audio speaker volume",
    0x6C: "Red black level",
    0x6E: "Green black level",
    0x70: "Blue black level",
    0x72: "Gamma",
    0x87: "Sharpness",
    0x8D: "Audio mute",
    0x9B: "Red hue",
    0x9C: "Yellow hue",
    0x9D: "Green hue",
    0x9E: "Cyan hue",
    0x9F: "Blue hue",
    0xA0: "Magenta hue",
    0xAC: "Horizontal frequency",
    0xAE: "Vertical frequency",
    0xB2: "Flat panel sub-pixel layout",
    0xB6: "Display technology type",
    0xC0: "Display usage time",
    0xC6: "Application enable key",
    0xC8: "Display controller type",
    0xC9: "Display firmware level",
    0xCA: "OSD / button lock",
    0xCC: "OSD language",
    0xD6: "Power mode",
    0xDC: "Display application (picture mode)",
    0xDF: "VCP version",
    0xE0: "Manufacturer specific (0xE0)",
    0xF0: "Manufacturer specific (0xF0)",
}

READ_ONLY_CODES: frozenset[int] = frozenset({
    0x02, 0x52, 0xAC, 0xAE, 0xB2, 0xB6, 0xC0, 0xC8, 0xC9, 0xDF,
})
"""Codes the MCCS spec defines as read-only (or report-only)."""

VALUE_NAMES: dict[int, dict[int, str]] = {
    0x14: {
        0x01: "sRGB", 0x02: "Display native", 0x03: "4000 K", 0x04: "5000 K",
        0x05: "6500 K", 0x06: "7500 K", 0x07: "8200 K", 0x08: "9300 K",
        0x09: "10000 K", 0x0A: "11500 K", 0x0B: "User 1", 0x0C: "User 2",
        0x0D: "User 3",
    },
    0x60: {
        0x01: "VGA-1", 0x02: "VGA-2", 0x03: "DVI-1", 0x04: "DVI-2",
        0x05: "Composite-1", 0x06: "Composite-2", 0x07: "S-Video-1",
        0x08: "S-Video-2", 0x09: "Tuner-1", 0x0A: "Tuner-2", 0x0B: "Tuner-3",
        0x0C: "Component-1", 0x0D: "Component-2", 0x0E: "Component-3",
        0x0F: "DisplayPort-1", 0x10: "DisplayPort-2", 0x11: "HDMI-1",
        0x12: "HDMI-2",
    },
    0xD6: {
        0x01: "On", 0x02: "Standby", 0x03: "Suspend", 0x04: "Off (soft)",
        0x05: "Off (hard)",
    },
    0xCA: {0x01: "Unlocked", 0x02: "Locked"},
    0xDC: {
        0x00: "Standard/Custom", 0x01: "Productivity", 0x02: "Mixed",
        0x03: "Movie", 0x04: "User defined", 0x05: "Games", 0x06: "Sports",
        0x08: "Text", 0x0B: "Photo",
    },
    0xB6: {
        0x01: "CRT", 0x02: "CRT", 0x03: "Plasma", 0x04: "LCD", 0x05: "LCD",
        0x06: "EL", 0x07: "OLED",
    },
}
"""Human names for discrete values of well-known codes."""

STANDARD_CONTROLS: tuple[int, ...] = (
    0x10, 0x12, 0x14, 0x16, 0x18, 0x1A, 0x87, 0x60, 0xD6,
)
"""Codes MonitorDeck offers controls for, if the monitor supports them."""


def code_name(code: int) -> str:
    """Return a readable name for a VCP code."""
    if code in VCP_NAMES:
        return VCP_NAMES[code]
    if 0xE0 <= code <= 0xFF:
        return f"Manufacturer specific (0x{code:02X})"
    return f"Unknown (0x{code:02X})"


def value_name(code: int, value: int) -> Optional[str]:
    """Return a readable name for one discrete value, if known."""
    return VALUE_NAMES.get(code, {}).get(value)


def is_known_code(code: int) -> bool:
    """True for codes defined by the MCCS standard (not vendor-specific)."""
    return code in VCP_NAMES and not 0xE0 <= code <= 0xFF


@dataclass
class VCPFeature:
    """One VCP code published in the capability string."""

    code: int
    allowed: Optional[list[int]] = None

    @property
    def name(self) -> str:
        """Readable name of the code."""
        return code_name(self.code)

    @property
    def is_continuous(self) -> bool:
        """True when the code is a range (slider) rather than a value list."""
        return self.allowed is None

    @property
    def read_only(self) -> bool:
        """True when the MCCS spec marks the code as read-only."""
        return self.code in READ_ONLY_CODES


@dataclass
class Capabilities:
    """Parsed capability string."""

    raw: str
    model: Optional[str] = None
    monitor_type: Optional[str] = None
    protocol: Optional[str] = None
    mccs_version: Optional[str] = None
    commands: list[int] = field(default_factory=list)
    features: dict[int, VCPFeature] = field(default_factory=dict)
    extras: dict[str, str] = field(default_factory=dict)

    def supports(self, code: int) -> bool:
        """True when the monitor publishes this VCP code."""
        return code in self.features

    def standard_controls(self) -> list[VCPFeature]:
        """The STANDARD_CONTROLS codes this monitor actually publishes."""
        return [self.features[c] for c in STANDARD_CONTROLS if c in self.features]

    def vendor_codes(self) -> list[VCPFeature]:
        """Published codes in the manufacturer-specific 0xE0-0xFF range."""
        return [f for code, f in sorted(self.features.items())
                if 0xE0 <= code <= 0xFF]


def _split_segments(body: str) -> list[tuple[str, str]]:
    """Split "key(value)key2(value2)" into pairs, respecting nested parens."""
    segments: list[tuple[str, str]] = []
    key_start = 0
    index = 0
    while index < len(body):
        char = body[index]
        if char == "(":
            key = body[key_start:index].strip()
            depth = 1
            value_start = index + 1
            index += 1
            while index < len(body) and depth:
                if body[index] == "(":
                    depth += 1
                elif body[index] == ")":
                    depth -= 1
                index += 1
            segments.append((key, body[value_start:index - 1]))
            key_start = index
        else:
            index += 1
    return segments


def _parse_hex_list(text: str) -> list[int]:
    """Parse "01 02 0A" into [1, 2, 10], skipping unparsable tokens."""
    values: list[int] = []
    for token in text.split():
        try:
            values.append(int(token, 16))
        except ValueError:
            log.debug("skipping unparsable capability token %r", token)
    return values


def _parse_vcp_section(text: str) -> dict[int, VCPFeature]:
    """Parse the vcp(...) section into features keyed by code."""
    features: dict[int, VCPFeature] = {}
    token = ""
    index = 0
    while index < len(text):
        char = text[index]
        if char == "(":
            depth = 1
            start = index + 1
            index += 1
            while index < len(text) and depth:
                if text[index] == "(":
                    depth += 1
                elif text[index] == ")":
                    depth -= 1
                index += 1
            code = _single_code(token)
            if code is not None:
                features[code] = VCPFeature(code, _parse_hex_list(text[start:index - 1]))
            token = ""
        elif char.isspace():
            code = _single_code(token)
            if code is not None:
                features.setdefault(code, VCPFeature(code))
            token = ""
            index += 1
        else:
            token += char
            index += 1
    code = _single_code(token)
    if code is not None:
        features.setdefault(code, VCPFeature(code))
    return features


def _single_code(token: str) -> Optional[int]:
    """Parse one hex code token, returning None when it is not a code."""
    token = token.strip()
    if not token:
        return None
    try:
        return int(token, 16)
    except ValueError:
        log.debug("skipping unparsable vcp code %r", token)
        return None


def parse_capabilities(raw: str) -> Capabilities:
    """Parse a raw MCCS capability string."""
    caps = Capabilities(raw=raw)
    body = raw.strip()
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]

    for key, value in _split_segments(body):
        key_lower = key.lower()
        if key_lower == "vcp":
            caps.features = _parse_vcp_section(value)
        elif key_lower == "cmds":
            caps.commands = _parse_hex_list(value)
        elif key_lower == "model":
            caps.model = value.strip()
        elif key_lower == "type":
            caps.monitor_type = value.strip()
        elif key_lower == "prot":
            caps.protocol = value.strip()
        elif key_lower in ("mccs_ver", "mccs_version"):
            caps.mccs_version = value.strip()
        else:
            caps.extras[key] = value.strip()
    return caps


@dataclass
class ScanRow:
    """One row of the capability table: a published code plus its live value."""

    code: int
    name: str
    current: Optional[int]
    maximum: Optional[int]
    read_only: bool
    continuous: bool
    allowed: Optional[list[int]]
    error: Optional[str] = None

    @property
    def access(self) -> str:
        """"R" for read-only codes, "R/W" otherwise."""
        return "R" if self.read_only else "R/W"

    @property
    def current_label(self) -> str:
        """Current value, with the discrete value's name when one is known."""
        if self.current is None:
            return self.error or "no reply"
        named = value_name(self.code, self.current)
        return f"{self.current} ({named})" if named else str(self.current)


def scan_monitor(monitor: Monitor, caps: Capabilities,
                 codes: Optional[Iterable[int]] = None) -> list[ScanRow]:
    """Read the current value of every published code. Read-only, never writes."""
    rows: list[ScanRow] = []
    for code in sorted(codes if codes is not None else caps.features):
        feature = caps.features.get(code, VCPFeature(code))
        reading = monitor.read_vcp(code)
        rows.append(ScanRow(
            code=code,
            name=feature.name,
            current=reading.current if reading else None,
            maximum=reading.maximum if reading else None,
            read_only=feature.read_only,
            continuous=feature.is_continuous,
            allowed=feature.allowed,
            error=None if reading else "no reply",
        ))
    return rows


def format_scan_table(rows: list[ScanRow]) -> str:
    """Render scan rows as a plain-text table."""
    header = f"{'Code':<6}{'Name':<34}{'Current':<18}{'Max':<7}{'Access':<8}Values"
    lines = [header, "-" * len(header)]
    for row in rows:
        allowed = ""
        if row.allowed:
            parts = []
            for value in row.allowed:
                named = value_name(row.code, value)
                parts.append(f"{value:02X}={named}" if named else f"{value:02X}")
            allowed = " ".join(parts)
        maximum = "-" if row.maximum is None else str(row.maximum)
        lines.append(f"0x{row.code:02X}  {row.name:<34}{row.current_label:<18}"
                     f"{maximum:<7}{row.access:<8}{allowed}")
    return "\n".join(lines)
