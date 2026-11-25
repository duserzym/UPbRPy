---
icon: material/sitemap-outline
---

# Acquisition model

!!! abstract "In short"
    After reading a file, UPbRPy holds the data in a few Python objects: a
    **session** containing **acquisitions** (one per laser spot), each with its
    **channels** (isotopes) and a signal value for every sweep. This page lists
    those objects field by field, the tables built from them for reduction, and
    how the dwell time and detector calibration are obtained.

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

Session readers skip acquisitions whose Y values are all missing, including
metadata-only Rich columns and per-spot files with no sweep rows. Each skip
adds `Skipped blank analysis '<label>' (no sweep rows)` to session `warnings`.
Source files and original sequence indices are retained. Studio displays
these warnings in the capabilities tooltip; experiment attrs and export
context retain them in `qtegra.warnings`, and reduction manifests retain them
in `metadata.qtegra.warnings`. A session with only blank analyses is empty;
experiment import rejects it. The standalone `read_spot_file` still returns
an Acquisition with zero sweeps for a blank file.

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

`experiment_from_session(session, sep="_", sample_aliases=None, acquisition_samples=None)` returns
tables before selection; `read_experiment_data` then initializes the
baseline, plateau and signal windows. Samples are the first part of the
label split at `sep`, with exact case-insensitive aliases applied afterward.
The optional `acquisition_samples` mapping supplies per-label sample names
from a session manifest using the [override rule](project-and-bundle.md#session-bundle).
`UPbRPy.io.sample_from_label(label, sep="_", aliases=None)` exposes the same
prefix and case-insensitive alias rule used at import. The resolved rule is
retained in `experiment.attrs["sample_label_rule"]` (`sep`, `aliases`).
Actual corrections are recorded in `experiment.attrs["sample_overrides"]`;
this mapping is always present, empty when none apply. Table columns do not change.
Spot numbers always use a trailing underscore integer, independently of `sep`.

| Table / field | Type / unit | Meaning |
| --- | --- | --- |
| `experiment.spot_names` | Text | One row per acquisition, sorted by label |
| `experiment.sample` | Text | Split and aliased sample name, or per-acquisition override |
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
`software`, `machine`, `acquisition_summary`, and `warnings`. It does not include every session
field: threshold and unrounded clock offset remain on QtegraSession.

### Optional laser guidance

When a resolved version-2 manifest provides usable laser times, import adds
these informational columns. A version-1 bundle, plain Qtegra input or log
with settings only adds no columns or laser attrs. Default selections and
reduction numbers remain unchanged.

| Experiment column | Type / unit | Meaning |
| --- | --- | --- |
| `laser_on_time`, `laser_off_time` | Float, session s | Firing endpoints shifted by the log clock offset; missing endpoints are NaN |
| `laser_duration_s` | Float, s | Off minus on; NaN without both endpoints |
| `laser_scan_position` | Numeric | One-based position; NaN when missing |
| `laser_detected_onset_time` | Float, session s | Independently detected signal start; NaN when detection fails |
| `laser_onset_residual_s` | Float, s | Detected onset minus laser-on minus session transport delay; NaN without a delay or pair |
| `laser_onset_flag` | Boolean | Absolute residual strictly above the QC threshold; false for missing residuals |

Relative firing times use the manifest acquisition's `started_at`, because
UPbDB estimated the clock correction against that instant. They are added
to the reader's existing `spot_starting_times`, which places acquisitions
and sweeps on the session axis. CSV starts can be rounded or use another
clock convention; they do not replace `started_at` in the offset arithmetic.
The session axis itself is unchanged, including reconstructed starts for
Rich files without StartTime. Stage coordinates are not added to experiment.

`experiment.attrs["laser"]` is a small JSON-safe object with
`transport_delay_s` (median detected onset minus laser-on; null with fewer
than three successful pairs), `n_pairs`, `qc_threshold_s` (default 2.0),
`log_sha256` (list), `source` (`manifest` or `lase`), and `spot_key` (comma
separated when logs use different keys). When `laser_guided` is configured
as the primary method or a fallback,
`reference_method` names the first non-laser configured fallback, or
`threshold` when there is none. This detector supplies both the transport
delay and the QC onset independently of the guided edges. A failed reference
pick supplies no pair, even if the guided pick succeeds.

Applying or replacing laser timing in Studio recomputes these independent
onsets from the loaded sweep data and import settings for every spot, including
reviewed spots. Saved windows, manual flags and cached reduction results are
preserved. Restoring a project with a saved laser tie also detects onsets for
every spot before overlaying its saved selections.

`UPbRPy.io.update_laser_detected_onsets(experiment, total_data, primary_channel,
*, selection_method="threshold", selection_fallback_methods=(),
**selection_kwargs)` performs this reference pass. It accepts the selection
parameters of `auto_select_baseline_peak` and updates only
`laser_detected_onset_time` and laser reference-method metadata. It uses the
active data detector, or, when Laser-guided is active, the first non-laser
fallback (threshold if none). Call `update_laser_qc` afterward to refresh QC.

`read_experiment_data` and `auto_select_baseline_peak` accept
`laser_qc_threshold_s=2.0` and `signal_duration_limit_source="fixed"`.
The latter also accepts `"laser"`: use each finite positive firing duration,
falling back to `signal_duration_limit_seconds` (possibly None). The existing
`selection_signal_duration_limit_seconds` records the limit actually used.
The `laser_guided` method is opt-in and labelled “Laser-guided”. It uses the
predicted on/off sweeps after adding session delay; unknown delay uses zero
and sets `laser_delay_assumed_zero=1` in selection diagnostics. Baseline and
plateau offsets and width modes follow the ordinary selection rules, with
the plateau capped at predicted off. Missing off uses ordinary washout
detection; missing on raises `SelectionError("no laser time for this spot")`
and allows configured fallbacks. QC columns never control reduction.

`available_selection_methods(*, include_laser=False)` preserves the existing
detector list by default. Controls for a session with usable firing times
can pass `include_laser=True` to include the registered `laser_guided` method.
The registered low-level detector is
`UPbRPy.io.laser_guided_onset(values, *, time=None,
laser_predicted_on_time=None, **options)`, returning an index and diagnostics;
normally callers use `select_signal_regions` or `autopick_signal_regions`.

The source-independent engine API in `UPbRPy.laser_guidance` is:

```python
add_laser_timing(experiment, windows, acquisition_starts, *,
                 scan_positions=None, source="manifest", log_sha256=(), spot_key="")
estimate_transport_delay(experiment) -> float | None
update_laser_qc(experiment, *, threshold_s=2.0)
laser_selection_options(spot, transport_delay_s=None, *,
                        signal_duration_limit_source="fixed",
                        signal_duration_limit_seconds=None) -> dict
```

`windows` maps labels to `(on_s_after_start, off_s_after_start)` with None
for missing endpoints. `acquisition_starts` maps labels to starts in session
seconds. For a single-spot re-pick, pass an experiment row and
`experiment.attrs["laser"]["transport_delay_s"]` to
`laser_selection_options`, then expand the returned keywords into
`autopick_signal_regions` with that spot's session-time trace. This helper
does not re-estimate the session delay. Batch selection computes reference
onsets before guided picking; subsequent manual edits and restoring saved
selections retain their ordinary behavior.

## Counting dwell

!!! warning "Multiply the rate by the dwell time before counting statistics"
    A reading of 10,000 cps on a channel with a 0.075 s dwell is 750 ions,
    not 10,000. Its counting uncertainty is √750 ≈ 3.65 %, not
    √10,000 = 1 %. UPbRPy before 2026-11-11 used the rate as the number of
    ions, which made every sweep uncertainty 1 / √(dwell) too small.
    On an apatite session this was 3.65× for 207Pb/206Pb.
    [Plain-language explanation and before/after numbers](../guide/counting-and-dwell.md).

Y is a count rate (cps). Poisson uncertainties need integrated counts,
`counts = Y * dwell`, so the Poisson variance of one reading is `Y / dwell`
in cps squared. `UPbRPy.qtegra.counting_dwell(session)` returns, per
channel, `(dwell_s, source)`; `experiment_from_session` stores the two parts
as `experiment.attrs["counting_dwell"]["dwell_s"]` and `["source"]`.

| Source | Used when | Value |
| --- | --- | --- |
| `recorded` | Any acquisition records a dwell (per-spot or merged input) | Median recorded dwell, s |
| `count_quantization` | No recorded dwell (Rich input); low pulse-counting values are quantized | `1 / step`, where `step` is the cps value of one count, recovered from the gaps between the lowest distinct Y values of all acquisitions and checked to make at least 98 % of values up to 20 steps integral within 0.05 counts |
| `channel_timing` | Y is not quantized (analog-only channels) and Time has one column per channel | Median time from this channel's reading to the next; includes settling time, so it is an upper bound on the dwell |
| `None` | None of the above | `None`; consumers then treat Y as counts |

On the zircon fixture, `count_quantization` on the Rich file reproduces the
recorded per-spot dwell of every quantized channel to 0.1 %.
`calc_raw_ratios` and `calc_concentration_depth_profile` take a `dwell_s`
mapping; the pipeline passes `counting_dwell["dwell_s"]`. A channel without a dwell is
treated as integrated counts (dwell 1 s), which understates Poisson errors by
`1 / sqrt(dwell)` for real cps data.

## Acquisition summary

`experiment.attrs["qtegra"]["acquisition_summary"]` is a small JSON-safe
mapping for export footnotes. It summarizes the original acquisitions before
selection, without per-acquisition lists. Missing scalars are `None`, missing
sets are empty lists. Timestamps retain the recorded clock; no timezone is
inferred and merged sessions keep Rich starts, not companion-clock starts.
`UPbRPy.io.acquisition_summary(session: QtegraSession) -> dict` also returns
this mapping without constructing experiment or sweep tables.

| Name | Type / unit | Meaning |
| --- | --- | --- |
| first_start | Text or `None` | Earliest recorded start in ISO form; original Rich text retains all fractional digits; per-spot start is converted to ISO |
| first_start_text | Text or `None` | Original export text of the earliest start |
| first_start_date | Text or `None` | Earliest start date, `YYYY-MM-DD` |
| last_start | Text or `None` | Latest recorded start, with the same ISO convention |
| last_start_text | Text or `None` | Original export text of the latest start |
| last_start_date | Text or `None` | Latest start date, `YYYY-MM-DD` |
| last_stop | Text or `None` | Latest recorded stop, with the same ISO convention |
| last_stop_text | Text or `None` | Original export text of the latest stop |
| last_stop_date | Text or `None` | Latest stop date, `YYYY-MM-DD` |
| users | List of text | Sorted unique recorded user names |
| machine | Text or `None` | Session instrument name |
| tune_names | List of text | Sorted unique recorded tune names |
| dwell_s | Mapping of channel to float or `None`, s | Median finite recorded dwell over acquisitions, in instrument channel order; `None` when never recorded |
| sweeps_per_acquisition | Mapping | `median` (float), `min` and `max` (integers) of acquisition sweep counts, or `None` for empty sessions |
| laser_output_settings_percent | List of text, % | Sorted unique nonempty `Laser Output Setting [%]` sample-list values, retaining exported text |

Projects (`project.py`) store source paths and reviewed selections, rather
than serializing experiment attrs. Re-import recreates this summary from the
source data, and `restore_project_selections` preserves those imported attrs.
Session bundles (`session_bundle.py`) unpack raw exports and an optional
project; importing those exports recreates the summary in the same way.
The acquisition summary is recreated from raw data; it is not an editable project field.
Studio session edits are persisted explicitly as described in [Projects and bundles](project-and-bundle.md#studio-spot-edits).

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

New import picks and re-picks use `UPbRPy.io.autopick_signal_regions(data,
**options)`. Plateau indices `[p, q)` have an exclusive end; the signal end
`e` is an inclusive measured sweep. Automatic picks retain the detector's
plateau and expand the signal to `s = max(0, min(detected_s, p - 1))` and
`e = min(n - 1, max(detected_e, q + 1))`, where `n` is this acquisition's
sweep count. Thus the visible end handles have one sweep of separation when
available, including after a nominal duration cap. The statistical detector
`select_signal_regions` remains available with its original boundaries and
diagnostics. Saved and restored windows are used exactly as recorded.
