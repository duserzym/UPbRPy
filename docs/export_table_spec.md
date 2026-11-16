# Publication results table: specification

Status: agreed design for the "Export results" table (2026-11-06). The
model is the FinalDataTable, Table S2 (sample data) and Table S3 (standard
data) sheets of Mark Schmitz's UPbR26plus workbook, as used for a Boise iCAP RQ
zircon session of 4 November 2026. UPbRPy keeps the content of those
sheets and changes the layout where that makes the table easier to publish
and to read.

This page lists every spreadsheet column group and column, its unit, the
UPbRPy quantity that feeds it, and what UPbRPy does not compute yet
(marked **gap**). Section 4 fixes the layout and section 5 the footnotes.

Decisions by Yiming Zhang on 2026-11-06 that this table follows:

- The preferred date keeps the fixed age transition (1000 Ma by default,
  `ReductionDecision.age_transition_ma`): 206Pb/238U below it, 207Pb/206Pb
  above it. A precision-aware switch is a caution for detrital work only and
  is not implemented.
- The table reports every date, not only the preferred one, with the
  metadata as footnotes, so the exported table is ready to publish.

## 1. What the spreadsheet sheets contain

| Sheet | Rows | Header rows | Notes |
|---|---|---|---|
| FinalDataTable | all 245 analyses incl. glasses, run order by sample | title (2 rows: settings), group row, two name rows | Has `Notes` (e.g. `high 204`), `Zr`, `Experiment`, and a secondary-correction block (cols 132-156) |
| Table S2 Sample data | unknown-sample analyses | same | No Notes, no Zr, no sample header rows, no footnotes |
| Table S3 Standard data | 72 reference-material zircons, glasses omitted | same | Text rows "Primary standards", "Secondary standards" separate the blocks |
| Table S1 Instrumental data | metadata | — | Laboratory, laser, ICP-MS, dwell list, data processing, reference materials, QC weighted means: the content of our footnotes |

Title-row settings on all three tables: discordance criteria `7/5-6/8`,
discordance threshold `5` %, activity (SiO2) `1`, activity (TiO2) `0.6`.

## 2. Column inventory

Units are those of the spreadsheet. "UPbRPy source" names the column in
`FinalizedReduction.parallel_results` (or the module) that feeds it today.

### 2.1 Identification

| Spreadsheet column | Unit | UPbRPy source | Status |
|---|---|---|---|
| Analysis | — | `spot_name` | ok |
| Notes | text | `automatic_flags` (flag_* columns) | ok, reworded (section 4.5) |
| Experiment (FinalDataTable col 62) | — | session name from the Qtegra export | ok, moves to footnote 1 (one session per table) |

### 2.2 Composition

| Spreadsheet column | Unit | UPbRPy source | Status |
|---|---|---|---|
| U | ppm (µg/g) | `U` | ok |
| Th | ppm | `Th` | ok |
| Pb | ppm | `Pb` | **wrong quantity today**: UPbRPy's `Pb` is calibrated from 208Pb against natural Pb in NIST glass, so for radiogenic zircon it is not total Pb (91500 2.4 vs 14.9 µg/g). New: total Pb = sum of the per-isotope Pb mass fractions 204 (where measured) + 206 + 207 + 208, each calibrated against the glass's own Pb isotope composition (section 3.2). The 208Pb-based value stays available as `Pb (208)` |
| Th/U | — | `Th/U` | ok |
| 206Pb | cps | not in results | **new**: plateau-mean baseline-subtracted 206Pb count rate (`UPbRPy.bias.spot_signal_quantities` already computes it) |
| 206Pb/204Pb | — | `206Pb/204Pb` | ok as a value; spreadsheet reports Hg-corrected 204 even when it is at background (values up to 2e8); UPbRPy leaves it blank when 204 is not detected. Keep UPbRPy's rule and footnote it |
| ±1s | absolute | `206Pb/204Pb_2e` / 2 | ok |

### 2.3 Corrected isotope ratios

| Spreadsheet column | Unit | UPbRPy source | Status |
|---|---|---|---|
| 208Pb/232Th, ±2s | —, % | `208Pb/232Th`, `208Pb/232Th_2e_no_calibration%` | ok |
| 207Pb/235U, ±2s | —, % | `207Pb/235U`, `207Pb/235U_2e_no_calibration%` | ok |
| 206Pb/238U, ±2s | —, % | `206Pb/238U`, `206Pb/238U_2e_no_calibration%` | ok |
| error corr. | — | `err_corr_no_calibration` (206/238 vs 207/235) | ok |
| 238U/206Pb, ±2s | —, % | `238U/206Pb`, `238U/206Pb_2e_no_calibration%` | ok |
| 207Pb/206Pb, ±2s | —, % | `207Pb/206Pb`, `207Pb/206Pb_2e_no_calibration%` | ok |
| (task list) 208Pb/206Pb, ±2s | —, % | 1 / `206Pb/208Pb` | optional column, off by default (not in S2/S3) |
| (UPbRPy extra) ρ Tera-Wasserburg | — | `tw_rho_no_calibration` | optional column, off by default |

