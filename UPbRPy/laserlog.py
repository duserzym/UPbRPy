"""Read Teledyne Chromium experiment logs without interpreting their clocks.

Bytes are decoded as strict UTF-8 first, falling back to ISO-8859-1. Raw
dictionary values retain their spelling and insertion order. Times are naive
local datetimes; scan and firing indices retain the indices in the file.
Only the standard library is used.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import csv
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from pathlib import Path
import re
from typing import TypeVar


__all__ = [
    "LaserSettings", "LaserFiring", "LaserScan", "LaserLogWarning", "LaserLog",
    "UnsupportedLaserLogFormat", "read_lase", "parse_lase", "detect_format",
]

_SCAN_KEY = re.compile(r"Scan([0-9]+)\Z")
_FIRING_KEY = re.compile(r"Spot([0-9]+)\.Pass([0-9]+)\.(.+)\Z")
_SPOT_NUMBER = re.compile(r"([0-9]+)\s*\Z")
_SPOT_SIZE = re.compile(
    r"\s*([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
    r"\s*(?:µm|um)(?:\s+(.+?))?\s*\Z"
)
# csv.reader(strict=True) alone permits quotes inside unquoted fields.
_CSV_ROW = re.compile(r'(?:[^,"\r\n]*|"(?:[^"]|"")*")(?:,(?:[^,"\r\n]*|"(?:[^"]|"")*"))*\Z')
_T = TypeVar("_T")


class UnsupportedLaserLogFormat(ValueError):
    """The content does not have the Chromium Scans/Header signature."""


@dataclass(frozen=True)
class LaserSettings:
    """Recorded settings and optional typed values; raw values are unchanged."""

    raw: dict[str, str] = field(default_factory=dict)
    output_pct: float | None = None
    fluence_j_cm2: float | None = None
    rep_rate_hz: float | None = None
    shot_count: int | None = None
    spot_size_um: float | None = None
    spot_shape: str | None = None
    scan_speed_um_s: float | None = None
    pass_enabled: bool | None = None
    pass_count: int | None = None
    dwell_time_s: float | None = None

    def key(self) -> tuple[
        float | None, float | None, int | None, float | None,
        float | None, str | None, float | None,
    ]:
        """Group by fluence, rate, shots, output, size, shape and scan speed."""
        return (
            self.fluence_j_cm2, self.rep_rate_hz, self.shot_count,
            self.output_pct, self.spot_size_um, self.spot_shape,
            self.scan_speed_um_s,
        )


@dataclass(frozen=True)
class LaserFiring:
    """One recorded SpotK.PassP group, including incomplete groups."""

    scan_index: int
    firing_index: int
    pass_index: int
    start: datetime | None
    laser_on: datetime | None
    laser_off: datetime | None
    end: datetime | None
    last_output_reading: float | None
    start_position_um: tuple[float, float, float] | None
    raw: dict[str, str]

    @property
    def complete(self) -> bool:
        """Whether both laser-on and laser-off times are present."""
        return self.laser_on is not None and self.laser_off is not None

    @property
    def laser_duration_s(self) -> float | None:
        """Recorded laser-off minus laser-on, or None when incomplete."""
        if self.laser_on is None or self.laser_off is None:
            return None
        return (self.laser_off - self.laser_on).total_seconds()


@dataclass(frozen=True)
class LaserScan:
    """One placed scan; declared counts do not create missing records."""

    index: int
    scan_type: str
    description: str
    spot_number: int | None
    selected: bool | None
    locked: bool | None
    vertices_um: tuple[tuple[float, float, float], ...]
    declared_vertex_count: int | None
    preablation: LaserSettings
    ablation: LaserSettings
    declared_spot_count: int | None
    expected_lase_time_s: float | None
    scan_length_um: float | None
    firings: tuple[LaserFiring, ...]
    data: dict[str, str]
    line_number: int

    @property
    def fired(self) -> bool:
        """Whether any firing has a laser-on time."""
        return any(firing.laser_on is not None for firing in self.firings)

    @property
    def fired_count(self) -> int:
        """Number of recorded firings with a laser-on time."""
        return sum(firing.laser_on is not None for firing in self.firings)


@dataclass(frozen=True)
class LaserLogWarning:
    """A recoverable content problem at a 1-based line, or at file level."""

    line_number: int | None
    message: str


@dataclass
class LaserLog:
    """Chromium scans and metadata, with recoverable parsing warnings."""

    path: Path | None
    system_info: dict[str, str]
    experiment_settings: dict[str, str]
    gas_control: dict[str, dict[str, str]]
    events: dict[str, dict[str, str]]
    other_sections: dict[str, dict[str, str]]
    scan_header: tuple[str, ...]
    scans: tuple[LaserScan, ...]
    warnings: tuple[LaserLogWarning, ...]
    truncated: bool
    sha256: str = field(default="", compare=False)

    @property
    def fired_scans(self) -> tuple[LaserScan, ...]:
        """Scans with a laser-on record, in scan-index order."""
        return tuple(scan for scan in self.scans if scan.fired)

    @property
    def unfired_scans(self) -> tuple[LaserScan, ...]:
        """Scans without a laser-on record, in scan-index order."""
        return tuple(scan for scan in self.scans if not scan.fired)

    @property
    def firings(self) -> tuple[LaserFiring, ...]:
        """All firings by laser-on time, missing times last; ties are stable."""
        return tuple(sorted(
            (firing for scan in self.scans for firing in scan.firings),
            key=lambda firing: (firing.laser_on is None, firing.laser_on or datetime.max),
        ))

    @property
    def first_laser_on(self) -> datetime | None:
        """Earliest recorded laser-on time, or None for an unfired log."""
        return min((f.laser_on for s in self.scans for f in s.firings
                    if f.laser_on is not None), default=None)

    @property
    def last_laser_on(self) -> datetime | None:
        """Latest recorded laser-on time, or None for an unfired log."""
        return max((f.laser_on for s in self.scans for f in s.firings
                    if f.laser_on is not None), default=None)

    def distinct_ablation_settings(self) -> list[tuple[LaserSettings, int]]:
        """Count all scans by settings key, in first-seen scan-index order."""
        groups: dict[tuple[float | int | str | None, ...], tuple[LaserSettings, int]] = {}
        for scan in self.scans:
            key = scan.ablation.key()
            settings, count = groups.get(key, (scan.ablation, 0))
            groups[key] = settings, count + 1
        return list(groups.values())

    def scans_by_spot_number(self) -> dict[int, list[LaserScan]]:
        """Group scans with a trailing description number; omit other scans."""
        groups: dict[int, list[LaserScan]] = {}
        for scan in self.scans:
            if scan.spot_number is not None:
                groups.setdefault(scan.spot_number, []).append(scan)
        return groups


def _decode(data: bytes | str) -> str:
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.removeprefix(b"\xef\xbb\xbf").decode("iso-8859-1")


def _split_lines(text: str) -> list[str]:
    """Split only on CRLF, CR and LF, preserving other characters."""
    lines = re.split(r"\r\n|\r|\n", text)
    if lines[-1] == "":
        lines.pop()
    return lines


def _signature(lines: Iterable[str]) -> None:
    """Check only the first two nonblank lines, allowing a leading BOM."""
    meaningful = (clean for line in lines if (clean := line.strip().lstrip("\ufeff").lstrip()))
    if (next(meaningful, "") != "[Scans]"
            or not next(meaningful, "").startswith("Header=Scan Type,")):
        raise UnsupportedLaserLogFormat("Expected [Scans] followed by Header=Scan Type,...")


def detect_format(path: str | Path) -> str:
    """Return 'lase' from the first headers, or raise UnsupportedLaserLogFormat.

    Only the first two nonblank lines are read. Detection uses content rather
    than the extension; file-system errors propagate to the caller.
    """
    with Path(path).open("rb") as handle:
        _signature(line for chunk in handle for line in _split_lines(_decode(chunk)))
    return "lase"


def _float(text: str) -> float:
    value = float(text)
    if not isfinite(value):
        raise ValueError("expected a finite number")
    return value


def _bool(text: str) -> bool:
    if text.strip() not in {"0", "1"}:
        raise ValueError("expected 0 or 1")
    return text.strip() == "1"


def _position(text: str) -> tuple[float, float, float]:
    parts = text.split(",")
    if len(parts) != 3:
        raise ValueError("expected x,y,z")
    return _float(parts[0]), _float(parts[1]), _float(parts[2])


def _time(text: str) -> datetime:
    return datetime.strptime(text, "%Y/%m/%d %H:%M:%S.%f")


def _value(
    raw: dict[str, str], key: str, convert: Callable[[str], _T],
    line_number: int, warnings: list[LaserLogWarning],
) -> _T | None:
    if key not in raw:
        return None
    try:
        return convert(raw[key])
    except (ValueError, OverflowError):
        warnings.append(LaserLogWarning(line_number, f"Unparseable {key}: {raw[key]!r}"))
        return None


def _key_values(text: str, line_number: int, warnings: list[LaserLogWarning]) -> dict[str, str]:
    raw: dict[str, str] = {}
    for item in text.split(";"):
        if not item.strip():
            continue
        key, separator, value = item.partition("=")
        if not separator:
            warnings.append(LaserLogWarning(line_number, f"Expected key=value: {item!r}"))
            continue
        raw[key.strip()] = value
    return raw


def _spot_size(text: str) -> tuple[float, str | None]:
    match = _SPOT_SIZE.fullmatch(text)
    if match is None:
        raise ValueError("expected a size in µm or um and optional shape")
    return _float(match[1]), match[2]


def _settings(text: str, line_number: int, warnings: list[LaserLogWarning]) -> LaserSettings:
    raw = _key_values(text, line_number, warnings)
    size = _value(raw, "Laser.SpotSize", _spot_size, line_number, warnings)
    return LaserSettings(
        raw=raw,
        output_pct=_value(raw, "Laser.Output", _float, line_number, warnings),
        fluence_j_cm2=_value(raw, "Laser.Fluence", _float, line_number, warnings),
        rep_rate_hz=_value(raw, "Laser.RepRate", _float, line_number, warnings),
        shot_count=_value(raw, "ShotCount", int, line_number, warnings),
        spot_size_um=size[0] if size is not None else None,
        spot_shape=size[1] if size is not None else None,
        scan_speed_um_s=_value(raw, "ScanSpeed", _float, line_number, warnings),
        pass_enabled=_value(raw, "PassEnabled", _bool, line_number, warnings),
        pass_count=_value(raw, "PassCount", int, line_number, warnings),
        dwell_time_s=_value(raw, "DwellTime", _float, line_number, warnings),
    )


def _firings(
    data: dict[str, str], scan_index: int, line_number: int, warnings: list[LaserLogWarning],
) -> tuple[LaserFiring, ...]:
    groups: dict[tuple[int, int], dict[str, str]] = {}
    for key, value in data.items():
        match = _FIRING_KEY.fullmatch(key)
        if match is None:
            continue
        try:
            indices = int(match[1]), int(match[2])
        except ValueError:
            warnings.append(LaserLogWarning(line_number, f"Unparseable firing indices: {key}"))
            continue
        groups.setdefault(indices, {})[match[3]] = value
    firings = []
    for (firing_index, pass_index), raw in sorted(groups.items()):
        # Use full data keys for warnings so the firing can be located.
        prefix = f"Spot{firing_index}.Pass{pass_index}."
        full = {prefix + key: value for key, value in raw.items()}
        firings.append(LaserFiring(
            scan_index=scan_index, firing_index=firing_index, pass_index=pass_index,
            start=_value(full, prefix + "Time.Start", _time, line_number, warnings),
            laser_on=_value(full, prefix + "Time.LaserOn", _time, line_number, warnings),
            laser_off=_value(full, prefix + "Time.LaserOff", _time, line_number, warnings),
            end=_value(full, prefix + "Time.End", _time, line_number, warnings),
            last_output_reading=_value(
                full, prefix + "Laser.LastOutputReading", _float, line_number, warnings,
            ),
            start_position_um=_value(full, prefix + "Position.Start", _position, line_number, warnings),
            raw=raw,
        ))
    return tuple(firings)


class _MalformedScan(ValueError):
    def __init__(self, message: str, *, partial: bool = False) -> None:
        super().__init__(message)
        self.partial = partial


def _scan(
    text: str, index: int, line_number: int, warnings: list[LaserLogWarning],
) -> LaserScan:
    try:
        values = next(csv.reader([text], strict=True))
    except csv.Error as error:
        raise _MalformedScan(
            f"Malformed Scan{index}: {error}", partial="unexpected end" in str(error),
        ) from None
    if not _CSV_ROW.fullmatch(text):
        raise _MalformedScan(f"Malformed quoting in Scan{index}")
    if len(values) != 9:
        raise _MalformedScan(
            f"Scan{index}: expected 9 fields, got {len(values)}", partial=len(values) < 9,
        )
    scan_type, description, selected, locked, count, vertices, pre, ablation, data_text = values
    try:
        positions = tuple(_position(vertex) for vertex in vertices.split(";")) if vertices else ()
    except ValueError:
        raise _MalformedScan(f"Scan{index}: unparseable Vertex List: {vertices!r}") from None
    declared = _value({"Vertex Count": count}, "Vertex Count", int, line_number, warnings)
    if declared is not None and declared != len(positions):
        warnings.append(LaserLogWarning(
            line_number, f"Scan{index}: Vertex Count {declared} differs from {len(positions)} vertices",
        ))
    match = _SPOT_NUMBER.search(description)
    number = (
        _value({"description number": match[1]}, "description number", int, line_number, warnings)
        if match else None
    )
    data = _key_values(data_text, line_number, warnings)
    return LaserScan(
        index=index, scan_type=scan_type, description=description, spot_number=number,
        selected=_value({"Selected": selected}, "Selected", _bool, line_number, warnings),
        locked=_value({"Lock Edit": locked}, "Lock Edit", _bool, line_number, warnings),
        vertices_um=positions, declared_vertex_count=declared,
        preablation=_settings(pre, line_number, warnings),
        ablation=_settings(ablation, line_number, warnings),
        declared_spot_count=_value(data, "SpotCount", int, line_number, warnings),
        expected_lase_time_s=_value(data, "ExpectedLaseTime", _float, line_number, warnings),
        scan_length_um=_value(data, "ScanLength", _float, line_number, warnings),
        firings=_firings(data, index, line_number, warnings), data=data, line_number=line_number,
    )


def parse_lase(data: bytes | str, *, path: str | Path | None = None) -> LaserLog:
    """Parse in-memory Chromium content, preserving complete scans on errors.

    The same signature as detect_format is required. Subsequent content
    problems become warnings. Missing SystemInfo or a partial final scan row
    sets truncated. A final newline is optional; CRLF, CR and LF are accepted.
    """
    text = _decode(data)
    # Keep leading blank lines for accurate source line numbers.
    lines = _split_lines(text)
    _signature(lines)
    warnings: list[LaserLogWarning] = []
    sections: dict[str, dict[str, str]] = {}
    scans: dict[int, LaserScan] = {}
    header: tuple[str, ...] = ()
    section = ""
    partial_final_scan = False
    last_content_line = next((i for i in range(len(lines), 0, -1) if lines[i - 1].strip()), 0)
    for line_number, line in enumerate(lines, 1):
        stripped = line.strip().lstrip("\ufeff").lstrip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            sections.setdefault(section, {})
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator:
            warnings.append(LaserLogWarning(line_number, f"Expected key=value or section: {stripped!r}"))
            if section == "Scans" and _SCAN_KEY.fullmatch(key) and line_number == last_content_line:
                partial_final_scan = True
            continue
        if section == "Scans" and key == "Header":
            try:
                header = tuple(next(csv.reader([value], strict=True)))
            except csv.Error as error:
                warnings.append(LaserLogWarning(line_number, f"Malformed Header: {error}"))
            continue
        match = _SCAN_KEY.fullmatch(key) if section == "Scans" else None
        if match is None:
            sections.setdefault(section, {})[key] = value
            continue
        try:
            index = int(match[1])
        except ValueError:
            warnings.append(LaserLogWarning(line_number, f"Unparseable scan index: {key}"))
            continue
        if index in scans:
            warnings.append(LaserLogWarning(line_number, f"Duplicate Scan{index}; kept first"))
        try:
            scan = _scan(value, index, line_number, warnings)
            if index not in scans:
                scans[index] = scan
        except _MalformedScan as error:
            warnings.append(LaserLogWarning(line_number, str(error)))
            partial_final_scan = error.partial and line_number == last_content_line
    missing_system_info = "SystemInfo" not in sections
    if missing_system_info:
        warnings.append(LaserLogWarning(None, "Missing [SystemInfo]; file may be truncated"))
    return LaserLog(
        path=Path(path) if path is not None else None,
        system_info=sections.pop("SystemInfo", {}),
        experiment_settings=sections.pop("Laser.ExperimentSettings", {}),
        gas_control={name.removeprefix("GasControl:"): sections.pop(name)
                     for name in list(sections) if name.startswith("GasControl:")},
        events={name.removeprefix("Events:"): sections.pop(name)
                for name in list(sections) if name.startswith("Events:")},
        other_sections={name: values for name, values in sections.items() if name != "Scans" or values},
        scan_header=header, scans=tuple(scans[index] for index in sorted(scans)),
        warnings=tuple(warnings), truncated=missing_system_info or partial_final_scan,
        sha256=hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest(),
    )


def read_lase(path: str | Path) -> LaserLog:
    """Detect and read a Chromium file; non-Chromium content is rejected."""
    path = Path(path)
    detect_format(path)
    return parse_lase(path.read_bytes(), path=path)
