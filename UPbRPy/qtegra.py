"""Read Qtegra exports into one acquisition model without changing their clocks.

Rich CSVs are the acquisition record; per-spot CSVs supply dwell and detector
calibration metadata. UPbR CSVs contain only Y and need reconstructed times.
Instrument state dictionaries use setting/readback names as keys; sample-list
fields use the original field names (for example, ``Duration [s]`` and
``Evaluate``). Empty instrument-state values are omitted, while sample-list
strings, including empty strings, are retained.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re

import numpy as np
import pandas as pd


__all__ = [
    "CAPABILITIES", "COUNTER_SATURATED", "DEFAULT_CYCLE_TIME_S",
    "Channel", "Acquisition", "SourceFile", "QtegraSession",
    "read_rich_csv", "read_spot_folder", "read_spot_file", "read_upbr_csv",
    "merge", "recover_xcal", "detect_format", "open_qtegra",
]

CAPABILITIES = frozenset({
    "timestamps", "stop_times", "per_channel_time", "analog_counter", "mass",
    "dwell_xcal", "instrument_state", "sequence_fields", "user",
})
COUNTER_SATURATED = -33.0
# Median sweep spacing in the Boise Zircon_04Nov26 session, in seconds.
DEFAULT_CYCLE_TIME_S = 0.4255

_SPOT_CAPABILITIES = frozenset({"timestamps", "instrument_state", "dwell_xcal"})
_SPOT_FIRST_LINE = re.compile(
    r"^(?P<label>.+):(?P<time>\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2} [AP]M);$"
)


@dataclass(frozen=True)
class Channel:
    """One channel in instrument order, with metadata when exported."""

    name: str
    mass: float | None = None
    dwell_s: float | None = None
    xcal: float | None = None


@dataclass
class Acquisition:
    """One acquisition; sweep arrays retain missing values as NaN."""

    label: str
    sequence_index: int
    channels: tuple[Channel, ...]
    y: np.ndarray
    time: np.ndarray | None
    analog: np.ndarray | None = None
    counter: np.ndarray | None = None
    start_text: str | None = None
    start: datetime | None = None
    stop_text: str | None = None
    stop: datetime | None = None
    tune_name: str | None = None
    tune_settings: dict[str, float | str] = field(default_factory=dict)
    readbacks: dict[str, float | str] = field(default_factory=dict)
    sequence_fields: dict[str, str] = field(default_factory=dict)
    user: str | None = None
    spot_number: int | None = None
    source_path: Path | None = None

    @property
    def channel_names(self) -> tuple[str, ...]:
        """Channel names in instrument order."""
        return tuple(channel.name for channel in self.channels)

    @property
    def n_sweeps(self) -> int:
        """Number of sweeps, including interior missing sweeps."""
        return self.y.shape[0]

    @property
    def sweep_time(self) -> np.ndarray | None:
        """Shared sweep time, or the first channel's time when per-channel."""
        if self.time_per_channel:
            return self.time[:, 0]
        return self.time

    @property
    def time_per_channel(self) -> bool:
        """Whether time has a separate column for each channel."""
        return self.time is not None and self.time.ndim == 2

    def channel_index(self, name) -> int:
        """Return a channel's column index; raise ValueError if absent."""
        return self.channel_names.index(name)


@dataclass(frozen=True)
class SourceFile:
    """The file and digest of the bytes used by a reader."""

    path: Path
    sha256: str
    format: str


@dataclass
class QtegraSession:
    """Acquisitions in sequence order, or chronological order for spot files."""

    acquisitions: list[Acquisition]
    format: str
    capabilities: frozenset[str]
    sources: tuple[SourceFile, ...]
    software: str | None = None
    machine: str | None = None
    pulse_counting_threshold: float | None = None
    clock_offset_hours: float | None = None
    clock_offset_seconds_median: float | None = None
    times_reconstructed: bool = False
    nominal_cycle_time_s: float | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def channel_names(self) -> tuple[str, ...]:
        """Channel names of the first acquisition, or an empty tuple."""
        return self.acquisitions[0].channel_names if self.acquisitions else ()

    @property
    def labels(self) -> list[str]:
        """Acquisition labels in session order."""
        return [acquisition.label for acquisition in self.acquisitions]

    def acquisition(self, label) -> Acquisition:
        """Find an acquisition by label; raise KeyError if absent."""
        for acquisition in self.acquisitions:
            if acquisition.label == label:
                return acquisition
        raise KeyError(label)

    def has(self, capability) -> bool:
        """Whether this session contains an exported capability."""
        return capability in self.capabilities