### 2.4 Dates (Ma)

The spreadsheet's `±2s` is analytical. Its `±2s-sys` adds, in quadrature,
twice the session reproducibility of the primary standard for that ratio
(Instructions: "fractionation error (from S513)": 206/238 0.72 %, 207/206
0.13 %, 208/232 0.92 %, 1s), mapped to date units. Checked: the excess of
±2s-sys over ±2s is 1.41 % of the 206/238 date (2 x 0.72 % = 1.44 %).

UPbRPy has the same two pieces: `*_2e_no_calibration` (analytical) and
`*_2e_with_calibration` (analytical plus the session-calibration residual
scatter of the primary standard).

| Spreadsheet column | Unit | UPbRPy source | Status |
|---|---|---|---|
| 208Pb/232Th date, ±2s, ±2s-sys | Ma | `208Pb/232Th_age`, `_2e_no_calibration`, new `_2e_sys` | ±2s-sys new |
| 207Pb/206Pb date, ±2s, ±2s-sys | Ma | `207Pb/206Pb_age`, ... | ±2s-sys new |
| 207Pb/235U date, ±2s, ±2s-sys | Ma | `207Pb/235U_age`, ... | ±2s-sys new |
| 206Pb/238U date, ±2s, ±2s-sys | Ma | `206Pb/238U_age`, ... | ±2s-sys new |
| disc., ±2s | % | `disc%`, `disc_2e%` | **two fixes**: UPbRPy uses 7/6-6/8 (`1 - t68/t76`) where the spreadsheet default is 7/5-6/8 (`1 - t68/t75`); and `disc_2e%` is the 2s relative to the discordance itself (e.g. 1400 %), not in percentage points. New: a setting `discordance_definition` ("7/5-6/8" default, "7/6-6/8"), discordance and its 2s both in percent (percentage points) |
| 207-method 206Pb*/238U* date, ±2s | Ma | `207Pb_corrected_age`, `207Pb_corrected_age_2e_no_calibration` | ok |
| 208-method 206Pb*/238U* date, ±2s | Ma | `208Pb_corrected_age`, `208Pb_corrected_age_2e_no_calibration` | ok |
| (UPbRPy) preferred date, ±2s, ±2s-sys, method | Ma, text | `preferred_date`, `preferred_date_2e`, `preferred_date_method` | ok; its own column group (the spreadsheet marks it with bold font only) |

New ±2s-sys definition (all four systems and the preferred date):

    2s_sys = sqrt( 2s_with_calibration^2
                 + (2 x decay-constant term)^2
                 + (2 x 238U/235U term, 207/235 and 207/206 only)^2
                 + (2 x primary reference-value term)^2
                 + (2 x long-term reproducibility term, if set)^2 )

The decay-constant and 238U/235U terms come from
`UPbRPy.uncertainty.systematic_date_components` and are always included in
this column, whatever the `propagate_*` switches say (the switches keep
controlling `*_2e_selected_systematic`). The primary reference-value term is
the relative uncertainty of the primary standard's accepted ratio for that
system (S513 206/238: 561.18 ± 0.63 Ma 95 %), mapped through
`relative_ratio_date_component`; blank in the footnote if the library has no
uncertainty for that ratio.

### 2.5 Concentrations (ppm = µg/g)

| Spreadsheet column | UPbRPy source | Status |
|---|---|---|
| P Ti Y Zr Nb La Ce Pr Nd Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta Th U | element columns (`P`, `Ti`, ...) | ok. Order: by mass of the measured isotope, as in the spreadsheet. Every calibrated element is listed; the internal-standard element (Si in the zircon preset) is not. S2/S3 omit Zr; we keep it |
| 95 % CI | `<El>_95CI` | optional columns, off by default |

### 2.6 Chondrite-normalized REE

CI chondrite values of Sun and McDonough (1989), doi:10.1144/GSL.SP.1989.042.01.19,
in µg/g, exactly as in the spreadsheet: La 0.237, Ce 0.612, Pr 0.095,
Nd 0.467, Sm 0.153, Eu 0.058, Gd 0.2055, Tb 0.0374, Dy 0.254, Ho 0.0566,
Er 0.1655, Tm 0.0255, Yb 0.17, Lu 0.0254. Pm is not measured; the column is
dropped. **new** (`<El>_N`).

