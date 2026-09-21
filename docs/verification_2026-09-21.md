# UPbRPy verification — 21 September 2026

The updated source passes the automated suite, four real-data app export/replay
checks, comparisons with all four publication walkthroughs, and an exported
notebook execution in a real Jupyter kernel. The initial audit verified the source workflow; the subsequent packaged build is documented below. Neither audit accepts the scientific quality of every analysis.

## Sources checked

- UPbRPy source based on `41a8170`, including the working-tree fixes below.
- Manuscript: `UPbRPy_manuscript/manuscript/main.tex`, revision `2a8fc7a`.
- Publication walkthroughs: `LAICPMS_UPbR_database/code/PUBLICATION_EXAMPLES.md`
  and its four designated spot notebooks, revision `2219146`.
- Existing manuscript/workflow compatibility audit was checked against current
  implementations and tests rather than taken as proof of execution.

The manuscript explicitly uses session residual scatter, excludes fitted
coefficient covariance from the production uncertainty branch, distinguishes
plateau spot means from full-signal profiles, and uses titanite Si normalization.
The code and regression checks now reflect these choices. Existing tests also
cover analytical/calibration/systematic uncertainty separation, ratio covariance,
common-Pb branches, explicit physical-depth calibration, and reporting constants.

## Repairs made during verification

1. Fixed a `package_version`/`upbrpy_version` key mismatch that prevented both
   bundle export and the exported notebook's publication cell from completing.
2. Made the app's notebook export preserve both concentration-export toggles.
3. Added notebook cell IDs and safely quoted embedded JSON, including analysis
   names/settings containing triple quotes.
4. A missing explicitly selected reference library now raises an error instead
   of silently substituting the default library. Replay manifests list CSV inputs.
5. Retained selection audit metadata alongside reviewed window coordinates.
6. Retained physical-depth calibration when GUI configuration is synchronized.
7. Updated titanite's material preset from rutile to titanite, removed stale
   guidance, and added the walkthrough secondary standards MADJCU, R632, and BLR.
8. Aligned app QC with the baseline-subtracted signal used in the notebooks.
9. Preserved imported sample labels/aliases in spot-concentration tables rather
   than rebuilding labels with an underscore-only separator.
10. Replaced an obsolete positive coefficient-uncertainty assertion with checks
    for the manuscript's scalar residual scatter, including zero and nonzero cases.
11. Installed the missing declared XlsxWriter, jupyter_bokeh, and CairoSVG
    dependencies in the development environment. Jupyter verification tools were
    installed separately in a temporary directory.

## Results

**205 tests passed, no failures or skips.** The nine warnings are the existing
Qt font-database constructor deprecation. Eight additional regression tests cover
export execution, GUI choices, reference handling, depth calibration, QC, and
sample identity.

| App export dataset | Spot rows | Profile sweeps | Exported notebook parity |
| --- | ---: | ---: | --- |
| Zircon_28Mar25_1 | 197 | 4,370 | Pass |
| Apatite_20Sep22 | 46 | 13,578 | Pass |
| Rutile_28Feb24 | 77 | 5,355 | Pass |
| Titanite_20Mar25 | 51 | 1,263 | Pass |

Each app workflow was imported through `GUI.services.load_session`, finalized,
exported as a bundle with input SHA-256 hashes, then replayed by executing every
exported code cell in a fresh namespace. Spot means, full depth profiles, and
selected results agree at `rtol=atol=1e-10`. Each bundle produced 15 files without
export warnings. These checks were rerun after the final scientific fixes.

All four original walkthrough notebooks executed every code cell successfully
using their existing settings; optional bulk vector export remained disabled as
specified in those notebooks. With matching settings and reviewed selections,
the shared pipeline reproduces their concentrations and common selected-result
columns for the same analyses at `rtol=atol=1e-9`. Their published spot tables
contain 190, 36, 70, and 43 rows, respectively; unlike the broader app audit tables,
they exclude concentration calibrants. GUI presets and session-specific notebook
choices are not assumed to be identical (for example, session polynomial degree,
selection windows, or preferred common-Pb method).

All four app notebooks pass nbformat schema validation. The exported zircon
notebook additionally completed all nine code cells in a fresh IPython/Jupyter
kernel and produced its workbook without cell errors.

