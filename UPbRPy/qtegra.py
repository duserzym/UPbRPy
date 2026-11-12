"""Read Qtegra exports into one acquisition model without changing their clocks.

Rich CSVs are the acquisition record; per-spot CSVs supply dwell and detector
calibration metadata.
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
    "Channel", "Acquisition", "SourceFile", "QtegraSession", "UnsupportedQtegraFormat",
    "read_rich_csv", "read_spot_folder", "read_spot_file",
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


class UnsupportedQtegraFormat(ValueError):
    """A Qtegra export lacks the time information required for reduction."""


def _no_time_error(path):
    return UnsupportedQtegraFormat(
        f"Unsupported Qtegra export: no time information: {path.name} has "
        "MainRuns Y rows but no MainRuns Time rows and no StartTime row. Export the "
        "Rich single CSV (Y, Time, Analog, Counter, X [u] and the metadata "
        "rows) or the per-spot folder."
    )


def _ignored_time_warnings(unsupported):
    return [
        f"Ignored {path.name}: no time information (Y-only export)."
        for path, _error in unsupported
    ]


@dataclass(frozen=True)
class Channel:
    """One channel in instrument order, with metadata when exported."""

    name: str
    mass: float | None = None
    dwell_s: float | None = None
    xcal: float | None = None


@dataclass
class Acquisition:
    """One acquisition; sweep arrays retain missing values as NaN.

    ``time`` contains seconds since this acquisition's recorded start.
    """

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
    # Native pandas column dtypes preserve the historical experiment tables.
    # Y stays a float array; consumers may change it without rereading a file.
    _data_dtypes: dict[str, str] = field(default_factory=dict, repr=False)

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
        """Seconds since acquisition start, using the first channel if needed."""
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
    """Acquisitions in sequence order, or chronological order for spot files.

    ``times_reconstructed`` marks nominal sweep timing from StartTime when
    Rich Time rows are absent; ``nominal_cycle_time_s`` records that spacing.
    """

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
    if (frame.iloc[1, :4] != "").any() or (frame.iloc[1, columns] == "").all():
        raise ValueError(
            f"Missing acquisition label row (header row 2) in {path.name}: "
            "row 2 must have four empty cells followed by one label per acquisition."
        )
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
    data_dtypes = []
    y_rows = main.loc[main[3] == "Y"]
    y_sweeps = pd.to_numeric(y_rows[1]).to_numpy()
    channel_masks = {name: (y_rows[2] == name).to_numpy() for name in names}
    for index, column in enumerate(columns):
        count = _sweep_count(cubes["Y"][index])
        dtypes = {}
        for name in names:
            texts = y_rows.loc[channel_masks[name] & (y_sweeps < count), column]
            if (texts != "").any():
                dtypes[name] = str(pd.to_numeric(texts, errors="coerce").dtype)
        data_dtypes.append(dtypes)
    return path, raw, frame, columns, labels, names, cubes, data_dtypes


def read_rich_csv(path, *, cycle_time_s=None) -> QtegraSession:
    """Read a Rich CSV, preserving original timestamp strings and detector data.

    Requires two headers, MainRuns Y for every channel, and Time or StartTime.
    Other row groups are optional. Without Time, reconstruct shared sweep
    offsets from StartTime with finite positive ``cycle_time_s`` (None uses
    DEFAULT_CYCLE_TIME_S). With Time rows, recorded times win and this keyword
    is ignored. StopTime is never used to estimate sweep spacing.

    Design for large files: read the bytes once, parse the table once with
    pandas, and convert/scatter each whole quantity block with pandas/numpy.
    Python loops assemble acquisitions and their short metadata dictionaries;
    no Python loop visits individual sweep cells.
    """
    path, raw, frame, columns, labels, names, cubes, data_dtypes = _read_single(
        path, ("Y", "Time", "Analog", "Counter", "X [u]"),
    )
    if "Time" not in cubes and not (frame[0] == "StartTime").any():
        raise _no_time_error(path)
    y_names = set(frame.loc[(frame[0] == "MainRuns") & (frame[3] == "Y"), 2])
    if missing := set(names) - y_names:
        raise ValueError(f"Missing MainRuns Y rows for channels: {', '.join(sorted(missing))}")
    reconstructed = "Time" not in cubes
    cycle = None
    if reconstructed:
        try:
            cycle = float(DEFAULT_CYCLE_TIME_S if cycle_time_s is None else cycle_time_s)
        except (TypeError, ValueError):
            raise ValueError("cycle_time_s must be finite and > 0.") from None
        if not np.isfinite(cycle) or cycle <= 0:
            raise ValueError("cycle_time_s must be finite and > 0.")
    groups = set(frame[0])
    capabilities = set()
    for present, capability in (
        ("Time" in cubes, "per_channel_time"),
        ("Analog" in cubes and "Counter" in cubes, "analog_counter"),
        ("X [u]" in cubes, "mass"),
        (bool(groups & {"TuneSettings", "StatusReport"}), "instrument_state"),
        (((frame[0] == "Info") & (frame[1] == "UserName")).any(), "user"),
        ("StartTime" in groups, "timestamps"),
        ("StopTime" in groups, "stop_times"),
        (bool(groups & {"BrigidMS", "PMIAnalyteLaser"}), "sequence_fields"),
    ):
        if present:
            capabilities.add(capability)
    mass_values = np.full((len(labels), len(names)), np.nan)
    if "X [u]" in cubes:
        masses = cubes["X [u]"]
        mass_values = np.take_along_axis(
            masses, np.isfinite(masses).argmax(axis=1)[:, None, :], axis=1,
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
            np.arange(count) * cycle if reconstructed else cubes["Time"][i, :count].copy(),
            analog=cubes["Analog"][i, :count].copy() if "analog_counter" in capabilities else None,
            counter=cubes["Counter"][i, :count].copy() if "analog_counter" in capabilities else None,
            spot_number=_spot_number(label), source_path=path,
            _data_dtypes={"Time": "float64", **data_dtypes[i]},
        ))
    session = QtegraSession(
        acquisitions, "rich", frozenset(capabilities),
        (SourceFile(path, sha256(raw).hexdigest(), "rich"),),
        times_reconstructed=reconstructed, nominal_cycle_time_s=cycle,
    )
    if reconstructed:
        session.warnings.append(
            f"No MainRuns Time rows: sweep times reconstructed with cycle_time_s={cycle} s from StartTime."
        )
    if "timestamps" not in capabilities:
        session.warnings.append(
            "No StartTime row: acquisitions are placed back to back on the session "
            "time axis; time-dependent drift corrections are approximate."
        )
    if ("Analog" in cubes) != ("Counter" in cubes):
        missing = "Counter" if "Analog" in cubes else "Analog"
        session.warnings.append(f"Missing MainRuns {missing} rows: ignoring unpaired Analog/Counter data.")
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
    table = table.iloc[1:].reset_index(drop=True).dropna(axis=1, how="all")
    table = table.apply(pd.to_numeric, errors="coerce")
    y = table.reindex(columns=names).to_numpy(dtype=float)
    count = _sweep_count(y)
    acquisition = Acquisition(
        label, 0, tuple(channels), y[:count].copy(),
        table["Time"].iloc[:count].to_numpy(dtype=float),
        start_text=start_text, start=datetime.strptime(start_text, "%m/%d/%Y %I:%M:%S %p"),
        tune_name=lines[3].split(":", 1)[0],
        sequence_fields={"spot_file_start_text": lines[0]},
        spot_number=_spot_number(label), source_path=path,
        _data_dtypes={name: str(dtype) for name, dtype in table.dtypes.items()},
    )
    software_fields = _key_values(lines[1].partition(":")[2])
    software = " ".join(software_fields[key] for key in ("Name", "Version") if software_fields.get(key))
    machine = _key_values(lines[2].partition(":")[2]).get("Machine")
    acquisition.tune_settings = {
        key: _state_value(value)
        for key, value in _key_values(lines[3].partition(":")[2]).items() if value
    }
    for line in lines[4:11]:
        acquisition.readbacks.update({
            key: _state_value(value)
            for key, value in _key_values(line.partition(":")[2]).items() if value
        })
    threshold = _key_values(lines[11].partition(":")[2]).get("Threshold")
    return (
        acquisition, software or None, machine, float(threshold) if threshold else None,
        SourceFile(path, sha256(raw).hexdigest(), "spot"),
    )


def read_spot_file(path) -> Acquisition:
    """Read one per-spot CSV; its sequence_index is zero until folder ordering."""
    return _read_spot(path)[0]


def _csv_formats(path, *, unsupported=None):
    found = {}
    for child in sorted(Path(path).iterdir()):
        if child.is_file() and child.suffix.lower() == ".csv":
            try:
                found[child] = detect_format(child)
            except UnsupportedQtegraFormat as error:
                if unsupported is not None:
                    unsupported.append((child, error))
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

    Non-CSV files and CSVs with other layouts are ignored. Y-only exports
    without time information add a session warning.
    """
    unsupported = []
    formats = _csv_formats(path, unsupported=unsupported)
    session = _spot_session([file for file, kind in formats.items() if kind == "spot"])
    session.warnings.extend(_ignored_time_warnings(unsupported))
    return session