def _spot_number(label):
    suffix = label.rsplit("_", 1)[-1]
    return int(suffix) if "_" in label and suffix.isdecimal() else None


def _state_value(text):
    try:
        return float(text)
    except ValueError:
        return text


def _iso_datetime(text):
    if not text:
        return None
    # Qtegra writes seven fractional digits. Truncate explicitly, not round.
    truncated = re.sub(r"(\.\d{6})\d+", r"\1", text)
    return datetime.fromisoformat(truncated)


def _sweep_count(y):
    present = ~np.isnan(y).all(axis=-1)
    return int(np.flatnonzero(present)[-1] + 1) if present.any() else 0


def _read_single(path, quantities):
    path = Path(path)
    raw = path.read_bytes()
    frame = pd.read_csv(
        BytesIO(raw), header=None, dtype=str, keep_default_na=False,
        encoding="utf-8-sig",
    )
    if frame.shape[0] < 3 or frame.shape[1] < 5 or frame.iat[0, 4] != "Sample 1":
        raise ValueError(f"Not a Qtegra single CSV: {path}")
    columns = np.flatnonzero(frame.iloc[0].to_numpy() != "")
    columns = columns[columns >= 4]
    labels = frame.iloc[1, columns].tolist()
    main = frame.loc[frame[0] == "MainRuns"]
    if main.empty or not (main[3] == "Y").any():
        raise ValueError(f"No MainRuns Y data in {path}")
    names = tuple(pd.unique(main[2]))
    sweeps = pd.to_numeric(main[1]).to_numpy(dtype=int)
    if (sweeps < 0).any():
        raise ValueError(f"Negative sweep index in {path}")
    channel_indices = pd.Categorical(main[2], categories=names).codes
    cubes = {}
    for quantity in quantities:
        mask = (main[3] == quantity).to_numpy()
        if not mask.any():
            continue
        texts = main.loc[mask, columns].to_numpy()
        values = pd.to_numeric(texts.ravel(), errors="coerce").reshape(texts.shape)
        cube = np.full((len(labels), int(sweeps.max()) + 1, len(names)), np.nan)
        cube[:, sweeps[mask], channel_indices[mask]] = values.T
        cubes[quantity] = cube
    return path, raw, frame, columns, labels, names, cubes


