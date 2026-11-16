# Qtegra input layer: plan and API (2026-11-04)

## Status

**2026-11-05, Milestone 1:** UPbR CSV dropped because it contains no time
information. Only Rich and per-spot exports are accepted. Detection raises
`UnsupportedQtegraFormat` for Y-only exports; discovery skips them with a
warning when usable inputs exist. Nominal timing fields and
`DEFAULT_CYCLE_TIME_S` remain reserved for a later milestone supporting
reduced Rich files. That reader expansion is not part of this milestone.
The API and consumer notes below reflect this decision.

Milestones 1–6 implemented on **2026-11-04**. The committed work, identified
by read-only `git log --oneline -8`, is milestone 1 (contract, plan and
fixture) `ce5d88e`, milestone 2 (readers) `c67f373`, milestone 3 (experiment
import) `c211ef1`, milestone 4 (detector calibration) `91afcbe`, and milestone
5 (Studio) `169878c`. Milestone 6 is this documentation update, not yet
committed; it has no commit hash.

Open questions and fixture limits:

- Should the applied correction include the fitted intercept, or should the
  fit pass through the origin? For full-session 238U, the through-origin
  slope is 105,047 versus 103,809 with an intercept. The implementation
  currently fits an intercept but applies only the slope.
- Can the Rich export be configured to include dwell time and xcal?
- Can the lab keep the LabBook file as the raw archive alongside its CSV
  exports?
- The fixture keeps 9 of 29 channels; it does not cover the full channel set.

Goal: UPbRPy and UPbRPy Studio open Rich and per-spot Qtegra exports
through one acquisition model, with the Rich single CSV as the canonical
record (`docs/qtegra_export_formats.md`), and offer a within-session detector
cross-calibration that only the Rich format makes possible. UPbDB's
ingest will import the readers from here, so the public names below are a
contract: do not rename them.

## Accepted formats, as found in the files

Fixture: `tests/fixtures/qtegra/Zircon_demo/` (made by
`scripts/make_qtegra_fixture.py` from the full session; 12 RM acquisitions selected by
label (NIST612 x3, NIST610 x3, S513 x2,
91500 x2, PL x2), 9 of the 29 channels: 29Si 91Zr 177Hf 204Hg 206Pb 207Pb
208Pb 232Th 238U). All files are UTF-8; the Rich CSV starts with a BOM; all
use CRLF line endings; no cell is quoted; every row ends with one empty cell.

**Rich single CSV** (`Zircon_demo_Rich.csv`). Columns 0 to 3 are keys,
column 4 + i is selected acquisition i, then the trailing empty cell.
Row 0 keeps the source acquisition numbers: `,,,,Sample 1,...,Sample 8,Sample 26,Sample 27,Sample 45,Sample 46,`.
Row 1: `,,,,<label 1>,<label 2>,...`.
Then:

- `MainRuns,<sweep>,<channel>,<quantity>,values...` for quantity in
  `X [u]` (mass), `Y` (cps used for reduction), `Time` (seconds since the
  acquisition start, **per channel**), `Analog` (analog-mode reading, cps),
  `Counter` (pulse-counting reading, cps; `-33` when the counter saturated).
  Order: for each channel in instrument order, a block per quantity in the
  order X, Y, Time, Analog, Counter, each with sweeps 0 to 72. Channel order
  is the order of first appearance.
- Empty cells: acquisitions with 72 sweeps have empty cells at sweep 72;
  those with 73 have values. Empty means missing, never zero. An acquisition
  has as many sweeps as its last sweep index with any non-empty Y.
- `TuneSettings,<tune name>,<setting>,Y,values...` (36 rows; tune name
  `Laser_STDS`). In this session acquisition 1 has all 36 empty: missing.
- `StatusReport,<group>,<readback>,Y,values...` (43 rows).
- `Info,UserName,,,<user>...`
- `StartTime,,,,2026-11-04T15:55:37.4924868,...` and `StopTime` likewise:
  naive ISO 8601 with **7** fractional digits (Python's
  `datetime.fromisoformat` accepts at most 6 before 3.11 and is strict about
  digit counts; parse explicitly and keep the original text).