def _check_pair(rich, spot, rtol, *, check_time=True):
    label = rich.label
    if rich.channel_names != spot.channel_names:
        raise ValueError(f"{label}: channel names differ: {rich.channel_names} vs {spot.channel_names}")
    if rich.y.shape != spot.y.shape:
        raise ValueError(f"{label}, channel {rich.channel_names[0]}: Y sweep counts differ")
    equal = np.isclose(rich.y, spot.y, rtol=rtol, atol=0, equal_nan=True)
    if not equal.all():
        channel = rich.channel_names[np.argwhere(~equal)[0, 1]]
        raise ValueError(f"{label}, channel {channel}: Y values or NaN masks differ")
    if check_time and (rich.sweep_time is None or spot.sweep_time is None
            or rich.sweep_time.shape != spot.sweep_time.shape
            or not np.allclose(rich.sweep_time, spot.sweep_time, rtol=rtol, atol=0, equal_nan=True)):
        raise ValueError(f"{label}, channel {rich.channel_names[0]}: sweep times differ")


def merge(rich: QtegraSession, folder: QtegraSession, *, rtol=1e-12) -> QtegraSession:
    """Return a Rich session enriched by matching per-spot labels, without mutation.

    Validate Y, missing-value masks and first-channel sweep times before
    merging. The observed clock offset is recorded, and neither clock shifts.
    ``spot_file_start_text`` retains the complete line 1 without its terminator.
    StartTime is required for every Rich acquisition. Reconstructed Rich sweep
    times are retained and are not compared to recorded companion times.
    """
    if rich.format != "rich" or folder.format != "spot_folder":
        raise ValueError("merge requires a rich session and a spot_folder session.")
    if any(acquisition.start is None for acquisition in rich.acquisitions):
        raise ValueError("StartTime is needed to merge a Rich session with per-spot files.")
    if len(set(rich.labels)) != len(rich.labels) or len(set(folder.labels)) != len(folder.labels):
        raise ValueError("Cannot merge duplicate acquisition labels.")
    companions = {acquisition.label: acquisition for acquisition in folder.acquisitions}
    missing = [label for label in rich.labels if label not in companions]
    if missing:
        raise ValueError(f"Missing per-spot labels: {', '.join(missing)}")
    for acquisition in rich.acquisitions:
        _check_pair(acquisition, companions[acquisition.label], rtol,
                    check_time=not rich.times_reconstructed)
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