### 2.7 Thermometry and trace-element ratios (zircon only)

Formulas checked against the spreadsheet's own concentrations (agreement to
machine precision unless stated). Subscript N = chondrite normalized.

| Group | Column | Formula | Status |
|---|---|---|---|
| Ti-in-zircon | T (°C) | Ferry and Watson (2007), doi:10.1007/s00410-007-0201-0: `T = 4800 / (5.711 - log Ti - log aSiO2 + log aTiO2) - 273.15`; matches the spreadsheet to 2e-4 relative with aSiO2 = 1, aTiO2 = 0.6 | **new**: `UPbRPy.thermometry.Ti_in_zircon_thermometry` exists; needs the two activities as export settings |
| Trace element ratios | Ce/Ce* | `Ce_N / sqrt(La_N Pr_N)` | new |
| | Eu/Eu* | `Eu_N / sqrt(Sm_N Gd_N)` | new |
| | ΣREE | La..Lu (14, no Y), µg/g | new |
| | (Gd/Yb)_N | `Gd_N / Yb_N` | new |
| | Lu/Hf, Nb/Ta, Nb/U, Nb/Th, Th/Y, Hf/Y | mass ratios | new (Th/U is in Composition) |
| Grimes et al. (2015), doi:10.1007/s00410-015-1199-3 | Nb/Yb, Ce/Yb, Gd/Yb, U/Yb | mass ratios | new |
| "Itano '24" | Nb/P, Ce/P, ΣREE+Y | mass ratios; ΣREE+Y in µg/g | new; **citation not verified** (no 2024 Itano paper with these ratios found; the Nb/P-Ce/P granite-type discrimination is Sawaki et al. 2024, Island Arc, doi:10.1111/iar.12539). Footnote names the spreadsheet's label until Yiming confirms |
| Lu et al. (2016, SEG Spec. Publ. 19, 329-347) and Pizarro et al. (2020), doi:10.1016/j.oregeorev.2020.103771 | Dy/Yb, 10,000(Eu/Eu*)/Y, (Ce/Nd)/Y | mass ratios; Eu/Eu* as above | new (Eu/Eu* is not repeated) |
| Roberts et al. (2024), doi:10.1016/j.epsl.2024.118638 | Ce/U | mass ratio | new (Th/U is not repeated) |
| Loucks et al. (2024), doi:10.5382/econgeo.5071 | U_i, Th_i | `U_i = U (f238 exp(λ238 t) + f235 exp(λ235 t))` with f from 238U/235U, `Th_i = Th exp(λ232 t)`, t = preferred date | new; matches the spreadsheet to 0.7 % (U_i) and 1e-5 (Th_i) |
| | 10,000(Eu/Eu*)/Yb_N, 1000(Ce/Nd)_N/Y | as named | new |
| | Ce/√(U_i·Ti), 10,000(Eu/Eu*)/Yb_N + 5 Ce/√(U_i·Ti) | — | **gap**: the spreadsheet's values do not follow from its own Ce, U_i and Ti (off by a factor of about 4.4 for every row, e.g. NIST612_896: 38.04 / sqrt(37.60 x 39.69) = 0.99, sheet 4.33). Left out until the definition is confirmed |

### 2.8 Not carried over

| Spreadsheet content | Why | Status |
|---|---|---|
| Secondary Correction, apparent ages: No bias / Pb bias / U bias (FinalDataTable cols 132-140) | The spreadsheet regresses secondary-standard offsets against 206Pb or 238U count rate and corrects unknowns. UPbRPy shows these bias trends (Bias page, `UPbRPy.bias`) but does not correct dates with them | **gap** (deliberate: a correction model needs its own decision) |
| Secondary Standard Correction Data (cols 142-156) | Inputs to the above | **gap** |
| Strikethrough for discordant, bold for preferred | Formatting carries meaning that is lost in CSV | Replaced by the Notes column (`discordant`) and the explicit preferred-date columns |
| Table S1 laboratory and laser text | Not in the Qtegra export | Footnote fields from export settings (section 5) |

## 3. New engine quantities

All of these live in a new module `UPbRPy/export_table.py` (no Qt), built on
`FinalizedReduction` plus the pipeline's spot means and reference archive.

1. `±2s-sys` for the four systems, the 207/208 common-Pb dates and the
   preferred date (section 2.4).