- `BrigidMS,Label|Duration [s]|Comment,,,...` and
  `BrigidMS,TRQuant,<Evaluate|Sample Type|Standard|Dilution Factor|Amount|Final Quantity>,,...`
- `PMIAnalyteLaser,<Identifier|Laser Scan Position Number|Laser Output Setting [%]>,,,...`

**Per-spot folder** (`zircon_demo/`, one CSV per acquisition, file name
suffix = sequence number, but order by time, not by name):

```
line 1   <label>:MM/DD/YYYY hh:mm:ss AM/PM;        (7 h ahead of Rich StartTime here)
line 2   Software:Name=Qtegra;Version=2.10.4345.64;File Version=1;
line 3   Configuration:Machine=iCAP RQ;
line 4   Laser_STDS:<setting>=<value>;...            (tune settings)
lines 5-11  <group>:<readback>=<value>;...           (readbacks, 7 groups)
line 12  Pulse Counting:Threshold=2500000;
line 13  (empty)
line 14  Time,<channel>,...,                          (header)
line 15  ,dwell time=<s>;xcal factor=<f>,...,         (one cell per channel)
line 16+ <time>,<y>,...,                              (one row per sweep)
         (empty last line)
```

The per-spot `Time` column equals the Rich `Time` of the first channel.

## Milestone 2: `UPbRPy/qtegra.py`

Pure Python + numpy (pandas allowed for fast CSV parsing). No Qt.

```python
CAPABILITIES = frozenset({"timestamps", "stop_times", "per_channel_time",
    "analog_counter", "mass", "dwell_xcal", "instrument_state",
    "sequence_fields", "user"})
COUNTER_SATURATED = -33.0
DEFAULT_CYCLE_TIME_S = 0.4255  # Boise median sweep spacing; reserved

class UnsupportedQtegraFormat(ValueError): ...

@dataclass(frozen=True)
class Channel:
    name: str
    mass: float | None = None      # X [u]
    dwell_s: float | None = None   # per-spot "dwell time"
    xcal: float | None = None      # per-spot "xcal factor"

@dataclass
class Acquisition:
    label: str
    sequence_index: int            # 1-based position in the Qtegra sequence
    channels: tuple[Channel, ...]
    y: np.ndarray                  # (n_sweeps, n_channels) float, NaN = missing
    time: np.ndarray | None        # (n_sweeps, n_channels) if per-channel, else (n_sweeps,)
    analog: np.ndarray | None = None    # (n_sweeps, n_channels)
    counter: np.ndarray | None = None   # (n_sweeps, n_channels), -33 kept as is
    start_text: str | None = None  # original text, never rewritten
    start: datetime | None = None  # naive, parsed from start_text
    stop_text: str | None = None
    stop: datetime | None = None
    tune_name: str | None = None
    tune_settings: dict[str, float | str] = field(default_factory=dict)
    readbacks: dict[str, float | str] = field(default_factory=dict)
    sequence_fields: dict[str, str] = field(default_factory=dict)   # original strings
    user: str | None = None
    spot_number: int | None = None # Rich "Laser Scan Position Number"
    source_path: Path | None = None
    # properties: channel_names, n_sweeps, sweep_time (first channel's time, (n_sweeps,)),
    #             time_per_channel (bool), channel_index(name)

@dataclass(frozen=True)
class SourceFile:
    path: Path
    sha256: str
    format: str                    # "rich" | "spot"

@dataclass
class QtegraSession:
    acquisitions: list[Acquisition]   # in acquisition order (sequence order for single CSVs, time order for folders)
    format: str                       # "rich" | "spot_folder" | "rich+spot_folder"
    capabilities: frozenset[str]
    sources: tuple[SourceFile, ...]
    software: str | None = None       # e.g. "Qtegra 2.10.4345.64"
    machine: str | None = None        # e.g. "iCAP RQ"
    pulse_counting_threshold: float | None = None
    clock_offset_hours: float | None = None   # merged only; folder time minus Rich time
    times_reconstructed: bool = False         # reserved; always False
    nominal_cycle_time_s: float | None = None # reserved; always None
    warnings: list[str] = field(default_factory=list)
    # properties/methods: channel_names, labels, acquisition(label), has(capability)

def read_rich_csv(path) -> QtegraSession
def read_spot_folder(path) -> QtegraSession
def read_spot_file(path) -> Acquisition        # one per-spot CSV (sequence_index 0 until ordered)
def merge(rich: QtegraSession, folder: QtegraSession, *, rtol=1e-12) -> QtegraSession
def recover_xcal(session_or_acquisition) -> dict[str, float]
def detect_format(path) -> str                 # "rich" | "spot" | "spot_folder" | "folder" | raises ValueError
def open_qtegra(path) -> QtegraSession
```

