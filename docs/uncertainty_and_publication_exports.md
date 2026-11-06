# Uncertainty choices and publication exports

UPbRPy keeps analytical, calibration-inclusive, and optional systematic
uncertainties as separate result branches. The GUI and reproducibility
notebook serialize the same `UncertaintySettings`, so choosing an uncertainty
scope never changes the reported central ratio or date.

Studio's **Export results…** also writes a grouped publication table in XLSX,
CSV and Markdown. Its analytical ±2s and ±2s-sys columns have explicit,
separate definitions and numbered provenance footnotes. See the
[publication-table contract](export_table_spec.md) and
[export data model](data-model/export-table.md) for column sources, settings
and output files. The existing audit workbook remains available by default.

## Analytical uncertainty

Raw isotope-ratio errors include Poisson counting statistics and the
uncertainty of each spot's measured baseline mean. The baseline component is
shared by all sweeps from that spot, so it is added once to a spot-mean
uncertainty rather than divided by the square root of the number of on-peak
sweeps. The mass-204 path also propagates uncertainty in the baseline-derived
204Hg/202Hg scale.

The `_2e_no_calibration` branch contains analytical uncertainty. The
`_2e_with_calibration` branch additionally contains the residual session
calibration scatter. Downhole-fit uncertainty is treated as systematic and is
not independently added to every sweep.

## Optional systematic date components

The decay-constant and 238U/235U switches are independent. Their individual
2-sigma contributions and their combined contribution are retained in named
columns. When either switch is enabled, the selected-total uncertainty adds
that component in quadrature to the requested analytical or
calibration-inclusive branch.

The default 238U/235U value is 137.818 ± 0.045 (2SD; Hiess et al., 2012,
https://doi.org/10.1126/science.1215507). The uranium decay constants are
lambda-238 = 1.55125e-10 yr-1 ± 0.11% (2SD) and lambda-235 = 9.8485e-10
yr-1 ± 0.14% (2SD; Jaffey et al., 1971,
https://doi.org/10.1103/PhysRevC.4.1889). The thorium decay constant is
lambda-232 = 4.9475e-11 yr-1 ± 0.042e-11 yr-1 (2SD; Le Roux and Glendenin,
1963). The publication workbook records
the central values, one-sigma uncertainties, and references in its Methods
worksheet even when the corresponding systematic uncertainty is not selected.

Long-term reproducibility is entered as relative 1SD in calibrated-ratio
space, then converted to date units with the local ratio--date derivative. A
user may supply a laboratory long-term value. The secondary-standard option
uses the largest absolute weighted-mean offset from the accepted date as a
conservative session proxy; it should not be represented as a true long-term
estimate unless it is based on repeated measurements across many sessions.

## Initial common-Pb model uncertainty

Stacey and Kramers (1975,
https://doi.org/10.1016/0012-821X(75)90088-6) define a two-stage composition
model, but do not prescribe a universal uncertainty or covariance for that
model. UPbRPy therefore labels every convention explicitly:

| Preset | Relative 1SD for 206/204, 207/204, 208/204 | Correlations | Meaning |
| --- | --- | --- | --- |
| Boise working | 0.61%, 0.52%, 0.63% | 0.98 for each pair | Laboratory working convention supplied by M. Schmitz; not a literature default |
| Horstwood example | 0.80%, 0.32%, 0.74% | not specified; stored as zero | Values printed for the common/blank Pb example in Horstwood et al. (2016), not a universal Stacey--Kramers uncertainty |
| ET_Redux default | 1%, 1%, 1% | 0.5 for each pair | Generic editable software defaults |

The full 206/204--207/204--208/204 covariance matrix is propagated into the
derived common 207/206 and 206/208 compositions used by the conventional 207-
and 208-method corrections. A separately measured cogenetic common-Pb
composition should use its measured uncertainties and correlations instead of
a generic preset.

## Horstwood Table 4-style export

The publication workbook contains grouped headers, isotope-aware number
formats, frozen identifiers, screening flags, separate uncertainty branches,
a methods/provenance sheet, and detailed footnotes. A separate optional spot
concentration sheet places isotope-agnostic elemental concentrations beside
single-analysis dates. The depth-profile sheet can independently include or
omit sweep-wise concentrations.

Single-analysis results are called **dates** in publication-facing tables and
GUI result views. Legacy `*_age` field names remain only in the parallel audit
data for notebook compatibility. Concordia graphics use true 2-sigma ellipse
semiaxes; for a bivariate normal distribution this is about 86.5% joint
coverage, not a 95% joint-confidence ellipse.
