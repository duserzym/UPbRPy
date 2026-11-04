# UPbRPy Studio architecture

Studio's place in the lab workflow (database mode, the `upbr://` scheme,
submit and preferred runs) is planned in `docs/lab_workflow_integration.md`.

## Technology decision

UPbRPy Studio remains a native `PySide6 + pyqtgraph` application. The
scientific package is already Python-native, so this keeps one calculation
engine for notebooks and the desktop app. It also avoids copying large
DataFrames across a web-process boundary and gives the trace editor direct,
high-performance access to NumPy arrays.

A TypeScript front end would be justified for a hosted, multi-user product.
For the current local analytical workflow it would add packaging complexity,
inter-process serialization, and a second application state model without
improving the numerical pipeline.

## Separation of responsibilities

- `UPbRPy.pipeline` owns stage ordering, configuration, cache invalidation,
  provenance, and all calls into the scientific modules. It has no Qt
  dependency.
- `GUI.services` translates between GUI session state and the shared pipeline.
  It contains adapters, not alternate scientific calculations.
- `GUI.workers` runs loading and reduction tasks on the Qt thread pool so
  plots and controls remain responsive.
- `GUI.widgets` renders traces, diagnostic fits, isotope controls, and
  read-only result tables.
- `UPbRPy.reproducibility` exports the reviewed settings, publication tables,
  audit tables, complete sweep data, provenance, and a replayable notebook.
- `UPbRPy.project` owns the versioned `.upbr` parameter/path document used to
  pause and resume GUI work without caching raw or calculated DataFrames.

The source directory is therefore the single source of truth. A correction
selected in the app and the same correction selected in a notebook execute
the same Python function.

## Guided workflow

The left processing panel exposes the reduction as explicit dependent stages:

1. Import files and auto-pick windows.
2. Review baseline, plateau, signal start, and signal end.
3. Subtract baselines.
4. Apply internal elemental normalization.
5. Calibrate concentrations.
6. Calculate raw ratios.
7. Model and apply downhole fractionation.
8. Model and apply session fractionation.
9. Calculate spot means and full-signal depth profiles.
10. Apply common-Pb methods, automatic flags, and preferred-age decisions.
11. Review secondary-standard weighted means and export.

Changing an upstream choice invalidates only its dependent products. Cached
independent stages are retained.

## Performance and memory strategy

- Data are held in pandas/NumPy in the shared process; the GUI does not make a
  second browser-side copy.
- pyqtgraph curves use clipping and automatic downsampling.
- Final tables use `QAbstractTableModel` and a sorting proxy rather than one
  widget per cell.
- Scientific work executes on background workers.
- The vectorized 207Pb/206Pb age inversion removes per-sweep optimization,
  making depth-profile age calculation practical for large sessions.
- The isotope grid starts with only the primary and essential geochronology
  channels visible, while retaining one-click access to every measured mass.

## Responsive behavior

At wide desktop sizes a narrow semantic workflow rail, plot workspace, and
spot/isotope inspector are shown together. Every processing stage remains
visible in the rail without scrolling on normal displays. Its vector glyphs
also encode locked, current, and completed states. The selected stage's
explanation and actions appear in a compact central context bar.

At intermediate widths the inspector collapses. Below 1100 pixels both side
panels collapse so the scientific plot remains usable; either panel can be
restored from the View menu. On short displays the rail hides settings that
are also available from the native menus and tightens its rows so the complete
sequence remains visible. Notes and the activity log also collapse.

Horizontal scrolling is limited to controls that cannot be meaningfully
compressed, such as the complete sequence of diagnostic plot modes.

## Reproducibility and export

The GUI's working-session format is a human-readable `.upbr` JSON document.
It stores relative and absolute fallbacks for the experiment and reference
library, the shared pipeline configuration, reviewed per-spot windows, saved
stage identifiers, and compact workspace state. Opening it verifies that the
spot list is unchanged, overlays the reviewed windows on a fresh raw-data
import, and recalculates the previously completed stages. This keeps project
files small and prevents stale calculated products from becoming an
undocumented alternate source of truth.

The publication and reproducibility bundle includes:

- a formatted Excel workbook and transparent companion CSV tables;
- selected and parallel common-Pb results;
- automatic flags, preferred ages, weighted means, and MSWD values;
- exact baseline, plateau, signal-start, and signal-end selections;
- spot-mean and optional complete depth-profile tables;
- a machine-readable reduction manifest; and
- a Jupyter notebook that reconstructs the shared pipeline and embeds the
  reviewed GUI selections.

The notebook restores the same TOML-first reference-material archive with the
legacy CSV fallback used by the application. Callable custom calibration
models are recorded by import path in the manifest.

## Packaging direction

- Notebook-only users install the core library.
- Desktop users install `UPbRPy[gui]`.
- Release builds can package the same entry point as signed macOS and Windows
  applications after the GUI stabilizes.

The native application mark is a scalable SVG derived from the visual language
of the Boise State Isotope Geology Laboratory logo. It combines isotope
orbits, geological mountain relief, and an idealized tetragonal zircon inside
a modern macOS-style squircle. Raster, Windows ICO, and macOS ICNS assets are
generated from that same vector source.
