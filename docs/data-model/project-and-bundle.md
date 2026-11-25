---
icon: material/package-variant-closed
---

# Projects and bundles

!!! abstract "In short"
    Three kinds of saved output exist. A **.upbr project** stores your settings
    and review choices so you can reopen a reduction. A **session bundle** keeps
    the raw instrument files together with sample names. A **reduction bundle**
    holds the results, their provenance and a notebook that repeats the
    reduction.

Implementation: [project.py](https://github.com/duserzym/UPbRPy/blob/main/UPbRPy/project.py),
[session_manifest.py](https://github.com/duserzym/UPbRPy/blob/main/UPbRPy/session_manifest.py),
[session_bundle.py](https://github.com/duserzym/UPbRPy/blob/main/UPbRPy/session_bundle.py),
[pipeline.py](https://github.com/duserzym/UPbRPy/blob/main/UPbRPy/pipeline.py) and
[reproducibility.py](https://github.com/duserzym/UPbRPy/blob/main/UPbRPy/reproducibility.py).

A `.upbr` project is UTF-8 JSON containing paths, settings and reviewed
selections. A session bundle supplies original raw input. A reduction bundle
supplies output tables, provenance and a replay notebook. These are distinct
formats; `export_reduction_bundle` does not package raw CSVs or a `.upbr`.

## .upbr project

`build_reduction_project` builds the document;
`write_reduction_project` writes it atomically and supplies a `.upbr` suffix.
`read_reduction_project` reads and validates it. Writers emit version **2**.
Version **1** loads with the same core structure; version 2 adds optional
`origin`. Versions below 1 or above 2 are rejected. An origin, if present,
is validated even in a version-1 document.

Required below describes the project validator and subsequent path use.
Writers emit all listed core keys except origin. Unknown project keys are
not rejected. Raw sweeps and computed tables are not embedded. Reopening uses
validated local results when available, otherwise re-imports dependencies and
recalculates the saved completed stages.

New saves record `saved_by` at the project root (`app`, package `version`,
and source `commit`, or null when unavailable) and `pipeline.settings_schema`
(currently **1**). Manifests also record top-level `settings_schema`, alongside
`upbrpy_version` and `upbrpy_source`. These keys are outside the scientific
configuration and leave both format versions unchanged. Older files without
them still load with defaults. Unknown setting keys, including nested settings,
a newer settings schema, or a newer project format refuse opening with a plain
message asking the user to update Studio; settings are never discarded to make
a file load. Bump `SETTINGS_SCHEMA_VERSION` whenever a saved setting is added
or changes meaning in `PipelineConfig` or its nested settings dataclasses.

### Standalone laser log

An optional top-level `laser_log` object stores the complete
`UPbRPy.laser_tie.LaserTie.to_dict()` only when an analyst has loaded a `.lase`.
It includes file name, SHA-256 of the original bytes, spot key, threshold,
clock evidence, warnings and per-label matches with relative laser times,
settings and stage coordinates. It is separate from pipeline configuration
and reduction manifests. Projects without a loaded log omit this object and
retain their existing serialization.

Reopening applies the stored tie before selection, even if the original file
has moved. A different SHA-256 in a same-named file beside the session (or in
its parent) warns and keeps the saved tie, including when cached results load.
Removing the standalone log removes this object on the next save and restores
manifest laser timing when available. Stage coordinates remain project data;
they are absent from analytical columns and reduction exports.

### Studio spot edits

`spot_edits` stores `spot_renames` (current name to original acquisition label),
`sample_overrides` (current name to `label_sample`, `sample`, `source`),
`removed_spots` (original labels) and `calculation_excluded_spots` (current names).
These optional fields are additive to project version 2. Restore removes and
renames raw acquisitions before strictly matching saved selection records, then
applies materials to both experiment and sweep tables. A missing original
acquisition or a duplicate final name fails restore with a message.

Studio material corrections use `source="studio"`; `label_sample` is derived
from the original label with the imported separator and aliases. Returning to
that sample removes the override. Rename chains retain one original label;
renaming back removes the rename entry. A rename with the same canonical label
prefix preserves the current sample and override source; a changed prefix previews
and re-derives the material. `original_spot_label(experiment,
spot_name)` in `UPbRPy.spot_edits` resolves the acquisition identity.
`spot_edit_metadata(experiment)` extracts these decisions and
`restore_spot_edits(experiment, total_data, edits)` returns replayed frame copies.

The Spots-tree action "Leave out of calculations" records
`calculation_excluded_spots` in experiment attrs and filters acquisitions before
baseline subtraction, ratios and calibration availability checks. It leaves raw
readings and selection windows accessible. Removal deletes working rows; both
operations are undoable and invalidate calculations from baseline. Final Review
exclusions remain separate: the spot stays reduced and its export row is retained
with `excluded=True` and a footnote, but fits and weighted means leave it out.
`ReductionPipeline.update_inputs(experiment,
total_data, *, invalidate_from=PipelineStage.BASELINE)` synchronizes both tables
after edits. Material changes retain baseline values but refresh their sample
labels and invalidate normalization (which uses the material's internal reference
concentration), ratios carrying sample labels, and dependent corrections.

Workspace keys `empty_spot_folders`, `spot_folder_overrides` and
`review_excluded_spots` preserve grouping and review exclusions. Empty
folders survive reopening; recognized material names belong under Reference
Materials and other names under Unknowns. Projects and autosave use the same
edit records. Raw source files remain untouched.
Database linking and acquisition navigation use original labels. Database
submission recalculates a private pipeline with original labels in result
tables; the submitted `.upbr` retains edited names for Studio. Regular exports
use edited names and include original labels in manifest provenance.

### Studio colours

Workspace keys `sample_colours` and `spot_colours` are optional mappings from
sample or current spot name to opaque `#rrggbb` strings. Restore ignores unknown
names and invalid colours. Missing mappings use the existing Studio palettes.
These keys affect display only: they are excluded from the scientific project
fingerprint and results-cache key, and changing them does not invalidate a
pipeline stage. They use the spot-edit undo history with `calculations=False`.
The engine does not interpret them. No project format version change is needed.

### Local results cache

Studio keeps disposable results in `~/Library/Caches/UPbRPy/results/` on macOS,
`${XDG_CACHE_HOME:-~/.cache}/UPbRPy/results/` on Linux, and
`%LOCALAPPDATA%\UPbRPy\results` on Windows. It verifies input contents,
reference files, settings and engine code before reuse. Display choices are
restored from the current project. Explicit Save prepares results after a short
idle delay; selection autosave does not. The `.upbr` format is unchanged.

Delete the contents of that directory to clear it. Set
`UPBRPY_RESULTS_CACHE=0` to disable caching, or `UPBRPY_RESULTS_CACHE_DIR` to
choose a different local cache directory. `UPBRPY_RESULTS_CACHE_MAX_MB` sets the
size limit (default 2048 MB); oldest-used entries are removed first. Keep this
private cache outside shared/synced folders: it contains Python pickles.
**File → Reopen and Recalculate** bypasses existing results and refreshes the
entry for the current project.


| Key | Required/optional | Type / unit | Example | Meaning / without it |
| --- | --- | --- | --- | --- |
| format | Required | String | `UPbRPy reduction project` | Exact marker; otherwise rejected |
| format_version | Required | Integer, converted with `int` | `2` | Supported version 1 or 2 |
| saved_by | Optional | Object | `{"app": "UPbRPy Studio", "version": "0.1.4", "commit": null}` | Saving application and source revision; absent in older files |
| pipeline.settings_schema | Optional | Integer | `1` | Saved settings revision; newer revisions refuse opening |
| saved_utc | Optional on read | ISO datetime string | `2026-11-05T12:00:00+00:00` | Writer time; not validated on read |
| paths | Required | Object | See children | Dependency paths; missing/non-object rejected |
| paths.source_directory | Required to reopen | Path record or string | `{"relative": "raw", "absolute": "/data/session/raw"}` | Source folder or CSV despite the key name; missing/unresolvable fails on restore |
| paths.reference_library | Optional | Path record, string or null | `null` | Alternate reference library; null uses packaged library; a recorded nonempty missing path fails resolution |
| paths.*.relative | Optional per path | String | `raw` | Tried relative to project parent first |
| paths.*.absolute | Optional per path | String | `/data/session/raw` | Original absolute fallback; writer records both where possible |
| pipeline | Required | Object | See children | Scientific settings and replay state |
| pipeline.config | Required | Object | See configuration tables | Missing rejected; empty object can reconstruct defaults |
| pipeline.completed_stages | Optional | Array of stage strings | `["import", "selection"]` | Default empty; identifiers validated, deduplicated and ordered by pipeline stage order |
| pipeline.resume_stage | Optional | Stage string | `selection` | Default `import`; invalid identifier rejected |
| spot_edits | Optional | Object | `{}` | Studio identity, sample, removal and exclusion decisions; absent preserves imported identities |
| selections | Required | Array of objects | One record per spot | Missing/non-array rejected; empty array accepted; row content restored separately |
| selections[].spot_names | Written when column exists | String | `NIST612_896` | Spot identity for selection restore |
| selections[].baseline_start_times | Written when column exists | Number/null, session s | `0.00732` | Reviewed baseline start |
| selections[].baseline_end_times | Written when column exists | Number/null, session s | `3.8` (illustrative) | Reviewed baseline end |
| selections[].plateau_start_times | Written when column exists | Number/null, session s | `6.0` (illustrative) | Reviewed plateau start |
| selections[].plateau_end_times | Written when column exists | Number/null, session s | `14.0` (illustrative) | Reviewed plateau end |
| selections[].signal_start_times | Written when column exists | Number/null, session s | `5.0` (illustrative) | Reviewed signal start |
| selections[].signal_end_times | Written when column exists | Number/null, session s | `20.0` (illustrative) | Reviewed inclusive signal end |
| selections[].selection_method | Written when column exists | String | `threshold` | Successful selection method |
| selections[].selection_channel | Written when column exists | String | `29Si` | Channel used for selection |
| selections[].plateau_filter_method | Written when column exists | String | `none` | Plateau filtering choice |
| selections[].plateau_filter_channel | Written when column exists | String | `29Si` | Filtering channel |
| selections[].selection_manual | Written by Studio | Boolean | `true` | Windows edited by hand (drag, keyboard nudge, undo/redo); a batch re-pick skips these unless told to overwrite them. Projects without it are inferred on opening: a spot whose saved windows differ from a fresh auto-pick under the saved settings counts as edited |
| selections[].selection_falling_edge_index | Written when column exists | Integer | `23` | Sweep index (within the spot) where the falling washout edge starts, or −1; Studio shows the median time from signal start to this sweep next to Nominal on-peak |
| selections[].selection_* | Optional additional columns | JSON-safe values | Selection diagnostics | All other experiment columns beginning `selection_` are also exported |
| workspace | Optional | Object | `{}` | Caller-defined GUI state; default empty; non-object rejected; engine imposes no child-key schema |
| origin | Optional | Object or null | See below | Database provenance; absent/null means no origin |

Stage identifiers are `import`, `selection`, `baseline`, `normalization`,
`concentration`, `ratios`, `downhole`, `session`, `spot_means`,
`depth_profile`, `finalize`, `export`. Paths are expanded/resolved on write;
restore prefers an existing relative path, then absolute fallback. A legacy
string path is expanded and resolved as given.

### Configuration keys

These are all keys emitted by `PipelineConfig.to_dict()`. They are optional
within a config object on reconstruction and otherwise use class defaults.
Unknown constructor keys fail. `selection_options`, model `options`,
`common_pb_options` and detector settings are extensible mappings whose
contents depend on the selected algorithm.

| Key within pipeline.config | Type / unit | Default / example | Meaning |
| --- | --- | --- | --- |
| workflow_key | String | `zircon` | Workflow preset identity |
| material | String | `zircon` | Material classification |
| primary_channel | String | `29Si` | Default signal selection channel |
| internal_standard_channel | String or null | `null` | Internal normalization element channel; null uses 29Si for zircon/titanite, 44Ca for apatite, 49Ti for rutile, independently of the picking channel |
| reference_overrides | Object of standard → channel → number, µg/g | `{}` | Project-local total-element concentrations, finite and greater than zero; applied to a private copy of the reference archive; never written to the shared library |
| selection_method | String | `threshold` | Selection algorithm, including opt-in `laser_guided` |
| selection_options | Object | `{}` | Selection/import options; Studio restores normalized import settings from here |
| detector_calibration | Object | `{}` | Mode/options for detector fitting; empty uses Qtegra Y |
| concentration_standards | Array of strings | `[]` | Concentration calibrators |
| primary_68_standard | String | Empty | Pb/U primary standard |
| primary_76_standard | String | Empty | Pb/Pb primary standard |
| primary_82_standard | String | Empty | Th/Pb primary standard |
| secondary_standards | Array of strings | `[]` | Validation materials |
| ratio_names | Nonempty array of strings | `206Pb/238U`, `207Pb/206Pb`, `208Pb/232Th` | Ratios requested |
| ratio_aggregation | String | `arithmetic` | Normalized ratio averaging convention |
| common_pb_calibration | String | `radiogenic` | `radiogenic` or `tera_wasserburg` |
| downhole | Fit-settings object | `mean`, outlier sigma `5.0` | Within-spot fractionation model |
| session | Fit-settings object | `polynomial`, options `{"degree": 2}` | Session drift model |
| downhole.method, session.method | String | Method name or importable callable reference | Fit implementation |
| downhole.options, session.options | Object | `{}` / `{"degree": 2}` | Algorithm kwargs |
| downhole.outlier_sigma, session.outlier_sigma | Number/null, sigma | `5.0` / `null` | Outlier threshold |
| decision | Object | See below | Preferred result choices |
| automatic_flags | Object | See below | Quality thresholds |
| common_pb_options | Object | `{}` | Common-Pb algorithm kwargs |
| uncertainty | Object | See below | Explicit uncertainty components |
| depth_calibration | Object or null | `null` | Optional linear pulse-to-depth calibration |

The new import settings `laser_qc_threshold_s` and
`signal_duration_limit_source` live in `PipelineConfig.selection_options`
alongside existing reader options (or its nested detector `selection_options`
mapping). Serialization omits values equal to their defaults,
`DEFAULT_LASER_QC_THRESHOLD_S` (3.0 s, from `UPbRPy.laser_guidance`) and
`"fixed"`, respectively. Projects without the threshold key load with 3.0 s;
an explicitly saved value, including 2.0 s, is retained. Existing default
configurations and reviewed selections serialize unchanged.
`UPbRPy.laser_guidance.nondefault_laser_settings(settings) -> dict` performs
this omission without mutating the supplied mapping.
Nondefault values and `selection_method="laser_guided"` round-trip normally.
`experiment.attrs["selection_parameters"]` also retains nondefault choices.
Laser timing and QC columns are not saved as reviewed selection decisions;
they are recomputed from input metadata on import. Restoring saved windows
does not re-pick them.

| Key within decision | Type / unit | Default | Meaning |
| --- | --- | --- | --- |
| common_pb_method | String | `none` | `none`, `204`, `207`, `208` |
| include_calibration_uncertainty | Bool | `true` | Choose calibration-inclusive uncertainty branch |
| age_transition_ma | Number, Ma | `1000.0` | Nonnegative transition from Pb/U to Pb/Pb preferred age |

| Key within automatic_flags | Type / unit | Default | Meaning |
| --- | --- | --- | --- |
| maximum_discordance_percent | Number, % | `20.0` | Discordance ceiling |
| maximum_u_mean_intercept_difference_percent | Number, % | `25.0` | U mean/intercept disagreement ceiling |
| maximum_ratio_rsd_percent | Number, % | `20.0` | Ratio scatter ceiling |
| maximum_primary_signal_rsd_percent | Number, % | `50.0` | Matrix-signal scatter ceiling |
| maximum_relative_2e_percent | Number, % | `20.0` | Relative uncertainty ceiling |
| minimum_radiogenic_206_fraction | Number, fraction | `0.05` | Radiogenic fraction floor |
| minimum_204pb_detection_sigma | Number, sigma | `3.0` | Mass-204 detection threshold |
| flag_session_extrapolation | Bool | `true` | Flag outside session calibration range |

| Key within uncertainty | Type / unit | Default | Meaning |
| --- | --- | --- | --- |
| propagate_baseline | Bool | `true` | Include baseline uncertainty in raw ratios |
| propagate_decay_constants | Bool | `false` | Include decay-constant uncertainty |
| propagate_u238_u235 | Bool | `false` | Include natural U ratio uncertainty |
| long_term_reproducibility_mode | String | `none` | `none`, `secondary`, `user` |
| long_term_reproducibility_relative_1s | Number, fraction | `0.0` | User external reproducibility, 1 sigma |
| u238_u235 | Number, ratio | `common_U238_U235` constant | Natural U ratio |
| u238_u235_1s | Number, ratio | `sigma_common_U238_U235` | Absolute 1-sigma ratio uncertainty |
| lambda_235_1s | Number, yr^-1 | `sigma_235` | Absolute 1-sigma decay uncertainty |
| lambda_238_1s | Number, yr^-1 | `sigma_238` | Absolute 1-sigma decay uncertainty |
| lambda_232_1s | Number, yr^-1 | `sigma_232` | Absolute 1-sigma decay uncertainty |

| Key within non-null depth_calibration | Type / unit | Default / requirement | Meaning |
| --- | --- | --- | --- |
| model | String | Writer adds `linear` | Only linear model supported |
| depth_per_pulse_um | Number, µm/pulse | Required, finite positive | Depth slope |
| repetition_rate_hz | Number, Hz | Required, finite positive | Laser pulse rate |
| intercept_um | Number, µm | `0.0` | Depth intercept |
| coefficient_covariance | 2×2 number array | Zeros, finite | Intercept/slope covariance |
| residual_1s_um | Number, µm | `0.0` | Residual depth scatter |
| repetition_rate_1s_hz | Number, Hz | `0.0` | Pulse-rate uncertainty |
| geometry_1s_um | Number, µm | `0.0` | Geometry uncertainty |
| pulse_range | Two numbers, pulses | `[0.0, Infinity]` | Ordered calibration range; Python JSON writer can emit Infinity |
| force_zero | Bool | `false` | Zero-intercept fit flag |

Detector options and the applied fit record are described under
[detector calibration](acquisition-model.md#detector-calibration). Nested
configuration validation occurs when rebuilding these settings, rather than
in the lightweight project structural validator.

### Studio workspace keys

The engine accepts any workspace mapping. Studio currently writes these
optional display keys; absence uses the current/default display state.

| Key within workspace | Type | Meaning |
| --- | --- | --- |
| selected_spot | String | Selected acquisition label |
| visible_channels | Array of strings | Checked trace channels |
| selected_stats_channel | String | Statistics panel channel |
| plot_mode | String | Trace display mode, normally `raw` |
| ratio_name | String | Selected ratio |
| workflow_stage | String/null | Active workflow stage |
| sigma_level | Integer | Display uncertainty multiplier |
| channel_colors | Object of strings | Channel-to-color mapping |
| selection_colors | Object of strings | Selection-region colors |
| spot_folder_overrides | Object of strings | Spot display-folder assignments |
| empty_spot_folders | Array of strings | Explicitly created empty Unknowns folders |
| review_excluded_spots | Array of strings | Review exclusions from fits and weighted means; exported rows stay flagged |
| include_export_concentrations | Bool | Include concentration columns in publication export |
| include_export_depth_concentrations | Bool | Include depth concentration columns |

### Origin block

| Key within origin | Required/optional | Type | Example | Meaning / without it |
| --- | --- | --- | --- | --- |
| server | Required when origin exists | Scalar converted to string | `https://lab.example.org` | Database base URL; trailing `/` removed |
| session_id | Required when origin exists | Scalar converted to string | Session UUID | Session identity; UUID syntax not validated |
| session_key | Optional | Scalar converted to string | `Zircon_04Nov26` | Human session key |
| export_sha256 | Optional | Scalar converted to string | Hex digest | Hash of `session_manifest.toml`, not the ZIP |
| exported_at | Optional | Scalar converted to string | ISO timestamp | Database export time; not parsed |
| run_id | Optional | Scalar converted to string | Run UUID | Submission run identity |
| Other keys | Optional | Scalar converted to string | Future server fields | Retained; mapping/list/tuple values rejected; empty/null fields dropped |

## Session bundle

The expected directory layout is:

```text
<session_key>/
  session_manifest.toml
  raw/                      original Qtegra exports
  <session_key>.upbr         optional pre-filled project
  README.md                 optional explanation
```

The manifest reader is permissive: the following tables/fields are optional
unless explicitly marked required. It ignores unknown metadata. Empty
manifest documents parse; ZIP unpacking separately requires the manifest and
`raw/` directory. Examples not from Qtegra itself are illustrative.

| Manifest key | Required/optional | Type / unit | Example | Meaning / without it |
| --- | --- | --- | --- | --- |
| format | Optional | String | `upbdb-session-bundle` | Also accepts `laicpmsdb-session-bundle`, written before the 2026-11-15 rename; other values rejected. Reading preserves the manifest file |
| format_version | Optional | Integer | `2` | Defaults to `1`; no maximum version check |
| session | Optional | Table | `[session]` | Defaults empty; if present must be a table |
| session.id | Optional | Scalar converted to text | Session UUID | Defaults empty; not used to gate raw parsing |
| session.key | Optional | Scalar converted to text | `Zircon_04Nov26` | Defaults empty; unpacker falls back to folder name |
| session.sep | Optional | Nonempty string | `_` | Label separator; absent uses explicit import argument or `_` |
| session.material | Optional | Text | `zircon` | Metadata; defaults empty |
| session.instrument | Optional | Text | `iCAP RQ` | Metadata; defaults empty |
| session.method | Optional | Text | `Zircon U-Pb` | Metadata; defaults empty |
| session.operator | Optional | Text | `Y. Zhang` | Metadata; defaults empty |
| samples | Optional | Table of alias = nonempty target string | `AFB = "AFB1"` | Case-insensitive sample aliases at import; invalid/blank entries skipped |
| reference_materials | Optional | Table | `primary_68 = "91500"` | Plan metadata; absent gives empty plan |
| reference_materials.<role> | Optional | String or array of strings | `concentration = ["NIST612"]` | Names stripped and deduplicated; numeric/bool values ignored |
| reference_materials.library_version | Optional | Ignored | `"format-2"` | No library version enforcement |
| reference_materials.plan | Optional alternative/addition | Array of tables | `[[reference_materials.plan]]` | Adds role/name pairs |
| reference_materials.plan[].role | Needed for row to contribute | Text | `primary_68` | Row ignored if empty |
| reference_materials.plan[].rm_name | Needed for row to contribute | Text | `91500` | Row ignored if empty |
| reference_materials.plan[].planned_bracket_every | Optional | Ignored | `10` | Accepted in plan row but not retained |
| acquisition | Optional | Array of tables | `[[acquisition]]` | Defaults empty; no raw file list restriction |
| acquisition[].label | Required for each listed row | Nonempty text | `NIST612_896` | Missing/blank row label rejected |
| acquisition[].acquisition_id | Optional | Text | Acquisition UUID | Defaults empty |
| acquisition[].spot_id | Optional | Text | Spot UUID | Defaults empty |
| acquisition[].atlas_spot_id | Optional | Text | GrainAtlas spot UUID | Defaults empty; old manifests remain valid |
| acquisition[].grain_id | Optional | Text | Grain UUID | Defaults empty |
| acquisition[].sample_id | Optional | Text | Sample UUID | Defaults empty |
| acquisition[].sample_label | Optional | Text | `SMP2` | Per-acquisition sample; absent/empty keeps the sample derived from the label |
| acquisition[].started_at | Optional | Text or TOML date/datetime | `2026-11-04T15:55:37` | Dates converted to ISO text; does not replace raw Qtegra start |
| acquisition[].sha256 | Optional | Text | Raw file SHA-256 | Unpacker verifies only if filename also nonempty |
| acquisition[].kind | Optional | Text | `primary_standard` | Metadata, not used to select reduction standard |
| acquisition[].filename | Optional | Text | `Zircon_demo_1.csv` | Relative to raw for hash checks; absent skips that check |

Version 2 adds optional top-level `[[laser_log]]` and `[[laser_setting]]`
tables, read as frozen `SessionLaserLog` and `SessionLaserSetting` objects in
`SessionManifest.laser_logs` and `.laser_settings`. There is no single
`[laser]` table. Each log stores `id`, `file_name`, `sha256`, `system_type`,
`serial_number`, `software_version`, `time_zone`, `laser_instrument`,
`spot_key` (`position` or `description`), `has_laser_times`, `clock_offset_s`,
`clock_offset_spread_s`, `offset_pairs` and `linked`. Each setting stores
`log_sha256`, `fluence_j_cm2`, `rep_rate_hz`, `spot_um`, `output_pct`,
`scan_speed_um_s`, `spot_shape`, `shot_count` and `acquisitions`.

Linked acquisition rows may carry `laser_log_sha256`, `laser_scan_position`
(one-based), `laser_on_at`, `laser_off_at`, `laser_fluence_j_cm2`,
`laser_rep_rate_hz`, `laser_spot_um`, `laser_spot_shape`, `laser_shot_count`,
`laser_output_pct`, `laser_stage_um` (three stage coordinates in µm) and
`laser_link_method` (`spot_number`, `time` or `manual`). These become one
optional `SessionAcquisition.laser: AcquisitionLaser`; without laser keys it
is `None`. Its fields omit the `laser_` prefix, except for `laser_on_at` and
`laser_off_at`; the log hash is `log_sha256`, and scan position is
`scan_position`. `duration_s` returns off minus on, or `None` when incomplete.
Stage coordinates are read for display/QC and excluded from exports unless
opted in; export support is a later stage.

Laser times accept TOML datetimes or ISO strings and are normalised to UTC;
naive values are treated as UTC. `SessionManifest.laser_log(sha256)` finds a
log, and `acquisition_laser_window(label)` returns `(on, off)` in seconds
after that acquisition's `started_at`: laser time plus the log's
`clock_offset_s`, minus acquisition start. The stored start text is parsed
with `datetime.fromisoformat`; a naive start is treated as UTC. Missing or
invalid start, missing on time, or missing log/offset gives `None`; a missing
off time gives `(on, None)`. Logs without firing times can still supply
settings and stage coordinates. `has_laser_data` is true when any log or
acquisition laser metadata exists.

Laser metadata is optional: invalid values or malformed laser table rows
are dropped with row/key context in `SessionManifest.warnings`. Hashes must
be 64 hexadecimal characters; numeric fields reject booleans and nonfinite
numbers. Missing strings default empty, optional numbers/times/stage default
`None`, `has_laser_times` defaults false, and `offset_pairs`, `linked` and
`acquisitions` default zero. Unknown keys and tables are ignored. Version-1
bundles retain their existing text values and reduction behaviour.

`SessionManifest.standards_config()` maps `primary`/`primary_68`,
`primary_76`, `primary_82` to the corresponding primary-standard config
fields; `concentration` to concentration standards and `secondary`/
`validation` to secondary standards. Scalar fields take the first name.
The raw importer uses separator, aliases and per-acquisition sample labels,
not the plan to configure a reduction or the acquisition rows to select files.

For each acquisition, import derives a sample from the label prefix split at
`sep`, then applies the case-insensitive `[samples]` alias map. A non-empty
`sample_label` is resolved through the same map. If that resolved name equals
the prefix or derived sample ignoring case, the derived name and casing stay
unchanged. Otherwise the resolved name replaces the sample in both experiment
and sweep tables. This corrects individual labelling errors without moving
other acquisitions with the same prefix. `SessionManifest.acquisition_samples()`
returns label-to-sample mappings for non-empty entries.

Import always sets `experiment.attrs["sample_overrides"]` to a JSON-safe
mapping, empty when there are no corrections. Each corrected label maps to
`{"label_sample": <derived>, "sample": <chosen>, "source": "session_manifest"}`.
Studio groups by the imported sample and marks these spots with `· DB` and a
tooltip naming the reassignment.

`read_experiment_data` looks inside a directory for
`session_manifest.toml`; for a directory named `raw`, it also checks its
parent. A file source starts this search in its parent. An in-memory session
uses its first source path. Explicit `session_manifest` can be a path or
SessionManifest; `False` disables discovery. With a manifest, a supplied
bundle root maps to its `raw/` when present. Explicit `sep` wins; explicit
aliases override matching manifest aliases case-insensitively.

`unpack_session_bundle` accepts ZIP bytes or a ZIP path, checks ZIP integrity,
one top-level folder, unsafe paths and symlinks, then verifies each manifest
raw filename/hash pair. It hashes the manifest and can compare it with
`expected_export_sha256`. Direct `read_experiment_data` import does not
perform these manifest hash checks. Project selection prefers
`<session_key>.upbr`, otherwise the first sorted `.upbr`; none is required.
Unpacking fills missing origin fields and replaces the server with the
client's URL when supplied.

The cache defaults to `~/UPbRPy/sessions/`, overridable with
`UPBRPY_SESSION_CACHE`. Matching manifest hashes reuse the cached directory;
a changed export renames the previous directory with `.superseded-<UTC>`.
The local `.upbrpy_bundle.json` cache record contains the following keys.

| Cache key | Type | Meaning |
| --- | --- | --- |
| session_id, session_key | Strings | Manifest session identity / folder fallback |
| server | String | Client server URL, trailing slash removed; empty if absent |
| export_sha256 | String | Manifest SHA-256 |
| zip_sha256 | String | Download bytes SHA-256 |
| fetched_at | String, UTC ISO | Fetch/reuse time |

## Reduction bundle output

`export_reduction_bundle(pipeline, output_directory, *,
include_depth_profile=True, hash_inputs=True, include_concentrations=True,
include_depth_concentrations=True) -> ExportBundle` runs finalization and
optionally depth profiling. The result lists directory, written files and
nonfatal warnings. It writes into a directory; it does not create a ZIP.

| File / directory | Type | Contents / when absent |
| --- | --- | --- |
| UPbRPy_publication_results.xlsx | XLSX workbook | Results, Horstwood Table 4, Secondary standards, QC metrics, Methods, Parallel audit, Table notes; optional nonempty Spot concentrations and Depth profiles |
| UPbRPy_publication_results/*.csv | CSV per worksheet | Lowercase names, spaces/slashes replaced by underscores; transparent companions |
| selection_windows.csv | CSV | Same available selection columns and extra `selection_*` columns as project selections |
| spot_mean_results.csv | CSV | `pipeline.run_spot_means()` output |
| depth_profile_results.csv | CSV | Written only when `include_depth_profile=True` |
| reduction_manifest.json | UTF-8 JSON | Pipeline provenance, keys below |
| UPbRPy_reproducible_workflow.ipynb | Jupyter notebook JSON | Unexecuted code and Markdown cells with settings and reviewed selections embedded; references original input/library paths |

If workbook export raises ImportError, the bundle records a warning and
writes each table directly in the bundle root using lowercase names with
spaces replaced by underscores. The function does not copy the reference
library, raw data or project into the output directory. External submission
code can add files separately.

### reduction_manifest.json

All top-level keys below are emitted by `pipeline.manifest`; some values
may be null. `hash_inputs=True` is the bundle default, while the standalone
`pipeline.manifest()` default is false.

| Key | Type / unit | Meaning / missing value behavior |
| --- | --- | --- |
| format_version | Integer, `2` | Pipeline manifest format |
| created_utc | ISO string | Manifest creation time |
| upbrpy_version | String | Installed package version or implementation fallback |
| settings_schema | Integer | Saved settings revision, currently `1`; newer revisions refuse replay |
| python_version | String | Runtime version |
| platform | String | Runtime platform description |
| source_directory | String/null | Input source path, possibly a CSV |
| reference_library_path | String/null | Explicit library path; null for packaged default |
| reference_library_version | String/null | Declared version or `format-2`, suffixed with `+sha256.<12 hex>` |
| reference_library_sha256 | String/null | Library identity digest |
| input_files | Array of objects | Pipeline's registered input files; not directory discovery performed during export |
| input_files[].path | String | Recorded input path, even if now missing |
| input_files[].size_bytes | Integer, bytes | Only if file exists |
| input_files[].modified_ns | Integer, Unix ns | Filesystem modification time, only if file exists |
| input_files[].sha256 | String | Only if file exists and hashing requested |
| config | Object | PipelineConfig serialized above |
| detector_calibration | Object | Applied experiment fit record, default `{"mode": "qtegra"}` |
| sample_overrides | Object | Per-acquisition sample corrections from experiment attrs, keyed by label with `label_sample`, `sample`, `source`; empty when none |
| spot_renames | Object | Current spot name to original raw acquisition label; rename chains collapsed |
| calculation_excluded_spots | Array of strings | Current spot names omitted from every calculation |
| removed_spots | Array of strings | Original labels removed from the working session |
| completed_stages | Array of strings | Stages cached at manifest creation |
| spot_count | Integer | Number of experiment rows |
| sweep_count | Integer | Number of total-data rows |
| channels | Array of strings | Pipeline channel list |
| metadata | Object | Caller metadata; pipeline origin added as `metadata.origin` if not overridden |
| implementation.pipeline_module | String | `UPbRPy.pipeline` |
| implementation.executable | String | `sys.executable` path |

Library hashing includes manifest filenames and file contents in manifest
order with CRLF normalized to LF. Raw input hashes use original bytes.
The reduction manifest does not contain the full `experiment.attrs["qtegra"]`
record. Hashes document identity; replay still needs the external inputs
and a compatible runtime.
