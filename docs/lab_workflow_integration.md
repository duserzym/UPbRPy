# UPbRPy Studio in the lab workflow: database mode

**Status:** implemented 2026-11-03 (steps 1 to 5 of section 4: commits
44f9104, 8fcd4a7, cca3195, 21bf44f), verified end to end on macOS against a
local laicpmsdb with the full 197-spot `Apatite_20Mar25` session (link with
handoff code, fetch, auto-pick, reduce, submit, prefer; preferred dates equal
to laicpmsdb's own script reduction of the same bundle). The Windows
registration (`--register-url-scheme`) and a frozen-app launch by a real
`upbr://` click are not yet tried on a machine. Part of the cross-repository
contract `upbstudio/docs/WORKFLOW.md` v1 (sections 3.2, 3.3 and 4), which wins
where the two differ; laicpmsdb's `docs/workflow_api.md` gives the server's
exact shapes. `GUI/roadmap.md` keeps the architecture of Studio itself.

## 1. What changes and what does not

UPbRPy Studio stays a native PySide6 + pyqtgraph application, and the engine
in `UPbRPy/` stays free of Qt, HTTP and database code. Standalone use is
unchanged: open a Qtegra export directory or a `.upbr`, reduce, export the
reduction bundle. **Database mode** adds one optional way in and one way out:

- **In:** open a session from a laicpmsdb server. Studio fetches the
  *session bundle* (`raw/` CSVs byte-identical, `session_manifest.toml`, a
  pre-filled `.upbr` with an `origin` block) and opens it exactly as a local
  folder. It arrives through a link, `upbr://session/<id>?server=&code=`,
  from UPb Studio or a browser, or through **File > Open from database…**.
- **Out:** **Submit to database** posts the *reduction bundle* that
  `export_reduction_bundle` already writes to the session's origin server,
  and records the returned run id in the `.upbr`. **Mark as preferred** calls
  the server once more.

A user without the database can receive a session bundle as a zip and send a
reduction bundle back as a zip; the files are identical to what database mode
moves over HTTP.

## 2. Pieces

| Piece | Where | Notes |
| --- | --- | --- |
| `[database]` extra | `pyproject.toml` | `requests` (or `httpx`) and `keyring`; the GUI imports them lazily and shows the two database actions disabled with a one-line reason when the extra is missing |
| client | `UPbRPy/integration/laicpmsdb.py` | thin, typed, no Qt: `exchange_code`, `fetch_session_bundle`, `submit_reduction`, `prefer_run`, `whoami`; bearer token per server stored in the OS keychain (`keyring`, service `UPbRPy`), never in files; errors mapped to messages the GUI can show |
| `origin` in the project | `UPbRPy/project.py` | format version 2, additive: `origin { server, session_id, session_key, export_sha256, exported_at, run_id? }`; version 1 files still load |
| manifest provenance | `UPbRPy/pipeline.py` `manifest()` | `hash_inputs=True` by default in `export_reduction_bundle`; `reference_library_version` from `library.toml`; `metadata.origin` echoing the project's origin (laicpmsdb plan section 8.3) |
| session manifest | `UPbRPy/io.py` | `read_experiment_data` accepts `session_manifest.toml` beside `raw/` as the source of `sample_aliases`, `sep` and the reference-material plan; the GUI pre-fills from it |
| URL scheme | `GUI/main.py`, `packaging/UPbRPyStudio.spec`, installer | `--open` accepts a directory, a `.upbr`, a session-bundle zip, or a `upbr://` URL; macOS `CFBundleURLTypes` for `upbr` in the spec and a `QFileOpenEvent` handler; Windows registry entry `HKCU\Software\Classes\upbr` written by the installer (and offered on first run of a portable build); a second launch hands the URL to the running instance (local socket) |
| database mode UI | `GUI/` | a "Database" group in the File menu: Open from database… (server URL and sign-in: password, or ORCID through the system browser returning to `upbr://auth/callback?code=`), Submit to database, Mark as preferred; the origin shown in the window title and the project panel; a status row with the run id after a submit |
| cache | `~/UPbRPy/sessions/<session_key>/` | one folder per fetched session; the bundle hash tells Studio whether a re-fetch is needed; the user may move it |
| tests | `tests/` | a pytest fixture that serves a fake laicpmsdb (`http.server` in a thread) with the export, exchange, import and prefer routes; project format v2 round trip; `--open upbr://` parsing; manifest provenance |

## 3. The exchange

```text
browser or UPb Studio ── upbr://session/<id>?server=S&code=C ──> Studio
Studio ── POST S/v1/auth/exchange {code} ──> bearer token (keychain)
Studio ── GET  S/v1/sessions/<id>/export/upbrpy ──> zip ──> ~/UPbRPy/sessions/<key>/
Studio opens <key>/<key>.upbr (origin set) ── reduce locally ── export_reduction_bundle
Studio ── POST S/v1/sessions/<id>/runs/import (multipart zip) ──> {run_id, counts, quarantine}
Studio ── POST S/v1/sessions/<id>/runs/<run_id>/prefer ──> ok
```

Without `code`, Studio uses the keychain token for `S`, else asks to sign
in. Every request carries `User-Agent: UPbRPy-Studio/<version>`.

## 4. Order of work

1. Project format v2 with `origin`; manifest provenance; `session_manifest.toml`
   in the reader (engine only, no GUI).
2. The client module and the fake-server tests.
3. `--open` of `.upbr`, zips and `upbr://` URLs; the scheme registration in
   the PyInstaller spec and installer; single-instance hand-off.
4. The File > Database actions and the sign-in dialog.
5. End-to-end against a local laicpmsdb with the fixture session
   `Apatite_20Mar25`: open through the link, reduce, submit, prefer, and
   compare the preferred ages with the frozen publication tables.

Done when a session page in laicpmsdb launches Studio on a Mac and on
Windows, the reduction appears in the database, and the standalone test
suite still passes with the `[database]` extra absent.

## 5. Choices made while implementing

- `origin.export_sha256` is the SHA-256 of `session_manifest.toml` (the
  server's `X-Export-SHA256`), not of the zip, which is streamed and not
  reproducible. An equal hash keeps the cached folder, saved project
  included; a different one moves it aside as `<key>.superseded-<UTC time>`.
- `origin.server` is replaced by the base URL Studio actually used, since the
  keychain token belongs to that URL.
- The reduction zip has its files at the top level and includes
  `<session_key>.upbr`; a resubmission sends `supersedes=<previous run_id>`.
- `upbr://run/<id>` opens a cached `.upbr` whose `origin.run_id` matches, else
  asks `GET /v1/runs/{id}` for the session and fetches it.
- A session manifest's `sep` and `[samples]` win over a mineral preset's
  defaults in Studio (explicit project settings still win), and only `raw/*.csv`
  are hashed as inputs.

## 6. Qtegra input layer for laicpmsdb

The UPbRPy input layer was implemented on 2026-11-04 and updated 2026-11-05.
The laicpmsdb `POST /v1/sessions/ingest` endpoint and watcher should import
these readers
rather than maintain another parser. The Rich single CSV is the canonical
record; a per-spot folder supplies companion metadata and remains an
accepted standalone input. Y-only exports are unsupported because they have
no time information. The export and storage contract is
[`qtegra_export_formats.md`](qtegra_export_formats.md), especially
[Where it lands](qtegra_export_formats.md#where-it-lands). The storage changes
below describe the laicpmsdb integration contract.

### Changed 2026-11-05

For laicpmsdb ingest callers:

- Removed `read_upbr_csv(path, *, cycle_time_s=None, acquisition_gap_s=0.0)`
  and `Acquisition.start_offset_s`.
- Removed `acquisition_gap_s`; Y-only exports no longer have nominal timing.
- Milestone 2 adds reduced Rich support to
  `read_rich_csv(path, *, cycle_time_s=None)` and
  `open_qtegra(path, *, cycle_time_s=None)`, and restores keyword-only
  `cycle_time_s=None` and the reconstructed-times `UserWarning` in
  `read_experiment_data`. This keyword now controls only Rich sweep timing
  when StartTime exists and Time rows do not; recorded Time and per-spot
  input ignore it.
- Rich capabilities now reflect optional row groups. Minimum: two headers,
  MainRuns Y for every channel, and MainRuns Time or StartTime. Merge requires
  Rich StartTime; reconstructed times are retained without comparing them
  to recorded companion times.
- Removed the `"upbr"` result from `detect_format`, session formats and
  source formats. Added exported `UnsupportedQtegraFormat(ValueError)` for
  single CSVs with MainRuns Y but no Time or StartTime.
- No public symbols were renamed. `times_reconstructed`,
  `nominal_cycle_time_s` and `DEFAULT_CYCLE_TIME_S` remain available.

### Reader API: `UPbRPy.qtegra`

These declarations give the current constants, dataclass fields and public
signatures; method bodies are omitted. `np` is numpy, `Path` is
`pathlib.Path`, `datetime` is `datetime.datetime`, and `dataclass` and `field`
come from `dataclasses`.

```python
CAPABILITIES = frozenset({
    "timestamps", "stop_times", "per_channel_time", "analog_counter", "mass",
    "dwell_xcal", "instrument_state", "sequence_fields", "user",
})
COUNTER_SATURATED = -33.0
DEFAULT_CYCLE_TIME_S = 0.4255

class UnsupportedQtegraFormat(ValueError): ...

@dataclass(frozen=True)
class Channel:
    name: str
    mass: float | None = None
    dwell_s: float | None = None
    xcal: float | None = None

@dataclass
class Acquisition:
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
    _data_dtypes: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def channel_names(self) -> tuple[str, ...]: ...
    @property
    def n_sweeps(self) -> int: ...
    @property
    def sweep_time(self) -> np.ndarray | None: ...
    @property
    def time_per_channel(self) -> bool: ...
    def channel_index(self, name) -> int: ...

@dataclass(frozen=True)
class SourceFile:
    path: Path
    sha256: str
    format: str

@dataclass
class QtegraSession:
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
    def channel_names(self) -> tuple[str, ...]: ...
    @property
    def labels(self) -> list[str]: ...
    def acquisition(self, label) -> Acquisition: ...
    def has(self, capability) -> bool: ...

def read_rich_csv(path, *, cycle_time_s=None) -> QtegraSession: ...
def read_spot_folder(path) -> QtegraSession: ...
def read_spot_file(path) -> Acquisition: ...
def merge(rich: QtegraSession, folder: QtegraSession, *, rtol=1e-12) -> QtegraSession: ...
def recover_xcal(session_or_acquisition) -> dict[str, float]: ...
def detect_format(path) -> str: ...
def open_qtegra(path, *, cycle_time_s=None) -> QtegraSession: ...
```

`y`, `analog` and `counter` have shape `(n_sweeps, n_channels)` in instrument
channel order; missing cells stay NaN, and counter saturation stays `-33`.
Recorded Rich `time` has the same shape; reconstructed Rich and per-spot
`time` have shape `(n_sweeps,)`. `sweep_time` selects the first channel when
time is per channel. Trailing sweeps with no Y are dropped; interior missing cells stay
missing. `_data_dtypes` is internal compatibility metadata for experiment
tables; ingest need not store it.

Single CSV acquisitions retain sequence order. Per-spot acquisitions are
sorted by start time, with file-name ties; `sequence_index` is one-based.
`read_spot_file` returns one acquisition with `sequence_index=0`.
`SourceFile.format` is `"rich"` or `"spot"`; `QtegraSession.format`
is `"rich"`, `"spot_folder"` or `"rich+spot_folder"`. Each source
has a SHA-256 of the original file bytes.

`detect_format` returns `"rich"`, `"spot"`, `"spot_folder"` or
`"folder"`, or raises `ValueError`. `open_qtegra` reads a file directly;
for a folder it discovers Rich in that folder and per-spot files there or
in one immediate companion subfolder. It merges companions by label,
skips Y-only exports with the warning `Ignored <name>: no time information
(Y-only export).`, and rejects multiple Rich files or ambiguous companions.
If no usable export exists, it raises the unsupported-file error. Direct
Y-only file detection and opening raise `UnsupportedQtegraFormat` with a
message starting `Unsupported Qtegra export: no time information` and export
instructions. A Time row in the first channel block or StartTime in the
footer identifies time information. Reduced Rich reading needs the two
headers, MainRuns Y for every channel, and Time or StartTime; other groups
are optional. The footer scan handles any number of metadata rows and no
footer at all.

`merge` returns a new session, validating channel order, Y, NaN masks and
first-channel times (`rtol=1e-12`, `atol=0`). StartTime is needed for every
Rich acquisition, otherwise merge raises ValueError. Reconstructed Rich
times are retained and skip the companion-time equality check.
Every Rich label must have a companion; extras produce a warning. It takes
dwell/xcal, software, machine
and pulse-counting threshold from the folder, fills empty Rich instrument
state dictionaries, and retains both sources. `recover_xcal` returns each
channel's median `y / analog` on saturated sweeps with finite Y and finite,
nonzero analog; channels without such sweeps are absent.

### Experiment adapter and detector calibration

Import the adapter from `UPbRPy.io`:

```python
def experiment_from_session(session, sep="_", sample_aliases=None): ...

def read_experiment_data(
    directory, primary_channel='29Si', baseline_sweep_n=10, peak_sweep_n=20,
    threshold=0.1, filter_percentile=70, baseline_offset=2, plateau_offset=2,
    sep=None, sample_aliases=None, selection_channels=None,
    selection_method='threshold', selection_fallback_methods=(),
    selection_options=None, signal_duration_limit_seconds=None,
    session_manifest=None, *, detector_calibration=None, cycle_time_s=None,
): ...
```

`experiment_from_session` returns `(experiment, element_channels, total_data)`
before automatic window selection; `read_experiment_data` also initializes
selections. `experiment` contains `spot_names`, `sample`, `spot_paths`,
`spot_starting_times` in session seconds, and the six initially empty
baseline/plateau/signal start/end columns. `total_data` contains `Time` in
session seconds (using each acquisition's first-channel sweep time), Y
columns, `spot_name` and `sample`. Samples are the first label component
split at `sep`, with case-insensitive aliases applied. JSON-safe input
provenance is in `experiment.attrs["qtegra"]`: `format`, `capabilities`,
`sources`, `times_reconstructed`, `nominal_cycle_time_s`,
`clock_offset_hours`, `software`, `machine`, `warnings`.

Import detector calibration from `UPbRPy.calibration`:

```python
@dataclass
class ChannelCrossCalibration:
    channel: str
    qtegra_factor: float | None
    factor: float
    factor_se: float
    intercept: float
    intercept_se: float
    n_points: int
    n_acquisitions: int
    times_s: np.ndarray | None = None
    factors: np.ndarray | None = None
    factors_se: np.ndarray | None = None
    raw_factors: np.ndarray | None = None
    acquisition_labels: list[str] = field(default_factory=list)

    def factor_at(self, t_s) -> np.ndarray: ...

@dataclass
class CrossCalibration:
    mode: str
    on: str
    window: tuple[float, float]
    channels: dict[str, ChannelCrossCalibration]
    primary_standard: str | None = None
    smoothing_s: float = 1800.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict: ...

def fit_cross_calibration(session, channel=None, window=(1e5, 2.4e6),
                          on="all", mode="constant", *, primary_standard=None,
                          sep="_", smoothing_s=1800.0) -> CrossCalibration: ...
def apply_cross_calibration(session, calibration) -> QtegraSession: ...
```

`channel` is a name, a list of names, or `None` for all eligible channels.
`mode` is `"qtegra"`, `"constant"` or `"drift"`; `on` is `"all"` or
`"primary_standard"`. The latter requires `primary_standard` and matches
the first label component case-insensitively. Constant and drift require
`analog_counter`; Qtegra mode records exported/recovered factors without
fitting and applies an unchanged copy.

The constant fit is OLS `counter = factor * analog + intercept`, with
finite, positive analog and valid counter inside the inclusive window.
Standard errors use residual variance with `n-2` degrees of freedom. Drift
fits per-acquisition slopes with at least five overlap points, holding the
constant fit's intercept fixed (`n-1` degrees of freedom). It smooths them
with a Gaussian kernel in session seconds, bandwidth `smoothing_s` and
inverse slope-variance weights. `times_s` are mean overlap-point times;
drift arrays and `acquisition_labels` are chronological. In drift mode
`factor` is the inverse-variance weighted mean of the raw slopes;
`factor_at` interpolates the smoothed factors linearly and holds endpoint
values outside their time range. Without qualifying drift fits it retains
the constant fit and records a warning. Errors are one standard error.

`apply_cross_calibration` returns a deep copy, replacing saturated Y with
`analog * factor_at(session_time)` only for calibrated channels with finite
analog. The intercept is reported but not applied; other Y cells and source
measurements are retained. `to_dict()` returns JSON-safe settings, warnings
and channel diagnostics (including drift arrays in drift mode); unknown or
nonfinite numbers become `None`.

### Capabilities, clocks and precision

| Session | Capabilities |
| --- | --- |
| Full Rich | `timestamps`, `stop_times`, `per_channel_time`, `analog_counter`, `mass`, `instrument_state`, `sequence_fields`, `user` |
| Reduced Rich | Only capabilities supplied by exported row groups; never `dwell_xcal` |
| Per-spot folder | `timestamps`, `instrument_state`, `dwell_xcal` |
| Rich + per-spot folder | Union of the Rich and folder capabilities |

Both recorded clocks are naive. Rich start/stop text is ISO 8601 with seven
fractional digits; parsing truncates to six fractional digits (microseconds)
while `start_text` and `stop_text` keep the original strings. Per-spot
`start_text` keeps its `MM/DD/YYYY hh:mm:ss AM/PM` timestamp and
`sequence_fields["spot_file_start_text"]` keeps the complete first line
without its terminator, also retained after merge. In this session the
per-spot clock is 7 h ahead of Rich and has only whole seconds. Merge never
shifts either clock: `clock_offset_hours` is the median folder-minus-Rich
offset rounded to the nearest 0.25 h (7.0 here), and
`clock_offset_seconds_median` stores the unrounded median. Store both
original texts and use the session's time-zone setting to resolve aware UTC
instants; never silently reinterpret the clocks.

Without MainRuns Time rows, Rich StartTime supplies calendar acquisition
starts and sweep offsets are `np.arange(n_sweeps) * cycle_time_s` (1-D).
None uses `DEFAULT_CYCLE_TIME_S` (0.4255 s, the Boise median sweep spacing);
explicit spacing must be finite and positive. StopTime includes overhead
and is never used to estimate spacing. `times_reconstructed=True` and
`nominal_cycle_time_s` record this choice in the session and experiment attrs.
The reader adds one session warning and `read_experiment_data` emits one
UserWarning mentioning reconstructed timing. With recorded Time rows these
fields remain False and None, and `cycle_time_s` is ignored; per-spot input
also ignores it. In-memory sessions retain their existing times.

With Time but no StartTime, experiment import uses a continuous axis in
sequence order: the next acquisition starts after the previous last sweep
by its median sweep spacing. Calendar gaps cannot be recovered.

Optional groups set `mass` (X [u]), `instrument_state` (TuneSettings or
StatusReport), `user` (Info/UserName), `timestamps` (StartTime), `stop_times`
(StopTime), and `sequence_fields` (BrigidMS or PMIAnalyteLaser, any subset).
Time rows set `per_channel_time`. Both Analog and Counter are needed for
`analog_counter`; an unpaired group is ignored with a session warning naming
the missing one. Absent fields remain None or empty dictionaries.

Rich prints Y with 17 significant digits, sufficient to round-trip a
double; per-spot CSVs print 15. For example, Rich
`449124.38355127671` becomes `449124.383551277` per spot. Keep Rich precision
and validate companions with the merge tolerance, not bitwise equality.

### What laicpmsdb should store

- Accept a Rich file, a per-spot folder, or both at ingest and in the
  watcher. Preserve the Rich CSV as a `RawAsset`, like each per-spot file,
  with original bytes and source SHA-256. Reject Y-only exports without time
  information. Keep full analytical files outside this code repository.
- Extend the Parquet sweep schema with nullable `analog`, `counter` and
  per-channel `time` columns. Preserve missing values and the counter
  saturation sentinel; reduction uses `y`.
- Store `Acquisition.channels` in instrument order with mass, dwell and
  xcal when known. Keep the session software, machine and pulse-counting
  threshold, and the two flat dictionaries `tune_settings` and `readbacks`
  in `instrument_state` (plus the tune name). Empty state values are
  omitted by the readers.
- Add acquisition `sequence_fields` JSON (original strings, including
  empty values) and a session-level `capabilities` list. Retain user,
  sequence index, original start/stop texts, clock-offset metadata,
  reconstruction flags and warnings. Labels join acquisitions to samples
  and spots using the session separator/aliases. Store Rich's
  `Laser Scan Position Number` as `spot_number` and check it against the
  trailing label number; the reader warns on a mismatch.
- Keep detector calibration as reduction provenance alongside raw Y. In
  UPbRPy the applied record is `experiment.attrs["detector_calibration"]`
  and the top-level `detector_calibration` in `reduction_manifest.json`;
  `PipelineConfig.detector_calibration` and the `.upbr` retain the choice,
  and publication-table settings report the mode and per-channel factors.
  Store each calibration choice with its reduction run and preserve raw assets.

UPb Studio needs only the file types its shell opens. Bracketeer's
sample-list columns correspond to Rich's `BrigidMS`/`PMIAnalyteLaser` rows,
which reconcile the planned sequence with acquired labels.