def read_rich_csv(path) -> QtegraSession:
    """Read a Rich CSV, preserving original timestamp strings and detector data.

    Design for large files: read the bytes once, parse the table once with
    pandas, and convert/scatter each whole quantity block with pandas/numpy.
    Python loops assemble acquisitions and their short metadata dictionaries;
    no Python loop visits individual sweep cells.
    """
    path, raw, frame, columns, labels, names, cubes = _read_single(
        path, ("Y", "Time", "Analog", "Counter", "X [u]"),
    )
    for quantity in ("Time", "Analog", "Counter", "X [u]"):
        if quantity not in cubes:
            raise ValueError(f"Missing Rich quantity {quantity!r} in {path}")
    masses = cubes["X [u]"]
    valid_mass = np.isfinite(masses)
    mass_values = np.take_along_axis(
        masses, valid_mass.argmax(axis=1)[:, None, :], axis=1,
    )[:, 0, :]
    acquisitions = []
    for i, label in enumerate(labels):
        count = _sweep_count(cubes["Y"][i])
        channels = tuple(
            Channel(name, mass=float(mass) if np.isfinite(mass) else None)
            for name, mass in zip(names, mass_values[i])
        )
        acquisitions.append(Acquisition(
            label, i + 1, channels, cubes["Y"][i, :count].copy(),
            cubes["Time"][i, :count].copy(),
            analog=cubes["Analog"][i, :count].copy(),
            counter=cubes["Counter"][i, :count].copy(),
            spot_number=_spot_number(label), source_path=path,
        ))
    session = QtegraSession(
        acquisitions, "rich", CAPABILITIES - {"dwell_xcal"},
        (SourceFile(path, sha256(raw).hexdigest(), "rich"),),
    )
    metadata = frame.loc[(frame.index >= 2) & (frame[0] != "MainRuns")]
    for row in metadata.itertuples(index=False, name=None):
        group, key, subkey = row[:3]
        for acquisition, column in zip(acquisitions, columns):
            text = row[column]
            if group == "TuneSettings":
                acquisition.tune_name = key
                if text:
                    acquisition.tune_settings[subkey] = _state_value(text)
            elif group == "StatusReport" and text:
                acquisition.readbacks[subkey] = _state_value(text)
            elif group == "Info" and key == "UserName":
                acquisition.user = text or None
            elif group in {"StartTime", "StopTime"}:
                prefix = "start" if group == "StartTime" else "stop"
                setattr(acquisition, f"{prefix}_text", text or None)
                setattr(acquisition, prefix, _iso_datetime(text))
            elif group in {"BrigidMS", "PMIAnalyteLaser"}:
                acquisition.sequence_fields[subkey or key] = text
                if key == "Laser Scan Position Number" and text:
                    number = int(text)
                    if acquisition.spot_number is not None and number != acquisition.spot_number:
                        session.warnings.append(
                            f"{acquisition.label}: Laser Scan Position Number {number} "
                            f"differs from label spot number {acquisition.spot_number}."
                        )
                    acquisition.spot_number = number
    return session


def _key_values(text):
    return dict(item.split("=", 1) for item in text.split(";") if "=" in item)


def _read_spot(path):
    path = Path(path)
    raw = path.read_bytes()
    lines = raw.decode("utf-8-sig").splitlines()
    match = _SPOT_FIRST_LINE.fullmatch(lines[0]) if lines else None
    if match is None or len(lines) < 15 or not lines[13].startswith("Time,"):
        raise ValueError(f"Not a Qtegra per-spot CSV: {path}")
    label, start_text = match.group("label", "time")
    names = tuple(lines[13].split(",")[1:-1])
    channel_metadata = lines[14].split(",")[1:-1]
    channels = []
    for name, text in zip(names, channel_metadata):
        values = _key_values(text)
        channels.append(Channel(
            name, dwell_s=float(values["dwell time"]) if values.get("dwell time") else None,
            xcal=float(values["xcal factor"]) if values.get("xcal factor") else None,
        ))
    # Match the existing per-spot parser: its metadata row forces string
    # columns, followed by pandas.to_numeric for the actual sweeps.
    table = pd.read_csv(BytesIO(raw), skiprows=13, encoding="utf-8-sig")
    table = table.iloc[1:, :len(names) + 1].apply(pd.to_numeric, errors="coerce")
    y = table.iloc[:, 1:].to_numpy(dtype=float)
    count = _sweep_count(y)
    acquisition = Acquisition(
        label, 0, tuple(channels), y[:count].copy(),
        table.iloc[:count, 0].to_numpy(dtype=float),
        start_text=start_text, start=datetime.strptime(start_text, "%m/%d/%Y %I:%M:%S %p"),
        tune_name=lines[3].split(":", 1)[0],
        sequence_fields={"spot_file_start_text": lines[0]},
        spot_number=_spot_number(label), source_path=path,
    )
    software_fields = _key_values(lines[1].split(":", 1)[1])
    software = " ".join(software_fields[key] for key in ("Name", "Version") if software_fields.get(key))
    machine = _key_values(lines[2].split(":", 1)[1]).get("Machine")
    acquisition.tune_settings = {
        key: _state_value(value)
        for key, value in _key_values(lines[3].split(":", 1)[1]).items() if value
    }
    for line in lines[4:11]:
        acquisition.readbacks.update({
            key: _state_value(value)
            for key, value in _key_values(line.split(":", 1)[1]).items() if value
        })
    threshold = _key_values(lines[11].split(":", 1)[1]).get("Threshold")
    return (
        acquisition, software or None, machine, float(threshold) if threshold else None,
        SourceFile(path, sha256(raw).hexdigest(), "spot"),
    )


