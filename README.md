<p align="center">
  <img src="GUI/assets/icons/png/upbrpy_zircon_256.png" width="112" alt="">
</p>

<h1 align="center">UPbRPy</h1>

<p align="center">Data reduction engine and desktop Studio for LA-ICP-MS U-Pb and trace-element data</p>

<p align="center">
  <a href="https://github.com/duserzym/UPbRPy/releases/latest">Download</a> ·
  <a href="https://duserzym.github.io/UPbRPy/">Manual</a> ·
  <a href="https://github.com/duserzym/grainatlas">GrainAtlas</a> ·
  <a href="https://github.com/duserzym/upblab">UPb Lab</a> ·
  <a href="https://github.com/duserzym/upb-standards">upb-standards</a>
</p>

<img src="docs/assets/screenshots/09-spot-concordia.png" width="100%" alt="Spot concordia review in UPbRPy Studio">

UPbRPy reads Thermo iCAP RQ / Qtegra exports from a Teledyne Analyte 193 nm
laser workflow. The Python engine and PySide6 Studio share one reduction
pipeline. Zircon, apatite, rutile and titanite presets
guide reduction. U-Pb ratios and trace elements come from the same spots.

Screenshots use real measurements with neutral sample labels and no acquisition
metadata. They illustrate the interface, not an endorsed reduction of a sample.
The [manual](https://duserzym.github.io/UPbRPy/manual/import/) explains each
control and the checks needed before interpreting results.

## Workflow

### Import and review windows

Open Qtegra exports, choose a mineral preset and check the automatically picked baseline, plateau and signal windows. Adjust any spot's intervals before reduction; undo and redo cover drags, nudges and batch picks.

<img src="docs/assets/screenshots/01-import-and-auto-pick-windows.png" width="100%" alt="Imported traces and automatically picked windows">

### Normalize and calibrate

Apply baseline subtraction and internal normalization, then calibrate trace-element
concentrations against measured reference glasses. Inspect calibration factors
and uncertainties by channel.

<img src="docs/assets/screenshots/05-calibrate-and-calculate-concentrations.png" width="100%" alt="Trace-element concentration calibration">

### Ratios and downhole correction

Build isotope ratios from baseline-subtracted signals. Fit primary-standard
ratios through the ablation to correct within-spot fractionation.

<img src="docs/assets/screenshots/07-downhole-correction.png" width="100%" alt="Isotope ratios and downhole correction fits">

### Session fractionation correction

Fit primary-standard ratios through session time after downhole correction.
Review drift, outliers and gaps before applying the session correction.

<img src="docs/assets/screenshots/08-session-fractionation-correction.png" width="100%" alt="Session fractionation correction through time">

### Final results

Review corrected spot means, dates, common-Pb alternatives, flags and secondary
references. Inspect sweep-level uncertainty and export publication tables.

<img src="docs/assets/screenshots/09-dates-and-concordia-review.png" width="100%" alt="Final spot results and dates">

### Depth profiles

Review and export corrected ratios, dates and concentrations through the full
signal interval. Time within an ablation requires a separate calibration to
become measured depth.

<img src="docs/assets/screenshots/10-depth-profile-export.png" width="100%" alt="Corrected full-signal sweep table for depth profiles">

### UPbDB sign-in

Studio can open a session bundle, link a local reduction to its database
session, submit a run and mark it preferred. Install `.[gui,database]` for
these optional actions.

<img src="docs/assets/screenshots/12-sign-in-to-upbdb.png" width="100%" alt="UPbDB sign-in dialog with placeholder address">

## Install

Download Studio from [GitHub Releases](https://github.com/duserzym/UPbRPy/releases/latest)
for macOS arm64. The app is signed with the lab's own certificate and is not
notarized. Unzip it, move it to Applications and launch it. If macOS blocks
the first launch, use **System Settings > Privacy & Security > Open Anyway**
after attempting to open the app.

**Help > Check for updates** offers Stable and Beta channels. Downloaded
archives are SHA-256 verified before **Restart to update** is offered.
See [Install and first launch](https://duserzym.github.io/UPbRPy/get-started/install/)
for details.

From a source checkout, with Python 3.12 or newer:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[gui]"
upbrpy-gui
# Equivalent launch: python -m GUI
```

On Windows use `.venv\Scripts\activate`. For the engine alone install with
`python -m pip install -e .`. Optional extras include `database`, `notebook`,
`export`, `docs`, `build` and `dev`.

```python
from UPbRPy.io import read_experiment_data

experiment, channels, sweeps = read_experiment_data("Rich.csv")
```

The [Python API guide](https://duserzym.github.io/UPbRPy/reference/python-api/)
shows the reduction pipeline and publication-table writers. Use a Rich CSV or
per-spot exports; the Rich CSV is the canonical record. Y-only UPbR CSV files
are unsupported because they contain no timing.

## Reference materials

Reference-material values come from a pinned MIT-licensed
[upb-standards](https://github.com/duserzym/upb-standards) snapshot
(`47c15a5998db`), shipped with Python and Studio. The
[reference-material guide](docs/data-model/reference-materials.md) describes
provenance, shared checkouts and `scripts/sync_upb_standards.py` refreshes.

## Citation

Use [CITATION.cff](CITATION.cff) for citation metadata.

## Related apps

- **[GrainAtlas](https://github.com/duserzym/grainatlas)**: stitch SEM images of grain mounts, find grains, place laser spots and print sheets.
- **[UPb Lab](https://github.com/duserzym/upblab)**: one window for the lab workflow: sessions, images and review of stored results.
- **[upb-standards](https://github.com/duserzym/upb-standards)**: reference-material values with full citations, used by UPbRPy.
- **UPbDB**: the lab database server for sessions, spots and stored reductions (private).

## For developers

After the source install above, install the development extra and run the quick tests:

```sh
python -m pip install -e ".[dev]"
make test-quick
```

See the [manual](https://duserzym.github.io/UPbRPy/),
[data model](docs/data-model/index.md) and
[Qtegra export contract](docs/qtegra_export_formats.md).
Development guides cover [building Studio](docs/development/building.md),
[tests](docs/testing.md) and [contributing](docs/development/contributing.md).
Report bugs through [GitHub Issues](https://github.com/duserzym/UPbRPy/issues).

## License

[MIT](LICENSE).