def _count_quantum(values, *, max_counts=20, tolerance=0.05, min_points=10):
    """Return the cps step of one pulse count, or None when Y is not quantized.

    Pulse-counting Y is ``n / dwell`` (dead-time corrected), so low positive
    values are integer multiples of ``1 / dwell``. The five smallest gaps
    between the lowest distinct values are tried as the step, smallest first;
    a step is accepted when at least 98 % of the values up to ``max_counts``
    steps lie within ``tolerance`` counts of an integer and they span at least
    three distinct counts. The accepted step is refined as the median of
    ``value / count``; dead-time correction is negligible at these rates.
    """
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values) & (values > 0)]
    if values.size < min_points:
        return None
    distinct = np.unique(np.round(values, 6))[:200]
    gaps = np.diff(distinct)
    # Rounding and dead-time correction split one count into near-equal values.
    gaps = np.unique(gaps[gaps > 1e-3 * distinct[1:]])
    for step in gaps[:5]:
        low = values[values <= (max_counts + 0.5) * step]
        if low.size < min_points:
            continue
        counts = np.round(low / step)
        integral = (np.abs(low / step - counts) <= tolerance) & (counts > 0)
        if integral.mean() < 0.98 or np.unique(counts[integral]).size < 3:
            continue
        return float(np.median(low[integral] / counts[integral]))
    return None