2. Total Pb: for each Pb isotope channel measured (204, 206, 207, 208), the
   isotope mass fraction in the sample = (sample isotope cps / glass isotope
   cps, internal-standard normalized as for the other elements) x glass Pb
   mass fraction x glass isotope mass abundance. Glass isotope abundances come
   from the glass's `206Pb/204Pb_i`, `207Pb/204Pb_i`, `208Pb/204Pb_i` in the
   reference library (NIST610: 17.045, 15.504, 36.954; Woodhead and Hergt
   2001). 204 is Hg-corrected; where 204 is not measured or not detected it
   contributes zero and the footnote says so. Columns: `Pb` (total) and
   optional `204Pb`, `206Pb`, `207Pb`, `208Pb` (µg/g) and `Pb (208)`.
3. 206Pb cps (plateau mean, baseline subtracted).
4. Discordance with `discordance_definition` and absolute 2s in percent.
5. Chondrite-normalized REE, ratios, Ti-in-zircon, Loucks U_i/Th_i (2.6, 2.7).
6. Notes per analysis (4.5).
7. Per-sample summaries (4.3) and the secondary-standard summary by system
   (4.4).

Export settings (`ExportSettings`, stored in the project file and the
reduction manifest): discordance definition and threshold (default 7/5-6/8,
5 %), aSiO2 (1.0) and aTiO2 (0.6, as in the spreadsheet; footnoted as an
assumption), chosen optional columns, CSV precision (displayed or full), and
the laboratory metadata of section 5 that the data files do not carry.

## 4. Layout

### 4.1 Header

- One title row: "Table <n>. U-Pb isotope ratios, dates and trace-element
  concentrations by LA-ICP-MS: <session name>".
- One row of column groups (merged cells in .xlsx): Analysis, Composition,
  Isotope ratios, Dates (Ma), Common-Pb corrected (Ma), Preferred date (Ma),
  Concentrations (µg/g), Chondrite-normalized, Ti-in-zircon, Trace-element
  ratios, and one group per classification scheme.
- One row of column names with units and numbered footnote markers, for
  example `206Pb/238U ±2s (%)`, `206Pb/238U date ±2s-sys (Ma)³`. In .xlsx the
  isotope masses and the markers are superscript (rich text); in CSV and
  Markdown they are plain (`206Pb/238U`, `[3]`).

The spreadsheet's two name rows (`206Pb` over `238U`) become one.

### 4.2 Row order

1. Unknowns, one block per sample in order of first analysis in the run.
   Each block starts with a sample header row, then its analyses in run order
   (`experiment.spot_starting_times`; reduction frames are alphabetical).
2. Reference materials, in their own block after the unknowns: primary
   standard(s), then secondary standards (in run order of first analysis),
   then reference glasses. Glass rows keep composition and concentrations;
   their isotope-ratio and date cells are blank (a glass U-Pb date has no
   meaning) and the Notes say "concentration reference glass".

### 4.3 Sample header rows

`<sample> (n = <analyses>, <excluded> excluded)` followed, where meaningful,
by an inverse-variance weighted mean of the preferred dates of included
analyses: `weighted mean <system> date = <mean> ± <2s> / ± <2s-sys> Ma
(2s; MSWD = <x>, n = <k>)`. "Meaningful" is decided by rule, not judgment:

- all included analyses share one preferred-date method, and
- the MSWD probability of fit is at least 0.001.

Otherwise the row reads `dispersed: MSWD = <x>, n = <k>; no weighted mean`
(the normal outcome for a detrital sample). The weighted mean uses the
analytical ±2s; ±2s-sys adds the systematic terms of section 2.4 once, to the
mean, not to each analysis. Reference-material header rows instead show the
206/238 and 207/206 weighted means side by side with each system's accepted
date (4.4).

### 4.4 Secondary-standard summary

One row per reference material and system (206Pb/238U, 207Pb/235U,
207Pb/206Pb, 208Pb/232Th): n, weighted mean ±2s, MSWD, probability, the
accepted date of the same system (`UPbRPy.bias.accepted_dates`) and the
offset in Ma and percent. Studio's Secondary standards view and the
reference-material header rows show 206/238 and 207/206 side by side. This
replaces the comparison of the preferred date with a single accepted
concordia age, which made 91500 look 40 Ma old (docs/notes/91500_bias_2026-11-05.md).

### 4.5 Notes column