Publication checks cover XLSX ZIP integrity, all data-cell values against CSV
companions, worksheet headers, frozen panes, filters, methods and table notes.
The Table 4 output distinguishes Ma dates, 2-sigma analytical/calibration/selected
uncertainties, concentration units and 95% confidence intervals, ratio
correlations, preferred method, and review flags. This was a structural and
numerical workbook check, not a journal-specific print-layout review.

## Repeating the checks

With the package and its development/GUI dependencies installed:

```sh
QT_QPA_PLATFORM=offscreen python -m pytest tests -q
python scripts/verify_reproducibility.py \
  --database ../LAICPMS_UPbR_database \
  --output /tmp/upbrpy-release-verification
```

The integration script creates links to the source datasets and writes all
notebooks, workbooks, and CSV files to the external scratch directory. It exits
with failure if any export or table comparison fails. Run it in a fresh output
directory. The original data are not modified.

The environment actually tested used Python 3.14, NumPy 2.4.5, pandas 3.0.3,
SciPy 1.17.1, PySide6 6.11.1, Bokeh 3.9.0, Panel 1.8.10, XlsxWriter 3.2.9,
and jupyter_bokeh 4.1.0. This is not a fresh dependency-resolution test of the
higher minimum versions currently declared for some scientific packages.

## Scientific and release boundaries

- Incomplete selections caused one zircon and three rutile spots to be omitted
  from ratio results with explicit warnings. Common-Pb root searches also emit
  numerical warnings for some real-data inputs. Exported validity/QC flags and
  missing values remain visible; no failing analyses were made to look accepted.
- These outputs are ready for review and reporting, not blanket acceptance of
  every date. Review selections, standards, correction choice, and QC flags for
  the dataset being published.
- Replay requires the original raw files and reference library. The bundle is
  not a self-contained raw-data archive; preserve those inputs and update paths
  when relocating a notebook. Input hashing was enabled in this audit; the GUI's
  normal bundle export still uses its existing optional-hash policy.
- The initial audit did not rebuild release bundles. Subsequent macOS packaging
  checks are documented below; Windows and fresh-machine certification remain separate.
- Large verification outputs remain outside the repository in
  `/tmp/upbrpy-verification`; only source, tests, this report, and the lightweight
  replay script were added to the repository.


## Subsequent rebuilt macOS application

The rebuilt app uses the declared dependencies: NumPy 2.5.3, SciPy 1.18.1,
pandas 3.0.3, Matplotlib 3.11.2, Bokeh 3.9.2, PySide6 6.11.1, and PyInstaller
6.22.3. **206 source tests pass** in this environment. The extra regression
ensures numerical depth-table imports do not load notebook plotting libraries.

The first frozen reduction found an import of excluded Bokeh in `isoplot`.
Plotting imports now occur inside plotting functions, allowing numerical exports
to work without enlarging the standalone app with notebook graphics libraries.
After rebuilding, all four packaged reductions complete with the counts above
and 15 export files each. Their reports confirm `frozen: true`. They ran outside
the checkout with PYTHONPATH and PYTHONHOME removed, and their spot, depth, and
selected-result CSVs match the source outputs at 1e-9. Native launch, offscreen
window rendering, archive production, and `codesign --verify --deep --strict`
all pass. The signature is local/ad-hoc, not Developer ID or notarization.

Local artifacts are under `~/Library/Caches/UPbRPy/27890196f07d/`:
- `dist/UPbRPy Studio.app`: approximately 188 MiB.
- `dist/UPbRPy-Studio-0.1.0-macOS-arm64.zip`: approximately 80 MiB.
- `work/`: disposable compiler and smoke-test output.
- `verification/`: all four frozen reduction bundles and logs.
- `previous-build/` and `previous-dist/`: preserved old generated outputs.
- `venv/`: development environment, reached by the ignored checkout `.venv` link.

The checkout decreased from about 3.2 GiB to **81 MiB including 71 MiB of Git
history**, leaving roughly 10 MiB of working files. New build defaults and CI
staging are external; repository-local build output paths are rejected. No
application binaries, archives, environments, or generated data are committed.
