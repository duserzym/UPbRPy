# Acquisition model

Implementation: [qtegra.py](https://github.com/duserzym/UPbRPy/blob/main/UPbRPy/qtegra.py),
[io.py](https://github.com/duserzym/UPbRPy/blob/main/UPbRPy/io.py) and
[calibration.py](https://github.com/duserzym/UPbRPy/blob/main/UPbRPy/calibration.py).

The public dataclasses in `UPbRPy.qtegra` carry arrays and provenance without
Qt, HTTP or database dependencies. Channel and SourceFile are frozen;
Acquisition and QtegraSession are mutable. `None` means unavailable, empty
dictionaries mean no entries, and numeric array NaN means missing.

In the tables, R = Rich, P = per-spot, M = merged. Array shape `(s, c)` means
sweeps by channels. Fields with default `None` or empty containers are
optional metadata. Required constructor fields are identified explicitly.

## QtegraSession fields

| Name | Type | Unit / definition | Filled by / example | Default or absence |
| --- | --- | --- | --- | --- |
| acquisitions | `list[Acquisition]` | Ordered acquisitions | R column order; P start then filename; M Rich order | Required constructor field |
| format | `str` | Reader format | `rich`, `spot_folder`, `rich+spot_folder` | Required |
| capabilities | `frozenset[str]` | Exported groups / format declarations | See capability table | Required |
| sources | `tuple[SourceFile, ...]` | Files actually read, with byte hashes | R single CSV; P each file; M both | Required |
| software | `str` or `None` | Software name and version | P/M: `Qtegra 2.10.4345.64` | `None` for R |
| machine | `str` or `None` | Instrument name | P/M: `iCAP RQ` | `None` for R |
| pulse_counting_threshold | `float` or `None` | cps; exported detector threshold | P/M: `2500000` | `None` for R; does not change the fit window automatically |
| clock_offset_hours | `float` or `None` | h; rounded median companion minus Rich start | M: `7.0` | `None` unless merged offsets available |
| clock_offset_seconds_median | `float` or `None` | s; unrounded median clock difference | M, computed from paired starts | `None` otherwise |
| times_reconstructed | `bool` | Whether nominal sweep offsets replaced missing Time | R/M without Time: `True` | `False` |
| nominal_cycle_time_s | `float` or `None` | s; reconstruction spacing | R/M without Time: `0.4255` by default | `None` with recorded time |
| warnings | `list[str]` | Nonfatal import/merge/calibration messages | All formats, possibly empty | Empty list |

Properties: `channel_names` uses the first acquisition; `labels` returns
ordered labels. `acquisition(label)` finds the first matching acquisition or
raises `KeyError`; `has(capability)` checks membership. These methods do not
validate the entire session.

## Acquisition fields

| Name | Type | Unit / definition | Filled by | Default or absence |
| --- | --- | --- | --- | --- |
| label | `str` | Acquisition name, e.g. `NIST612_896` | R header 2; P line 1 | Required |
| sequence_index | `int` | 1-based reader order; standalone `read_spot_file` gives 0 | R/P/M | Required |
| channels | `tuple[Channel, ...]` | Instrument order | All | Required |
| y | `np.ndarray` float `(s, c)` | cps; reduction signal | All | Required; NaN for missing cells |
| time | `np.ndarray` or `None` | s from acquisition start; R recorded `(s, c)`, P/reconstructed R `(s,)` | All readers | Required constructor argument; experiment import rejects `None` |
| analog | `np.ndarray` float `(s, c)` or `None` | Analog reading, cps on instrument scale | R/M with both detector groups | `None` |
| counter | `np.ndarray` float `(s, c)` or `None` | cps; saturated sentinel `COUNTER_SATURATED=-33.0` retained | R/M with both detector groups | `None` |
| start_text | `str` or `None` | Original timestamp text, without per-spot label/semicolon | R StartTime; P line 1 | `None` |
| start | `datetime` or `None` | Parsed start; fixture clocks are naive | All when supplied; Rich fractions truncated after 6 digits | `None`; experiment synthesizes an axis, merge/drift require starts |
| stop_text | `str` or `None` | Original stop timestamp text | R/M StopTime | `None` |
| stop | `datetime` or `None` | Parsed stop | R/M StopTime | `None` |
| tune_name | `str` or `None` | Tune prefix, e.g. `Laser_STDS` | R TuneSettings; P line 4; M fill if needed | `None` |
| tune_settings | `dict[str, float or str]` | Setting names to numeric/text values; units not normalized | R/P/M | Empty dictionary |
| readbacks | `dict[str, float or str]` | Flattened readback names to values | R/P/M | Empty dictionary |
| sequence_fields | `dict[str, str]` | Original sample-list values and companion line 1 | R BrigidMS/PMIAnalyteLaser; P/M `spot_file_start_text` | Empty dictionary |
| user | `str` or `None` | Recorded operator account | R/M Info UserName | `None` |
| spot_number | `int` or `None` | Trailing `_` integer; explicit laser number overrides with warning on conflict | All labels; R laser field | `None` |
| source_path | `Path` or `None` | Source CSV; M retains Rich path | All readers | `None` for caller-built objects if omitted |

Properties: `channel_names`, `n_sweeps`, `time_per_channel` and `sweep_time`
(first channel if 2-D). `channel_index(name)` raises `ValueError` if absent.
The private `_data_dtypes` field preserves historical pandas dtypes and is
not part of this public field contract.

## Channel fields

| Name | Type | Unit / definition | Filled by / example | Default or absence |
| --- | --- | --- | --- | --- |
| name | `str` | Exported isotope/channel name | All: `29Si` | Required |
| mass | `float` or `None` | u; first finite X value | R/M: `28.97599983215332` | `None` for P or missing X |
| dwell_s | `float` or `None` | s; dwell per channel | P/M: `0.005` | `None` for R |
| xcal | `float` or `None` | Exported analog multiplier | P/M: `65695.80659` | `None` for R; recovery is a separate function |

## SourceFile fields

| Name | Type | Unit / definition | Filled by | Default or absence |
| --- | --- | --- | --- | --- |
| path | `Path` | Source filename/path as read | R/P/M | Required |
| sha256 | `str` | Hex SHA-256 of original bytes, including BOM/line endings | All session readers | Required |
| format | `str` | Per-file `rich` or `spot` | All session readers | Required |

## Capabilities

`CAPABILITIES` is the following frozenset. Optional Rich group presence sets
flags at session level; it does not assert that every cell is populated.
P has a fixed set even with empty optional metadata. M takes the union.

| Capability | Meaning | Inputs that give it | Features using it / behavior without it |
| --- | --- | --- | --- |
| timestamps | StartTime row or per-spot format | R StartTime; all P | Recorded calendar gaps; merge and detector drift also check actual starts |
| stop_times | StopTime row | R/M | End metadata only; no requirement for reduction or spacing reconstruction |
| per_channel_time | Recorded channel-specific Time quantity | R/M Time | Channel-specific detector drift times; experiment sweep axis uses first channel; P/reconstruction uses shared time |
| analog_counter | Both Analog and Counter quantities | R/M with both | Required for fitted detector cross-calibration; default Qtegra Y needs neither |
| mass | X [u] quantity | R/M X | Channel mass metadata; reduction can use channel names without it |
| dwell_xcal | Per-spot format's dwell/xcal metadata | All P; M union | Exported detector factor available when populated; Rich can recover factors from saturated sweeps separately |
| instrument_state | TuneSettings or StatusReport, or per-spot format | R with either group; all P | Instrument-state inspection; absent state does not block reduction |
| sequence_fields | BrigidMS or PMIAnalyteLaser group | R/M with either | Sample-list metadata/explicit laser spot number; no automatic standard or dilution choice |
| user | Info UserName row | R/M | Operator metadata; not required for reduction |

## Experiment tables

`experiment_from_session(session, sep="_", sample_aliases=None)` returns
tables before selection; `read_experiment_data` then initializes the
baseline, plateau and signal windows. Samples are the first part of the
label split at `sep`, with exact case-insensitive aliases applied afterward.
Spot numbers always use a trailing underscore integer, independently of `sep`.

| Table / field | Type / unit | Meaning |
| --- | --- | --- |
| `experiment.spot_names` | Text | One row per acquisition, sorted by label |
| `experiment.sample` | Text | Split and aliased sample name |
| `experiment.spot_paths` | Text or `None` | Original CSV path; merged acquisitions point to Rich |
| `experiment.spot_starting_times` | Float, s | Start relative to earliest experiment start |
| `baseline_start_times`, `baseline_end_times`, `plateau_start_times`, `plateau_end_times`, `signal_start_times`, `signal_end_times` | Session s; initially `None` | Reviewed/automatic selection windows |
| `element_channels` | List of text | First acquisition's channel names |
| `total_data.Time` | Float, s | Session axis using first-channel/shared offsets and starts |
| `total_data.<channel>` | Numeric, cps | Y; wholly missing channels may be omitted in individual frames |
| `total_data.spot_name`, `total_data.sample` | Text | Sweep identity; concatenated in session reader order |

`experiment.attrs["qtegra"]` contains JSON-safe `format`, sorted
`capabilities`, `sources` (objects with `path`, `sha256`, `format`),
`times_reconstructed`, `nominal_cycle_time_s`, `clock_offset_hours`,
`software`, `machine`, and `warnings`. It does not include every session
field: threshold and unrounded clock offset remain on QtegraSession.

## Detector calibration

`read_experiment_data(..., detector_calibration=None)` or `"qtegra"` keeps
exported Y. `"constant"`, `"drift"`, or a mapping with `mode`, `on`, `window`,
`primary_standard`, `channels`, `smoothing_s` fits before table construction.
Fitting constant/drift requires `analog_counter`. Drift also requires
parsed starts for every acquisition; continuous experiment timing does not
supply those starts to the calibration code.

Fits use finite counter values other than -33 in the inclusive default
`(1e5, 2.4e6)` cps window and finite positive analog values. Constant mode
fits `counter = factor * analog + intercept` by OLS with at least three
points and full rank. Drift holds that intercept fixed, fits acquisitions
with at least five points, and smooths slopes with a Gaussian kernel and
inverse-variance weights (`smoothing_s=1800.0` s). It interpolates between
smoothed values and holds endpoint values outside their time range.
`on="primary_standard"` selects the split label prefix case-insensitively;
this fitting filter does not apply sample aliases.

Applying calibration changes only saturated cells with finite analog in
calibrated channels: `Y = analog * factor_at(time)`. The intercept is
reported but not applied; other Y cells stay unchanged. Inputs are copied.
`recover_xcal(session_or_acquisition)` separately returns median Y/Analog on
finite saturated sweeps with nonzero analog; it does not set Channel.xcal
or add a capability. Exported xcal takes precedence for calibration reports.

The applied record is `experiment.attrs["detector_calibration"]`. Default
import records `{"mode": "qtegra"}`. Fitted records include mode, basis,
window, primary standard, smoothing, warnings, and per-channel factors,
one-standard-error estimates, intercepts, point/acquisition counts, relative
difference from Qtegra and acquisition labels; drift adds times and slope
arrays. Nonfinite numeric fit diagnostics serialize as `None`.