Short, comma-separated, from fixed vocabulary: `excluded` (unticked in
Studio's final review), `discordant` (|disc| - 2s > threshold), `high 204`
(rule below), `reverse fractionation`, `high common Pb`,
`burn-through`, `high ratio scatter`, `U mean/intercept mismatch`,
`high uncertainty`, `session extrapolation`, `concentration reference glass`.
Excluded analyses stay in the table and are left out of weighted means.
Glass rows carry only `concentration reference glass` (and `excluded`).

`high 204` uses the UPbR26plus criterion, read from the workbook's formulas and
VBA (Samples row 4800, Backgrounds rows 4792 and 4797, and the macro block
"shades red if 204 counts above background"): the plateau-mean mass-204 count
rate minus the analysis's own baseline-mean mass-204 count rate exceeds five
times the standard deviation, over all analyses in the session, of the
per-analysis baseline-mean mass-204 count rates. No Hg correction enters this
test. Decided by Yiming Zhang on 2026-11-06 ("Use high 204"). The engine's
`flag_detectable_204pb` (3 sigma, Hg-corrected) is a different and more
sensitive test; it still drives the automatic flags but not this note.

Different baseline windows can change the session spread of baseline means
and therefore the high-204 threshold. Compare the selected gas-blank
windows when checking this rule against the spreadsheet.

### 4.6 Number formats

Values keep full precision in .xlsx cells; the display format is fixed per
column. CSV and Markdown write the displayed values unless "full precision"
is chosen.

| Quantity | Display |
|---|---|
| Dates, ±2s, ±2s-sys | 0.1 Ma |
| 206/238, 207/206, 208/232 | 5 decimals |
| 207/235, 238/206 | 4 and 3 decimals |
| ±2s in % | 0.01 % below 1 %, else 0.1 % |
| error correlation | 0.00 |
| disc. and ±2s | 0.1 % |
| 206Pb/204Pb ±1s | integer |
| 206Pb cps | integer |
| concentrations, ΣREE | 3 significant figures (0.00 below 10, 0.0 below 100, integer above) |
| chondrite-normalized | as concentrations |
| ratios of elements | 3 significant figures |
| T (°C) | integer |

### 4.7 Formats

- `.xlsx`: one "Table" sheet as above with frozen panes (header rows and the
  Analysis column), merged group cells, number formats, bold sample header
  rows, the footnotes under the table; a "Secondary standards" sheet (4.4);
  the existing audit sheets stay (Methods, Parallel audit, QC metrics,
  Depth profiles).
- `.csv`: one flat header row (`group: name (unit)`), a `Sample` column, no
  header rows inside the data, footnotes in `<name>_footnotes.txt`.
- `.md`: a GitHub-flavoured Markdown table of the chosen columns, sample
  header rows as bold rows, footnotes as a numbered list.

## 5. Footnotes

Generated from the reduction manifest, the reference library and the export
settings, numbered in the order the columns first need them. Fields with no
source in the data are written as "not recorded" and listed in Studio's
export preview so the user can fill them in.

1. Session: name, acquisition date range (first and last acquisition start),
   operator (Qtegra `UserName`), instrument (`machine`), laser system, spot
   diameter, repetition rate, fluence (export settings).
2. Method: per-channel dwell times, sweeps per analysis (median and range),
   baseline and plateau windows (selection method and median durations).
3. Primary standard(s) for 206/238, 207/206, 208/232 with reference values,
   ages and citations with DOIs from `sources.toml`; reference glasses for
   concentrations with citations.
4. Corrections: baseline subtraction and its propagation, downhole model,
   session model, Hg correction of 204, common-Pb methods and the initial Pb
   model (Stacey and Kramers 1975), detector cross-calibration mode and
   factors.
5. Uncertainties: all ±2s are absolute 2s unless marked %; analytical branch
   contents; ±2s-sys contents (section 2.4) with the numbers used.
6. Decay constants (Jaffey et al. 1971; Le Roux and Glendenin 1963) and
   238U/235U (Hiess et al. 2012) with DOIs.
7. Discordance definition and threshold.
8. Preferred date rule (age transition) and weighted-mean rule (4.3).
9. Total Pb definition (3.2); 206Pb/204Pb blank when 204 is not detected.
10. Chondrite values (Sun and McDonough 1989); Ti-in-zircon calibration and
    the activities used; each classification scheme's citation.
11. UPbRPy version and commit, reference-library version and hash,
    data-reduction date.

## 6. Comparison test

`tests/test_export_vs_upbr26plus.py` runs only when the Boise zircon session
folder and a CSV extracted from its Table S2 and S3 are present (paths in
environment variables); it is skipped elsewhere because the data stay on the
laptop. Acceptance: per analysis, ratios and dates within the combined 2s of
the two reductions for at least 95 % of analyses; median relative differences
below 0.5 % for the four ratios; total Pb within 10 % (median) or the
difference explained in the test docstring.