Decisions:

- **Capabilities**: Rich = all but `dwell_xcal`; spot folder = `timestamps`,
  `instrument_state`, `dwell_xcal`; merged = union.
- **Sweep count**: drop trailing sweeps whose Y is empty for every channel;
  interior empty cells stay NaN.
- **Rich `time`** is per channel (n_sweeps, n_channels); spot folder `time` is
  shared (n_sweeps,).
- **Spot number**: parse the trailing integer after the last `sep` (`_`) of
  the label; when Rich gives `Laser Scan Position Number` and the two differ,
  add a warning naming the label (do not fail).
- **Folder order**: acquisitions sorted by start time (ties by file name);
  `sequence_index` = 1-based position in that order. Non-CSV files and CSVs
  that are not per-spot exports are ignored.
- **`merge(rich, folder)`**: match by label (every Rich label must be found,
  else `ValueError` listing the missing ones; extra folder labels are a
  warning). For each pair check channel names equal and `y` equal where both
  are finite (`np.allclose(..., rtol, atol=0)` with matching NaN masks), and
  the folder time equal to Rich first-channel time; any mismatch raises
  `ValueError` naming the label and channel. Take `dwell_s` and `xcal` per
  channel, `software`, `machine`, `pulse_counting_threshold` from the folder;
  fill Rich `tune_settings`/`readbacks` that are empty from the folder (warn
  per acquisition filled); keep the folder's original line-1 text in
  `sequence_fields["spot_file_start_text"]`. `clock_offset_hours` = median of
  (folder start - Rich start) in hours rounded to the nearest 0.25 h (7.0 for
  this session); store the unrounded median seconds in a warning-free
  attribute `clock_offset_seconds_median` too (add that field). Never shift
  either time.
- **`recover_xcal`**: for every channel with saturated sweeps
  (`counter == -33` and analog finite and nonzero), the factor is
  `median(y / analog)` over those sweeps; channels without saturation are
  absent from the result.
- **Nominal timing**: `times_reconstructed=False` and
  `nominal_cycle_time_s=None` for current readers. They and
  `DEFAULT_CYCLE_TIME_S = 0.4255` remain for later reduced-Rich support.
- **`open_qtegra(path)`**: a file is read by its detected format. A folder:
  collect its CSVs; if it holds a Rich file, read it and, if per-spot files
  are in the same folder or in exactly one immediate subfolder, merge them.
  A folder with only per-spot files is a spot folder. More than one Rich
  file is an error. Detection checks the headers, the first channel block
  for Time, and the footer for StartTime. A single CSV with MainRuns Y but
  neither Time nor StartTime raises `UnsupportedQtegraFormat`. Discovery
  skips such files and adds `Ignored <name>: no time information (Y-only
  export).` to a usable session, or raises the unsupported-file error when
  there are no usable exports.
- **Speed**: `read_rich_csv` on the full 36 MB file must take well under 5 s
  (read once with `pandas.read_csv(header=None, dtype=str,
  keep_default_na=False)` or the csv module, then vectorise per quantity).
- **Hashes**: SHA-256 of every file read, in `sources`.

Tests (`tests/test_qtegra.py`, on the fixture only): each reader's counts
(12 acquisitions, 9 channels, sweep counts, labels in order); Rich metadata
(user, start/stop text kept verbatim, parsed datetimes, masses, sequence
fields, spot number check, acquisition 1 tune settings empty in Rich);
spot-folder dwell/xcal/threshold/software/machine; `merge` equality, capabilities union,
`clock_offset_hours == 7.0`, tune filled for acquisition 1, mismatch raises
(perturb a copy in memory); the Y rule on Rich (y == counter where counter !=
-33; y == analog * xcal, xcal from the merged folder, where counter == -33,
`rtol=1e-9`); `recover_xcal` equals the folder xcal for 29Si, 91Zr and 177Hf
(`rtol=1e-9`); `open_qtegra` on each file, on the per-spot subfolder, on the
fixture root (Rich + subfolder -> merged); Y-only copies rejected or ignored
with a warning beside usable exports;
`detect_format`; empty sweep 72 handled.