def read_spot_file(path) -> Acquisition:
    """Read one per-spot CSV; its sequence_index is zero until folder ordering."""
    return _read_spot(path)[0]


def _csv_formats(path):
    found = {}
    for child in sorted(Path(path).iterdir()):
        if child.is_file() and child.suffix.lower() == ".csv":
            try:
                found[child] = detect_format(child)
            except ValueError:
                pass
    return found


def _spot_session(paths):
    records = [_read_spot(path) for path in paths]
    if not records:
        raise ValueError("No Qtegra per-spot CSV files found.")
    records.sort(key=lambda record: (record[0].start, record[0].source_path.name))
    session = QtegraSession(
        [record[0] for record in records], "spot_folder", _SPOT_CAPABILITIES,
        tuple(record[4] for record in records),
        software=records[0][1], machine=records[0][2], pulse_counting_threshold=records[0][3],
    )
    for index, acquisition in enumerate(session.acquisitions, 1):
        acquisition.sequence_index = index
    return session


def read_spot_folder(path) -> QtegraSession:
    """Read per-spot CSVs ordered by start time, breaking ties by file name.

    Non-CSV files and CSVs with other layouts are ignored.
    """
    return _spot_session([file for file, kind in _csv_formats(path).items() if kind == "spot"])


def read_upbr_csv(path, *, cycle_time_s: float | None = None,
                  acquisition_gap_s: float = 0.0) -> QtegraSession:
    """Read Y-only UPbR data with reconstructed session-relative times.

    The default cycle is 0.4255 s, the Boise session's median sweep spacing.
    Each acquisition starts after the previous one's sweep count times cycle
    plus gap; its time array includes that offset. Calendar starts stay None.
    """
    cycle = DEFAULT_CYCLE_TIME_S if cycle_time_s is None else float(cycle_time_s)
    gap = float(acquisition_gap_s)
    if not np.isfinite(cycle) or cycle <= 0:
        raise ValueError("cycle_time_s must be finite and positive.")
    if not np.isfinite(gap) or gap < 0:
        raise ValueError("acquisition_gap_s must be finite and nonnegative.")
    path, raw, _frame, _columns, labels, names, cubes = _read_single(path, ("Y",))
    acquisitions = []
    offset = 0.0
    for i, label in enumerate(labels):
        count = _sweep_count(cubes["Y"][i])
        acquisitions.append(Acquisition(
            label, i + 1, tuple(Channel(name) for name in names),
            cubes["Y"][i, :count].copy(), offset + np.arange(count) * cycle,
            spot_number=_spot_number(label), source_path=path,
        ))
        offset += count * cycle + gap
    return QtegraSession(
        acquisitions, "upbr", frozenset(),
        (SourceFile(path, sha256(raw).hexdigest(), "upbr"),),
        times_reconstructed=True, nominal_cycle_time_s=cycle,
        warnings=[f"UPbR sweep times reconstructed with cycle_time_s={cycle} and acquisition_gap_s={gap}."],
    )


def _check_pair(rich, spot, rtol):
    label = rich.label
    if rich.channel_names != spot.channel_names:
        raise ValueError(f"{label}: channel names differ: {rich.channel_names} vs {spot.channel_names}")
    if rich.y.shape != spot.y.shape:
        raise ValueError(f"{label}, channel {rich.channel_names[0]}: Y sweep counts differ")
    equal = np.isclose(rich.y, spot.y, rtol=rtol, atol=0, equal_nan=True)
    if not equal.all():
        channel = rich.channel_names[np.argwhere(~equal)[0, 1]]
        raise ValueError(f"{label}, channel {channel}: Y values or NaN masks differ")
    if (rich.sweep_time is None or spot.sweep_time is None
            or rich.sweep_time.shape != spot.sweep_time.shape
            or not np.allclose(rich.sweep_time, spot.sweep_time, rtol=rtol, atol=0, equal_nan=True)):
        raise ValueError(f"{label}, channel {rich.channel_names[0]}: sweep times differ")


