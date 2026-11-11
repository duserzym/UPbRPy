# UPbRPy data model

**UPbRPy data model 1.0, 2026-11-05**

This specification describes the Qtegra inputs, acquisition objects,
reference-material library, saved projects and bundles read or written by
this version of UPbRPy. The Python readers and writers define the behavior.
The data-model version is separate from the TOML, project and pipeline
`format_version` numbers.

## Accepted inputs at a glance

| Input | Role | Minimum content | Without the richer metadata |
| --- | --- | --- | --- |
| [Rich single CSV](rich-csv.md) | Canonical acquisition record; one column per acquisition | Two headers, MainRuns Y for each channel, and Time or StartTime | Reduction still uses Y; capabilities describe the exported groups |
| [Per-spot folder](per-spot.md) | Accepted input, also a Rich companion | Fixed per-file header layout, start, shared Time and channel Y | No analog/counter, mass, stop, user or sample-list metadata |
| [Per-spot file](per-spot.md) | One accepted acquisition | Same layout as a folder member | No session companions are discovered when opening a file directly |
| Folder containing Rich and per-spot exports | Discover and optionally merge | At most one Rich and an unambiguous companion set | Rich alone or immediate per-spot files alone can load |
| [Session bundle root](project-and-bundle.md#session-bundle) | Raw exports with label aliases and database identifiers | `session_manifest.toml` and usable exports in `raw/` | Direct CSV/folder import works without a manifest |
| [QtegraSession](acquisition-model.md) | In-memory input to experiment import | Acquisitions with Y and sweep times | Paths and optional metadata can be absent |
| UPbR Y-only single CSV | Unsupported since 2026-11-05 | Has neither MainRuns Time nor StartTime | Rejected: neither sweep spacing nor acquisition starts can be recovered |

The [decision record](../qtegra_export_formats.md) explains the choice of
Rich as the record. Its historical comparisons and intended downstream
contracts are distinct from the implemented behavior specified here.

!!! warning "Y is a count rate: counting errors need the dwell time"
    Qtegra Y values are cps, not detected ions. The Poisson variance of one
    reading is `Y / dwell`, not `Y`. Treating cps as counts understates every
    sweep counting uncertainty by `1 / sqrt(dwell)`: 3.65x at 75 ms, 6.3x at
    25 ms. Spot means from sweep scatter are unaffected; sweep ratio errors,
    sweep error correlations, depth-profile concentration errors and the
    Poisson term of 206Pb/204Pb are. Per-spot exports record the dwell; for
    Rich input UPbRPy recovers it from the cps value of one count. See
    [Counting dwell](acquisition-model.md#counting-dwell). UPbRPy before
    2026-11-11 used `Y` as the variance.

## Minimum for a reduction

A loadable reduced Rich file has the two header rows, channel Y rows and
at least one timing source. Recorded MainRuns Time is preferred. With only
StartTime, UPbRPy reconstructs nominal sweep offsets and warns; with only
Time, it joins acquisitions on a continuous axis without calendar gaps.
See [Rich timing](rich-csv.md#reduced-files-and-timing).

Import is the first step: a numerical reduction also needs usable baseline
and signal sweeps, channels for the requested ratios (for example `206Pb`
and `238U` for Pb/U), and reference values and standard acquisitions for
the selected calibrations. The import default signal channel is `29Si`;
choose another `primary_channel` when appropriate. No single channel list
or reference-material choice is sufficient for every workflow.

`read_experiment_data` returns `(experiment, element_channels, total_data)`.
The [acquisition model](acquisition-model.md#experiment-tables) describes
these tables. A [.upbr project](project-and-bundle.md) stores settings and
selections, while a reduction bundle writes results and provenance. Neither
duplicates the raw acquisition arrays.
