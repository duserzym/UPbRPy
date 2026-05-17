# GUI Roadmap

## Product direction

The GUI is being rebuilt as a native `PySide6 + pyqtgraph` application that uses the existing `UPbRPy` source functions as the reduction engine. The goal is to support the full traditional LA-ICP-MS reduction workflow while extending it for depth-profile processing, sweep-level uncertainty propagation, and depth-profile visualization.

## Non-negotiables

- Keep notebook workflows backward-compatible.
- Do not break the existing public `UPbRPy` import surface while the GUI is being rebuilt.
- Keep plotting native and performance-oriented; do not return to browser views embedded in Qt.
- Keep heavy reduction logic out of widgets.

## First implementation slice

- Native app shell with an iolite-like workstation aesthetic and light/dark appearance toggle.
- Shared core data loader through `UPbRPy.read_experiment_data`.
- Spot list, channel visibility controls, primary channel selection, and trace plot.
- Baseline and plateau overlays using pyqtgraph regions, including draggable selection review.
- Notebook-derived mineral presets for zircon, apatite, rutile, and titanite with guided step-by-step explanations.
- Actionable stage buttons that call the existing `UPbRPy` functions for baseline subtraction, normalization, concentration, and ratio correction previews.
- Native app icon shown in the window chrome/dock and echoed in the in-app brand header.
- Architecture that is ready for worker-thread loading and reduction jobs.

## Next milestones

### Phase 1: Trace workspace

- Finish spot trace interaction patterns.
- Make workflow steps executable instead of informational, starting with baseline review and subtraction.
- Add synchronized selection editing for baseline and plateau windows.
- Add a persistent session state model and project save/load format.

### Phase 2: Calibration and QC

- Primary and secondary reference material management.
- Downhole fractionation and session drift views.
- QC summary panels for standards, unknowns, and rejected analyses.

### Phase 3: Depth profiles

- Sweep-level depth-profile explorer.
- Profile-aligned uncertainty visualization.
- Export tools for profile tables and notebook recipes.

### Phase 4: Packaging and release

- Use `pyproject.toml` as the distribution source of truth.
- Keep conda for development convenience only.
- Publish the library with an optional `gui` extra: `pip install UPbRPy[gui]`.
- Build user-friendly standalone apps for macOS and Windows with PyInstaller or Briefcase after the GUI stabilizes.

## Environment strategy

For development, the conda environment remains practical because the scientific stack is already there. For release, the better cross-platform path is:

1. Keep the package installable with standard `pip` via `pyproject.toml`.
2. Make GUI dependencies optional extras rather than hard requirements for notebook-only users.
3. Use standalone application bundles for end users who should not need Python or conda at all.

That gives three supported modes:

- Notebook users: install the library only.
- Developer users: use conda or a local virtual environment.
- End users: install a packaged desktop app for macOS or Windows.

## Core refactor strategy

The current `UPbRPy/UPbRPy.py` file should not be split immediately if that risks breaking notebooks. The safer plan is:

1. Introduce internal modules later for I/O, selection logic, calibration, uncertainty propagation, and plotting.
2. Keep `UPbRPy/UPbRPy.py` as a compatibility facade that re-exports the existing public functions.
3. Keep `UPbRPy/__init__.py` re-export behavior unchanged until a versioned deprecation plan exists.

That gets the source tree cleaner without forcing users to rewrite existing notebooks.

## Current notebook-derived workflow notes

- Zircon, apatite, and rutile are supported directly by the current `internal_normalization` backend material modes.
- The titanite notebook currently follows a titanite-specific sequence but still reuses the rutile-style backend normalization branch.
- Rutile notebooks add an explicit glass outlier filtering stage before concentration calibration.
- Apatite notebooks use longer baseline and plateau defaults and require more caution around initial or common Pb assumptions.
- Depth-profile notebooks separate final spot tables from final sweep-level depth-profile tables; the GUI should preserve that distinction.

## Iolite-informed workflow

The current GUI sequence should track the way analysts actually work through iolite, then bridge into the notebook-specific UPbRPy details.

1. Import data and synchronize metadata.
2. Build or auto-generate selections and baseline groups.
3. Review selections, run a selection checker, and refine bad windows before crunching.
4. Subtract baselines and inspect corrected traces.
5. Establish beam-seconds and masks for U-Pb style ratio review.
6. Run the reduction stages: internal normalization, concentration calibration, raw ratios, downhole correction, and session correction.
7. Review standards, QA/QC, concordia-style outputs, and only then export spot or depth-profile products.

This order comes from the iolite U-Pb tutorial, automatic selections tool, selection refinement notes, and selection checker guidance, then is specialized to the notebook sequence used in this repository.

## Executed in the current GUI

- Import and notebook-aligned auto-pick for the zircon preset now use the actual zircon notebook defaults.
- Baseline and plateau windows are draggable in the trace view during the review steps and can be applied back into the live session.
- The workflow panel exposes stage actions for baseline subtraction, internal normalization, concentration calibration, raw isotope ratios, downhole correction, and session correction previews.
- Plot modes now switch between raw traces, baseline-subtracted traces, normalized plateau data, raw ratios, downhole-corrected ratios, and session-corrected ratios.
- A light theme is available by default, with manual `light`, `dark`, and `follow system` appearance modes.
- Zircon validation has been run against the external `LAICPMS_UPbR_database` zircon dataset, including the stage action chain through session correction.