def merge(rich: QtegraSession, folder: QtegraSession, *, rtol=1e-12) -> QtegraSession:
    """Return a Rich session enriched by matching per-spot labels, without mutation.

    Validate Y, missing-value masks and first-channel sweep times before
    merging. The observed clock offset is recorded, and neither clock shifts.
    ``spot_file_start_text`` retains the complete line 1 without its terminator.
    """
    if rich.format != "rich" or folder.format != "spot_folder":
        raise ValueError("merge requires a rich session and a spot_folder session.")
    if len(set(rich.labels)) != len(rich.labels) or len(set(folder.labels)) != len(folder.labels):
        raise ValueError("Cannot merge duplicate acquisition labels.")
    companions = {acquisition.label: acquisition for acquisition in folder.acquisitions}
    missing = [label for label in rich.labels if label not in companions]
    if missing:
        raise ValueError(f"Missing per-spot labels: {', '.join(missing)}")
    for acquisition in rich.acquisitions:
        _check_pair(acquisition, companions[acquisition.label], rtol)
    result = deepcopy(rich)
    result.format = "rich+spot_folder"
    result.capabilities = rich.capabilities | folder.capabilities
    result.sources += folder.sources
    result.software = folder.software
    result.machine = folder.machine
    result.pulse_counting_threshold = folder.pulse_counting_threshold
    result.warnings.extend(folder.warnings)
    rich_labels = set(rich.labels)
    extras = [label for label in folder.labels if label not in rich_labels]
    if extras:
        result.warnings.append(f"Extra per-spot labels ignored: {', '.join(extras)}")
    offsets = []
    for acquisition in result.acquisitions:
        spot = companions[acquisition.label]
        acquisition.channels = tuple(
            Channel(channel.name, channel.mass, companion.dwell_s, companion.xcal)
            for channel, companion in zip(acquisition.channels, spot.channels)
        )
        filled = []
        for name in ("tune_settings", "readbacks"):
            if not getattr(acquisition, name) and getattr(spot, name):
                setattr(acquisition, name, deepcopy(getattr(spot, name)))
                filled.append(name)
        if filled:
            if not acquisition.tune_name:
                acquisition.tune_name = spot.tune_name
            result.warnings.append(f"{acquisition.label}: filled {', '.join(filled)} from per-spot file.")
        acquisition.sequence_fields["spot_file_start_text"] = spot.sequence_fields.get(
            "spot_file_start_text", spot.start_text,
        )
        if spot.start is not None and acquisition.start is not None:
            offsets.append((spot.start - acquisition.start).total_seconds())
    if offsets:
        result.clock_offset_seconds_median = float(np.median(offsets))
        result.clock_offset_hours = round(result.clock_offset_seconds_median / 900) / 4
    return result


def recover_xcal(session_or_acquisition) -> dict[str, float]:
    """Recover each channel's median Y/Analog factor on saturated sweeps."""
    acquisitions = (
        session_or_acquisition.acquisitions
        if isinstance(session_or_acquisition, QtegraSession) else [session_or_acquisition]
    )
    ratios = {}
    for acquisition in acquisitions:
        if acquisition.counter is None or acquisition.analog is None:
            continue
        for index, name in enumerate(acquisition.channel_names):
            analog = acquisition.analog[:, index]
            y = acquisition.y[:, index]
            mask = ((acquisition.counter[:, index] == COUNTER_SATURATED)
                    & np.isfinite(analog) & (analog != 0) & np.isfinite(y))
            if mask.any():
                ratios.setdefault(name, []).append(y[mask] / analog[mask])
    return {name: float(np.median(np.concatenate(values))) for name, values in ratios.items()}


