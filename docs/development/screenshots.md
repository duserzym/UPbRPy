# Manual screenshots

`scripts/make_manual_screenshots.py` drives Studio in one process and captures
widgets with `QWidget.grab()`. Dialogs are composed from their own widget grabs
over the main-window grab. No pixels are read from the desktop.

```sh
QT_QPA_PLATFORM=offscreen scripts/guarded.sh -m 6144 -t 600 -- \
  .venv/bin/python scripts/make_manual_screenshots.py --out /tmp/upbrpy-shots
```

`--source /path/to/Rich.csv` selects a real Rich export. A folder may contain
one Rich CSV and companion per-spot files. The default is the reference-only
fixture at `tests/fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv`.
The nine-channel fixture has no 202Hg. Studio currently requires it for raw
ratios, so a fixture-only run captures the available stages through concentration
calibration, sign-in and Updates. It explicitly reports unavailable later views
in `captures.json`; it never inserts surrogate measurements. Use a complete
Rich export to regenerate the entire tour. The checked-in tour was generated
from a complete real export with neutral labels.

`--out` defaults to `docs/assets/screenshots/`. For a display run, omit
`QT_QPA_PLATFORM`; `--scale 2` requests a scale factor, or leave it unset to use
the display's device pixel ratio. Windows remain 1440×900 logical pixels, with
an additional 1280×800 empty overview. Review every display capture before use.

The source is read only. A temporary `Zircon_demo` copy retains numerical cells
and metadata. Reference-material labels and their library aliases stay;
other prefixes become Sample A, Sample B and so on in acquisition order,
retaining spot suffixes. Labels are changed in both Rich label rows and
companion first lines; companion filenames are neutral. The replacement map
is held in memory and is never written or printed. The temporary copy is
removed at the end of the run.

Settings are redirected to temporary INI storage before Studio is imported.
The script blocks requests and keyring imports, socket connections and browser
launches, disables update scheduling and the persistent results cache, and
redirects autosave/inbox folders. It waits for reduction workers and selection
and readout timers before capturing. The activity log is hidden, source paths
are only in tooltips, and the export dialog's preview is captured on its Table
tab because the footnotes contain acquisition metadata. The sign-in dialog uses a
placeholder server and is never submitted. Updates shows the actual source
checkout state, without a simulated release.

PNGs are optimized losslessly first, then palette-compressed if needed to keep
each near or below 400 KB. This affects image pixels only, never measurements.

## Capture inventory

| PNG stem | View |
| --- | --- |
| `00-overview` | Empty Studio at 1440×900 |
| `00-overview-1280x800` | Empty Studio at 1280×800 |
| `01-import-and-auto-pick-windows` | Raw traces and automatic windows |
| `02-review-baseline-and-plateau-picks` | Baseline, plateau and signal edge edits |
| `03-subtract-baseline` | Baseline-subtracted traces |
| `04-internal-normalization` | Normalized plateau intensities |
| `05-calibrate-and-calculate-concentrations` | Concentration calibration factors |
| `06-build-raw-isotope-ratios` | Raw ratio sweeps |
| `07-downhole-correction` | Primary-reference downhole correction |
| `08-session-fractionation-correction` | Fractionation through session time |
| `09-dates-and-concordia-review` | Final spot table |
| `09-spot-concordia` | Primary-reference spot concordia |
| `10-depth-profile-export` | Corrected full-signal sweep table |
| `10-depth-concordia` | One reference acquisition's depth concordia |
| `11-export-results` | Export dialog, Table preview tab |
| `12-sign-in-to-upbdb` | Unsigned-in dialog with placeholder server |
| `13-updates` | Updates dialog in a source checkout |