def counting_dwell(session: QtegraSession) -> dict[str, tuple[float | None, str | None]]:
    """Return each channel's dwell time in seconds and how it was obtained.

    Qtegra Y is a count rate in cps; Poisson statistics apply to the
    integrated counts ``Y * dwell``. The value per channel is

    - ``"recorded"``: the median exported dwell (per-spot files);
    - ``"count_quantization"``: ``1 / step``, where ``step`` is the cps value
      of one pulse count recovered from the low pulse-counting values of all
      acquisitions (Rich files carry no dwell);
    - ``"channel_timing"``: the median time between this channel's and the
      next channel's reading (the last channel uses the sweep spacing). This
      includes the settling time and so overestimates the dwell slightly;
    - ``(None, None)`` when none of these is available.
    """
    names = list(session.channel_names)
    result = {}
    for name in names:
        recorded = [
            channel.dwell_s
            for acquisition in session.acquisitions
            for channel in acquisition.channels
            if channel.name == name and channel.dwell_s is not None
            and np.isfinite(channel.dwell_s) and channel.dwell_s > 0
        ]
        if recorded:
            result[name] = (float(np.median(recorded)), "recorded")
            continue
        values = [
            acquisition.y[:, acquisition.channel_index(name)]
            for acquisition in session.acquisitions
            if name in acquisition.channel_names
        ]
        step = _count_quantum(np.concatenate(values)) if values else None
        if step is not None and 1e-5 < 1 / step < 10:
            result[name] = (1 / step, "count_quantization")
            continue
        gaps = []
        for acquisition in session.acquisitions:
            if not acquisition.time_per_channel or name not in acquisition.channel_names:
                continue
            index = acquisition.channel_index(name)
            time = acquisition.time
            if index + 1 < time.shape[1]:
                gap = time[:, index + 1] - time[:, index]
            elif time.shape[0] > 1:
                gap = time[1:, 0] - time[:-1, index]
            else:
                continue
            gaps.append(gap[np.isfinite(gap) & (gap > 0)])
        gaps = np.concatenate(gaps) if gaps else np.array([])
        result[name] = (float(np.median(gaps)), "channel_timing") if gaps.size else (None, None)
    return result


def _has_start_time_footer(path):
    # Scan metadata backwards to the last MainRuns row. Complete rows are
    # assembled across chunks so wide acquisition columns remain supported.
    with path.open("rb") as handle:
        position = handle.seek(0, 2)
        pending = b""
        while position:
            size = min(position, 4096)
            position -= size
            handle.seek(position)
            lines = (handle.read(size) + pending).split(b"\n")
            pending = lines[0]
            for line in reversed(lines[1:]):
                if line.startswith(b"StartTime,,,,"):
                    return True
                if line.startswith(b"MainRuns,"):
                    return False
        return pending.startswith(b"StartTime,,,,")