def _has_start_time_footer(path):
    # StartTime precedes StopTime and the twelve sample-list rows. Read just
    # that footer, including arbitrarily wide acquisition columns, rather
    # than scanning a Y-only table to distinguish it from a Rich export.
    with path.open("rb") as handle:
        position = handle.seek(0, 2)
        tail = b""
        while position and tail.count(b"\n") < 16:
            size = min(position, 4096)
            position -= size
            handle.seek(position)
            tail = handle.read(size) + tail
    return any(line.startswith(b"StartTime,,,,") for line in tail.splitlines()[-16:])


def detect_format(path) -> str:
    """Identify Qtegra layouts from headers, the first block and a short footer.

    Directories containing only per-spot exports return ``spot_folder``;
    other directories return ``folder`` for discovery by :func:`open_qtegra`.
    File detection does not load the acquisition table.
    """
    path = Path(path)
    if path.is_dir():
        formats = set(_csv_formats(path).values())
        return "spot_folder" if formats == {"spot"} else "folder"
    with path.open(encoding="utf-8-sig") as handle:
        first = handle.readline().rstrip("\r\n")
        if _SPOT_FIRST_LINE.fullmatch(first):
            return "spot"
        if not first.startswith(",,,,Sample 1,"):
            raise ValueError(f"Unrecognized Qtegra format: {path}")
        handle.readline()  # Acquisition labels.
        first_channel = None
        found_y = False
        for line in handle:
            keys = line.rstrip("\r\n").split(",", 4)
            if keys[0] == "StartTime":
                return "rich"
            if len(keys) >= 4 and keys[0] == "MainRuns":
                if keys[3] != "Y":
                    return "rich"
                if first_channel is not None and keys[2] != first_channel:
                    break
                first_channel = keys[2]
                found_y = True
            elif found_y:
                break
        if found_y:
            return "rich" if _has_start_time_footer(path) else "upbr"
    raise ValueError(f"No Qtegra MainRuns data: {path}")


def open_qtegra(path, **kwargs) -> QtegraSession:
    """Open a file or discover a session and its immediate companion folder.

    ``cycle_time_s`` and ``acquisition_gap_s`` are forwarded for UPbR input.
    Rich takes precedence over derived UPbR files, which produce a warning.
    Ambiguous Rich or companion candidates raise ValueError.
    """
    unexpected = set(kwargs) - {"cycle_time_s", "acquisition_gap_s"}
    if unexpected:
        raise TypeError(f"Unexpected open_qtegra arguments: {', '.join(sorted(unexpected))}")
    path = Path(path)
    kind = detect_format(path)
    if kind == "rich":
        return read_rich_csv(path)
    if kind == "upbr":
        return read_upbr_csv(path, **kwargs)
    if kind == "spot":
        return _spot_session([path])
    if kind == "spot_folder":
        return read_spot_folder(path)
    formats = _csv_formats(path)
    rich_files = [file for file, kind in formats.items() if kind == "rich"]
    upbr_files = [file for file, kind in formats.items() if kind == "upbr"]
    spot_paths = [file for file, kind in formats.items() if kind == "spot"]
    if len(rich_files) > 1:
        raise ValueError(f"More than one Rich CSV in {path}")
    if rich_files:
        session = read_rich_csv(rich_files[0])
        companions = [
            [file for file, kind in _csv_formats(child).items() if kind == "spot"]
            for child in sorted(path.iterdir()) if child.is_dir()
        ]
        companions = [files for files in companions if files]
        if len(companions) > 1 or (spot_paths and companions):
            raise ValueError(f"Ambiguous per-spot companion folders in {path}")
        if spot_paths or companions:
            session = merge(session, _spot_session(spot_paths or companions[0]))
        if upbr_files:
            session.warnings.append(
                f"Derived UPbR CSV ignored beside Rich: {', '.join(file.name for file in upbr_files)}"
            )
        return session
    if spot_paths:
        session = _spot_session(spot_paths)
        if upbr_files:
            session.warnings.append("Derived UPbR CSV ignored beside per-spot exports.")
        return session
    if len(upbr_files) == 1:
        return read_upbr_csv(upbr_files[0], **kwargs)
    if len(upbr_files) > 1:
        raise ValueError(f"More than one UPbR CSV in {path}")
    raise ValueError(f"No Qtegra exports found in {path}")