## Milestone 3: `read_experiment_data` on `QtegraSession`

`read_experiment_data(source, ...)` keeps its signature and return value.
`source` may be a Rich CSV, a per-spot folder or file, a folder containing
these exports, a session bundle root, or an already-built `QtegraSession`.
The bundle/session-manifest logic stays as it is (a file source looks for a
manifest beside the file). The keyword-only `detector_calibration` argument
is described in milestone 4.

- Add `experiment_from_session(session, sep="_", sample_aliases=None) ->
  (experiment, element_channels, total_data)` before auto-selection, and have
  `read_experiment_data` call it. `total_data` columns: `Time` (session
  seconds, from each acquisition's `sweep_time`), one column per channel
  (Y), `spot_name`, `sample`, exactly as today. `experiment` columns as today;
  `spot_paths` = the per-spot file for folder sessions and the single CSV path
  for Rich sessions.
- **The per-spot path must give identical numbers.** Parse per-spot values
  with the same parser as today (pandas `read_csv` + `to_numeric`), so that
  frames are bit-identical. Prove it with a test that keeps a frozen copy of
  today's implementation (in the test module) and compares with
  `pandas.testing.assert_frame_equal(check_exact=True)` on the fixture's
  per-spot folder and on a synthetic folder from the existing tests.
- From Rich: the same channel arrays to the 15th significant digit (found
  in milestone 3: Rich prints Y with 17 significant digits, a round-trip
  double, while the per-spot files print 15, e.g. `449124.38355127671` vs
  `449124.383551277`; Rich is the more precise record), the
  same within-acquisition `Time`; session start offsets differ by the
  sub-second part only (per-spot line-1 times are truncated to whole seconds,
  Rich keeps 7 digits; the 7 h clock offset cancels because times are
  relative to the first acquisition). Test: Y columns identical, per-spot
  relative times identical, session starts within 1 s.
- `experiment.attrs["qtegra"]` = `{"format", "capabilities" (sorted list),
  "sources" [{path, sha256, format}], "times_reconstructed",
  "nominal_cycle_time_s", "clock_offset_hours", "software", "machine",
  "warnings"}`. Only JSON-safe values.
- Timing provenance keys remain `times_reconstructed=False` and
  `nominal_cycle_time_s=None`.
- `GUI/services.raw_input_files(path)` must return the single CSV for a file
  source and, for a folder, the CSVs the session was actually built from
  (use the `sources` of the session). Keep the GUI change to that function
  in this milestone.

## Milestone 4: detector cross-calibration (`UPbRPy/calibration.py`)

```python
@dataclass
class CrossCalibration:
    mode: str                      # "qtegra" | "constant" | "drift"
    on: str                        # "all" | "primary_standard"
    window: tuple[float, float]    # counter cps window, default (1e5, 2.4e6)
    channels: dict[str, ChannelCrossCalibration]
    def to_dict(self) -> dict      # JSON-safe, for reports and manifests

@dataclass
class ChannelCrossCalibration:
    channel: str
    qtegra_factor: float | None    # exported xcal (folder) or recovered from saturated sweeps
    factor: float                  # session constant (constant), or time-weighted mean (drift)
    factor_se: float
    intercept: float
    intercept_se: float
    n_points: int
    n_acquisitions: int
    times_s: np.ndarray | None     # drift: acquisition mid-times, session seconds
    factors: np.ndarray | None     # drift: smoothed factor at times_s
    factors_se: np.ndarray | None
    raw_factors: np.ndarray | None # drift: per-acquisition slope before smoothing
    def factor_at(self, t_s) -> np.ndarray   # constant or interpolated drift

def fit_cross_calibration(session, channel=None, window=(1e5, 2.4e6),
                          on="all", mode="constant", *, primary_standard=None,
                          sep="_", smoothing_s=1800.0) -> CrossCalibration
def apply_cross_calibration(session, calibration) -> QtegraSession
```

- Requires `analog_counter` (else `ValueError` naming the format). `channel`
  is a name, a list, or `None` = every channel that has points in the window.
- Points: sweeps where counter != -33, counter finite, `window[0] <= counter
  <= window[1]`, analog finite and > 0. Fit `counter = factor * analog +
  intercept` by ordinary least squares; standard errors from the residual
  variance and `(X^T X)^-1`.
- `on="primary_standard"` keeps acquisitions whose sample (label split at
  `sep`, first part, case-insensitive) equals `primary_standard` (required
  then).
- `mode="constant"`: one fit on all points. `mode="drift"`: one slope per
  acquisition (acquisitions with fewer than 5 points skipped; intercept fixed
  at the constant fit's intercept), then a Gaussian-kernel weighted average
  along acquisition mid-time with bandwidth `smoothing_s`, weights 1/se^2;
  `factor_at` interpolates linearly inside and holds the end values outside.
  `mode="qtegra"`: no fit; factors are the Qtegra ones (`apply` is then a
  no-op).
- `apply_cross_calibration` returns a new session (inputs untouched) whose `y`
  is `counter` where counter != -33 and `analog * factor_at(t)` where it is
  -33 (t = the sweep's session time), for the calibrated channels only;
  other channels unchanged.
- `read_experiment_data(..., detector_calibration=None)`: `None` or
  `"qtegra"` = today; `"constant"`, `"drift"`, or a mapping
  `{"mode", "on", "window", "primary_standard", "channels", "smoothing_s"}`
  fits on the session and applies before building the experiment.
  `experiment.attrs["detector_calibration"] = calibration.to_dict()`.
- `PipelineConfig.detector_calibration: dict = {}` (the mapping above, or
  `{"mode": "qtegra"}`), round-tripped by `to_dict`/`pipeline_config_from_dict`;
  `ReductionPipeline.manifest()` adds a top-level `"detector_calibration"`
  (the `to_dict()` of the applied calibration, from `experiment.attrs`, or
  `{"mode": "qtegra"}`), and the publication-table metadata gets the mode and
  the factors per channel. Default results stay identical to today.
- Tests against an independent least-squares fit written in the test
  (`numpy.polyfit` or normal equations) on the fixture: constant factor and
  intercept per channel, SEs, `on="primary_standard"` subset, drift output
  shapes and that a session with one acquisition gives the constant result,
  `apply` changes only saturated cells and only calibrated channels, `qtegra`
  mode changes nothing, manifest and config round trip.

## Milestone 5: Studio

- **File > Open session...** accepts a folder (existing directory dialog) and
  a new **File > Open Qtegra file...** (CSV file dialog, filter
  `Qtegra exports (*.csv)`); drag and drop of a file or folder if Studio
  already supports drop. `session.directory` may now be a file path; audit
  every use of `session.directory` (project save/restore, recent items,
  window titles, `raw_input_files`, `infer_workflow_preset`, bundle origin).
- A **capabilities badge** in the session header/status area: the format
  (`Rich`, `Per-spot folder`, `Rich + per-spot`) and, in its tooltip,
  the capability list and warnings.
- A small **Detector cross-calibration** panel (in the import/selection
  stage controls, or a dock that fits): mode picker (Qtegra factor /
  Session constant / Time drift), basis picker (All acquisitions / Primary
  standard), channel picker, a pyqtgraph plot of factor versus session time
  (per-acquisition raw slopes as points, smoothed curve as a line, Qtegra
  factor as a dashed horizontal line), and a table row of factor +- SE per
  channel. Disabled with a one-line reason when the session lacks
  `analog_counter`. Changing the mode re-imports through
  `read_experiment_data(..., detector_calibration=...)` and invalidates the
  reduction cache like other import settings; the choice is saved in the
  `.upbr` import settings.
- Light theme, existing widgets and spacing; the window must still fit
  1280x800 (`tests/test_small_screens.py` style tests extended).
- Offscreen tests: open each fixture format through the service layer and
  the main window, badge text, panel enabled/disabled, mode change updates
  `import_settings`.

## Milestone 6: docs

README and `docs/lab_workflow_integration.md`: formats accepted, which is
canonical, what each lacks, how the cross-calibration modes work, the public
API of `UPbRPy.qtegra` (for UPbDB), and how to regenerate the fixture.