def detect_format(path) -> str:
    """Identify Qtegra layouts from headers, the first block and metadata footer.

    Directories containing only per-spot exports return ``spot_folder``;
    other directories return ``folder`` for discovery by :func:`open_qtegra`.
    Single CSVs with Y but no Time or StartTime raise UnsupportedQtegraFormat.
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
        if not handle.readline().startswith(",,,,"):  # Acquisition labels.
            raise ValueError(f"Missing acquisition label row (header row 2): {path}")
        first_channel = None
        found_y = False
        for line in handle:
            keys = line.rstrip("\r\n").split(",", 4)
            if keys[0] == "StartTime":
                return "rich"
            if len(keys) >= 4 and keys[0] == "MainRuns":
                if first_channel is not None and keys[2] != first_channel:
                    break
                first_channel = keys[2]
                if keys[3] == "Time":
                    return "rich"
                if keys[3] == "Y":
                    found_y = True
            elif found_y:
                break
        if found_y:
            if _has_start_time_footer(path):
                return "rich"
            raise _no_time_error(path)
    raise ValueError(f"No Qtegra MainRuns data: {path}")


def _companion_spot_paths(path, spot_paths, *, unsupported=None) -> list[Path]:
    companions = [
        [file for file, kind in _csv_formats(child, unsupported=unsupported).items()
         if kind == "spot"]
        for child in sorted(path.iterdir()) if child.is_dir()
    ]
    companions = [files for files in companions if files]
    if len(companions) > 1 or (spot_paths and companions):
        raise ValueError(f"Ambiguous per-spot companion folders in {path}")
    return spot_paths or (companions[0] if companions else [])


def companion_folder(rich_csv_path: str | Path) -> Path | None:
    """Find the per-spot companion beside a Rich CSV without loading data.

    Return its parent directory (the CSV's own directory or one immediate
    subdirectory), or None. Discovery uses the same rules as open_qtegra;
    ambiguous exports raise ValueError. To include it, open the Rich CSV's
    parent directory, which merges the exports.
    """
    rich_csv_path = Path(rich_csv_path)
    if detect_format(rich_csv_path) != "rich":
        return None
    parent = rich_csv_path.parent
    formats = _csv_formats(parent)
    if sum(kind == "rich" for kind in formats.values()) > 1:
        raise ValueError(f"More than one Rich CSV in {parent}")
    spots = _companion_spot_paths(
        parent, [file for file, kind in formats.items() if kind == "spot"],
    )
    return spots[0].parent if spots else None


def open_qtegra(path, *, cycle_time_s=None) -> QtegraSession:
    """Open a file or discover a session and its immediate companion folder.

    Exports without time information are skipped during folder discovery,
    with a warning if usable exports exist, otherwise their error is raised.
    Ambiguous Rich or companion candidates raise ValueError.
    ``cycle_time_s`` is forwarded to read_rich_csv for missing Time rows;
    None uses DEFAULT_CYCLE_TIME_S. Recorded Time rows and per-spot input
    ignore this keyword.
    """
    path = Path(path)
    kind = detect_format(path)
    if kind == "rich":
        return read_rich_csv(path, cycle_time_s=cycle_time_s)
    if kind == "spot":
        return _spot_session([path])
    unsupported = []
    formats = _csv_formats(path, unsupported=unsupported)
    rich_files = [file for file, kind in formats.items() if kind == "rich"]
    spot_paths = [file for file, kind in formats.items() if kind == "spot"]
    if len(rich_files) > 1:
        raise ValueError(f"More than one Rich CSV in {path}")
    if rich_files:
        session = read_rich_csv(rich_files[0], cycle_time_s=cycle_time_s)
        spots = _companion_spot_paths(path, spot_paths, unsupported=unsupported)
        if spots:
            session = merge(session, _spot_session(spots))
    elif spot_paths:
        session = _spot_session(spot_paths)
    elif unsupported:
        raise unsupported[0][1]
    else:
        raise ValueError(f"No Qtegra exports found in {path}")
    session.warnings.extend(_ignored_time_warnings(unsupported))
    return session
