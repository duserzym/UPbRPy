# Use the engine from Python

The engine runs without Qt or a database. Import focused modules so a reader
or numerical task does not load the notebook plotting interface.

## Read and inspect a session

```python
from UPbRPy.qtegra import open_qtegra
from UPbRPy.io import read_experiment_data

session = open_qtegra("Zircon_demo_Rich.csv")
print(len(session.acquisitions), session.channel_names)
print(sorted(session.capabilities))

experiment, channels, total_data = read_experiment_data(
    session,
    primary_channel="29Si",
    baseline_sweep_n=10,
    peak_sweep_n=20,
    threshold=1,
    filter_percentile=0.3,
    signal_duration_limit_seconds=9.0,
)
```

`open_qtegra(path, *, cycle_time_s=None)` accepts a Rich CSV, per-spot file or
folder. Folder discovery can merge Rich and companion files. `read_rich_csv`
and `read_spot_folder` provide explicit readers; `merge(rich, folder, *,
rtol=1e-12)` checks matching acquisitions. See the [acquisition
model](../data-model/acquisition-model.md) for the public dataclasses, capabilities
and provenance fields.

`read_experiment_data(directory, ...)` also accepts a session bundle or an
in-memory `QtegraSession`. It returns the experiment selection table, channel
list and sweep table. Important keywords include `sample_aliases`, `sep`,
`selection_method`, `selection_options`, `signal_duration_limit_seconds`,
`detector_calibration` and `cycle_time_s`. Reconstructed time is explicitly
recorded and warned about; it is not equivalent to recorded channel timing.

## Build a reduction

Review and correct the selections before finalization. The following choices
use reference materials present in the demo. A full reduction requires the
channels needed by the ratio calculation and sufficient reference coverage.
The small reader fixture retains a subset of the instrument's channels.

```python
from UPbRPy.pipeline import PipelineConfig, ReductionPipeline
from UPbRPy.standards import build_standard_archive

config = PipelineConfig(
    workflow_key="zircon",
    material="zircon",
    primary_channel="29Si",
    concentration_standards=("NIST610", "NIST612"),
    primary_68_standard="S513",
    primary_76_standard="S513",
    primary_82_standard="S513",
    secondary_standards=("91500", "PL"),
)
pipeline = ReductionPipeline(
    experiment, total_data, channels, build_standard_archive(), config,
    source_directory="Zircon_demo_Rich.csv",
    input_files=("Zircon_demo_Rich.csv",),
    build_diagnostic_plots=False,
)
baseline = pipeline.run_baseline()
concentrations = pipeline.run_concentration()
ratios = pipeline.run_ratios()
downhole = pipeline.run_downhole()
session_corrected = pipeline.run_session()
final = pipeline.run_finalize()
depth = pipeline.run_depth_profile()
```

`ReductionPipeline(experiment, total_data, channels, standard_archive, config,
*, source_directory=None, reference_library_path=None, input_files=(),
build_diagnostic_plots=True, origin=None)` caches stage products.
`run_to(stage)` runs prerequisites, and `update_config(config)` or
`update_experiment(experiment)` invalidates dependent products. Studio calls
this same pipeline. `PipelineConfig` stores scientific choices; Studio's
workflow presets additionally supply import and display defaults.

## Export a publication table

```python
from UPbRPy.export_table import (
    ExportSettings, build_export_table, write_export_xlsx,
    write_export_csv, write_export_markdown,
)

settings = ExportSettings(csv_full_precision=True)
table = build_export_table(pipeline, settings)
write_export_xlsx(table, "results.xlsx")
write_export_csv(table, "results.csv")
write_export_markdown(table, "results.md")
```

`build_export_table(pipeline, settings=None, *, excluded_spots=())` uses the
finalized products and reference archive. Keep missing metadata explicit and
inspect footnotes. [Export table reference](../data-model/export-table.md)
lists the columns, uncertainty branches and settings. For saved selections and
portable inputs see [Projects and bundles](../data-model/project-and-bundle.md).

## Detector cross-calibration

`fit_cross_calibration(session, channel=None, window=(1e5, 2.4e6), on="all",
mode="constant", *, primary_standard=None, sep="_", smoothing_s=1800.0)` fits
Counter against Analog in the overlap window. `apply_cross_calibration(session,
calibration)` returns a new session with corrected saturated Y values.
`recover_xcal(session_or_acquisition)` recovers exported factors where possible.
The default Qtegra mode preserves the exported Y. Fit intercepts are recorded;
the applied correction uses the fitted slope. See the
[Qtegra decision](../qtegra_export_formats.md) for assumptions and limitations.
