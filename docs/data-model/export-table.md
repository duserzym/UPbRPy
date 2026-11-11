# Export table

!!! abstract "In short"
    The export table is the publication-ready results table that UPbRPy Studio
    writes as Excel, CSV and Markdown. This page lists every column, the
    settings that control it, and the files written.

Implementation: `UPbRPy/export_table.py` (quantities, footnotes and file writers),
`UPbRPy/reproducibility.py` (bundles), `GUI/export_dialog.py` (Studio dialog).
The [publication-table specification](https://github.com/duserzym/UPbRPy/blob/main/docs/export_table_spec.md) defines the
scientific quantities and display policy. Engine code has no Qt dependency.

`build_export_table(pipeline, settings=None, *, excluded_spots=())` returns an
`ExportTable` with full-precision `rows`, a column catalogue, `sample_summaries`,
per-system `secondary_summary`, `settings` and provenance `context`.
`visible_columns()` selects enabled, material-applicable columns containing a
value; entirely blank columns are suppressed. Excluded spots remain in `rows`
and are omitted from summaries. Unknown sample blocks precede primary standards,
secondary standards and concentration glasses, each in acquisition order.
Glass ratio/date cells are blank.

## Columns

The catalogue below covers all supported concentration elements. A session
exports only populated, applicable columns. Zircon-only groups are
Ti-in-zircon, Trace-element ratios and the classification schemes. Labels
acquire units and numbered markers at rendering time. Footnote IDs are stable;
numbers are assigned to global notes and then in first visible-column use order.

Source quantity names refer to the analytical parallel results unless stated
otherwise. Concentrations use the concentration stage and normalization;
Pb isotope fractions and total Pb use independently calibrated isotope signals.
`_2e_sys` combines analytical/session calibration, reference, physical and
long-term components in date space. Optional columns start disabled.

| Key | Group | Label | Unit | Default/optional | Source quantity | Footnote ids |
| --- | --- | --- | --- | --- | --- | --- |
| spot_name | Analysis | Analysis |  | Default | spot identity | session, method |
| notes | Analysis | Notes |  | Default | exclusion, discordance, UPbR26plus high-204 rule and QC flags | notes, glasses |
| U | Composition | U | µg/g | Default | U | — |
| Th | Composition | Th | µg/g | Default | Th | — |
| Pb | Composition | Pb | µg/g | Default | sum of calibrated 204Pb, 206Pb, 207Pb, 208Pb mass fractions | total_pb |
| 204Pb | Composition | 204Pb | µg/g | Optional | calibrated isotope mass fraction | total_pb |
| 206Pb | Composition | 206Pb | µg/g | Optional | calibrated isotope mass fraction | total_pb |
| 207Pb | Composition | 207Pb | µg/g | Optional | calibrated isotope mass fraction | total_pb |
| 208Pb | Composition | 208Pb | µg/g | Optional | calibrated isotope mass fraction | total_pb |
| Pb_208 | Composition | Pb (208) | µg/g | Optional | 208Pb-based elemental concentration | total_pb |
| Th/U | Composition | Th/U |  | Default | Th / U mass concentration | — |
| 206Pb_cps | Composition | 206Pb | cps | Default | baseline-subtracted plateau mean 206Pb cps | — |
| 206Pb/204Pb | Composition | 206Pb/204Pb |  | Default | 206Pb/204Pb | pb204 |
| 206Pb/204Pb_1s | Composition | 206Pb/204Pb ±1s |  | Default | 206Pb/204Pb_2e / 2 | pb204 |
| 208Pb/232Th | Isotope ratios | 208Pb/232Th |  | Default | 208Pb/232Th | standards, corrections |
| 208Pb/232Th_2e_no_calibration% | Isotope ratios | 208Pb/232Th ±2s | % | Default | 100 × analytical 2s / abs(208Pb/232Th) | uncertainty |
| 207Pb/235U | Isotope ratios | 207Pb/235U |  | Default | 207Pb/235U | standards, corrections |
| 207Pb/235U_2e_no_calibration% | Isotope ratios | 207Pb/235U ±2s | % | Default | 100 × analytical 2s / abs(207Pb/235U) | uncertainty |
| 206Pb/238U | Isotope ratios | 206Pb/238U |  | Default | 206Pb/238U | standards, corrections |
| 206Pb/238U_2e_no_calibration% | Isotope ratios | 206Pb/238U ±2s | % | Default | 100 × analytical 2s / abs(206Pb/238U) | uncertainty |
| err_corr_no_calibration | Isotope ratios | ρ 206/238–207/235 |  | Default | err_corr_no_calibration | — |
| 238U/206Pb | Isotope ratios | 238U/206Pb |  | Default | 238U/206Pb | standards, corrections |
| 238U/206Pb_2e_no_calibration% | Isotope ratios | 238U/206Pb ±2s | % | Default | 100 × analytical 2s / abs(238U/206Pb) | uncertainty |
| 207Pb/206Pb | Isotope ratios | 207Pb/206Pb |  | Default | 207Pb/206Pb | standards, corrections |
| 207Pb/206Pb_2e_no_calibration% | Isotope ratios | 207Pb/206Pb ±2s | % | Default | 100 × analytical 2s / abs(207Pb/206Pb) | uncertainty |
| 208Pb/206Pb | Isotope ratios | 208Pb/206Pb |  | Optional | 1 / 206Pb/208Pb | standards, corrections |
| 208Pb/206Pb_2e_no_calibration% | Isotope ratios | 208Pb/206Pb ±2s | % | Optional | 100 × 206Pb/208Pb_2e / abs(206Pb/208Pb) | uncertainty |
| tw_rho_no_calibration | Isotope ratios | ρ Tera-Wasserburg |  | Optional | tw_rho_no_calibration | — |
| 208Pb/232Th_age | Dates | 208Pb/232Th date | Ma | Default | 208Pb/232Th_age | — |
| 208Pb/232Th_age_2e_no_calibration | Dates | 208Pb/232Th date ±2s | Ma | Default | 208Pb/232Th_age_2e_no_calibration | uncertainty |
| 208Pb/232Th_age_2e_sys | Dates | 208Pb/232Th date ±2s-sys | Ma | Default | 208Pb/232Th_age combined systematic 2s | uncertainty, constants |
| 207Pb/206Pb_age | Dates | 207Pb/206Pb date | Ma | Default | 207Pb/206Pb_age | — |
| 207Pb/206Pb_age_2e_no_calibration | Dates | 207Pb/206Pb date ±2s | Ma | Default | 207Pb/206Pb_age_2e_no_calibration | uncertainty |
| 207Pb/206Pb_age_2e_sys | Dates | 207Pb/206Pb date ±2s-sys | Ma | Default | 207Pb/206Pb_age combined systematic 2s | uncertainty, constants |
| 207Pb/235U_age | Dates | 207Pb/235U date | Ma | Default | 207Pb/235U_age | — |
| 207Pb/235U_age_2e_no_calibration | Dates | 207Pb/235U date ±2s | Ma | Default | 207Pb/235U_age_2e_no_calibration | uncertainty |
| 207Pb/235U_age_2e_sys | Dates | 207Pb/235U date ±2s-sys | Ma | Default | 207Pb/235U_age combined systematic 2s | uncertainty, constants |
| 206Pb/238U_age | Dates | 206Pb/238U date | Ma | Default | 206Pb/238U_age | — |
| 206Pb/238U_age_2e_no_calibration | Dates | 206Pb/238U date ±2s | Ma | Default | 206Pb/238U_age_2e_no_calibration | uncertainty |
| 206Pb/238U_age_2e_sys | Dates | 206Pb/238U date ±2s-sys | Ma | Default | 206Pb/238U_age combined systematic 2s | uncertainty, constants |
| disc_pct | Dates | Discordance | % | Default | 100 × (1 − t68 / t75 or t76), per setting | discordance |
| disc_2s_pct | Dates | Discordance ±2s | % | Default | absolute analytical discordance 2s, with 68/75 correlation | — |
| 207Pb_corrected_age | Common-Pb corrected | 207-method 206Pb*/238U* date | Ma | Default | 207Pb_corrected_age | corrections, uncertainty |
| 207Pb_corrected_age_2e_no_calibration | Common-Pb corrected | 207-method 206Pb*/238U* date ±2s | Ma | Default | 207Pb_corrected_age_2e_no_calibration | corrections, uncertainty |
| 207Pb_corrected_age_2e_sys | Common-Pb corrected | 207-method 206Pb*/238U* date ±2s-sys | Ma | Default | 207Pb_corrected_age combined systematic 2s | corrections, uncertainty |
| 208Pb_corrected_age | Common-Pb corrected | 208-method 206Pb*/238U* date | Ma | Default | 208Pb_corrected_age | corrections, uncertainty |
| 208Pb_corrected_age_2e_no_calibration | Common-Pb corrected | 208-method 206Pb*/238U* date ±2s | Ma | Default | 208Pb_corrected_age_2e_no_calibration | corrections, uncertainty |
| 208Pb_corrected_age_2e_sys | Common-Pb corrected | 208-method 206Pb*/238U* date ±2s-sys | Ma | Default | 208Pb_corrected_age combined systematic 2s | corrections, uncertainty |
| preferred_date | Preferred date | Preferred date | Ma | Default | preferred_date | preferred, uncertainty |
| preferred_date_2e | Preferred date | Preferred date ±2s | Ma | Default | preferred method analytical _2e_no_calibration | preferred, uncertainty |
| preferred_date_2e_sys | Preferred date | Preferred date ±2s-sys | Ma | Default | preferred method _2e_sys | preferred, uncertainty |
| preferred_date_method | Preferred date | Method |  | Default | preferred_date_method | preferred, uncertainty |
| P | Concentrations | P | µg/g | Default | P from concentration stage | — |
| P_95CI | Concentrations | P 95% CI | µg/g | Optional | P_95CI from concentration stage | uncertainty |
| Ti | Concentrations | Ti | µg/g | Default | Ti from concentration stage | — |
| Ti_95CI | Concentrations | Ti 95% CI | µg/g | Optional | Ti_95CI from concentration stage | uncertainty |
| Y | Concentrations | Y | µg/g | Default | Y from concentration stage | — |
| Y_95CI | Concentrations | Y 95% CI | µg/g | Optional | Y_95CI from concentration stage | uncertainty |
| Zr | Concentrations | Zr | µg/g | Default | Zr from concentration stage | — |
| Zr_95CI | Concentrations | Zr 95% CI | µg/g | Optional | Zr_95CI from concentration stage | uncertainty |
| Nb | Concentrations | Nb | µg/g | Default | Nb from concentration stage | — |
| Nb_95CI | Concentrations | Nb 95% CI | µg/g | Optional | Nb_95CI from concentration stage | uncertainty |
| La | Concentrations | La | µg/g | Default | La from concentration stage | — |
| La_95CI | Concentrations | La 95% CI | µg/g | Optional | La_95CI from concentration stage | uncertainty |
| Ce | Concentrations | Ce | µg/g | Default | Ce from concentration stage | — |
| Ce_95CI | Concentrations | Ce 95% CI | µg/g | Optional | Ce_95CI from concentration stage | uncertainty |
| Pr | Concentrations | Pr | µg/g | Default | Pr from concentration stage | — |
| Pr_95CI | Concentrations | Pr 95% CI | µg/g | Optional | Pr_95CI from concentration stage | uncertainty |
| Nd | Concentrations | Nd | µg/g | Default | Nd from concentration stage | — |
| Nd_95CI | Concentrations | Nd 95% CI | µg/g | Optional | Nd_95CI from concentration stage | uncertainty |
| Sm | Concentrations | Sm | µg/g | Default | Sm from concentration stage | — |
| Sm_95CI | Concentrations | Sm 95% CI | µg/g | Optional | Sm_95CI from concentration stage | uncertainty |
| Eu | Concentrations | Eu | µg/g | Default | Eu from concentration stage | — |
| Eu_95CI | Concentrations | Eu 95% CI | µg/g | Optional | Eu_95CI from concentration stage | uncertainty |
| Gd | Concentrations | Gd | µg/g | Default | Gd from concentration stage | — |
| Gd_95CI | Concentrations | Gd 95% CI | µg/g | Optional | Gd_95CI from concentration stage | uncertainty |
| Tb | Concentrations | Tb | µg/g | Default | Tb from concentration stage | — |
| Tb_95CI | Concentrations | Tb 95% CI | µg/g | Optional | Tb_95CI from concentration stage | uncertainty |
| Dy | Concentrations | Dy | µg/g | Default | Dy from concentration stage | — |
| Dy_95CI | Concentrations | Dy 95% CI | µg/g | Optional | Dy_95CI from concentration stage | uncertainty |
| Ho | Concentrations | Ho | µg/g | Default | Ho from concentration stage | — |
| Ho_95CI | Concentrations | Ho 95% CI | µg/g | Optional | Ho_95CI from concentration stage | uncertainty |
| Er | Concentrations | Er | µg/g | Default | Er from concentration stage | — |
| Er_95CI | Concentrations | Er 95% CI | µg/g | Optional | Er_95CI from concentration stage | uncertainty |
| Tm | Concentrations | Tm | µg/g | Default | Tm from concentration stage | — |
| Tm_95CI | Concentrations | Tm 95% CI | µg/g | Optional | Tm_95CI from concentration stage | uncertainty |
| Yb | Concentrations | Yb | µg/g | Default | Yb from concentration stage | — |
| Yb_95CI | Concentrations | Yb 95% CI | µg/g | Optional | Yb_95CI from concentration stage | uncertainty |
| Lu | Concentrations | Lu | µg/g | Default | Lu from concentration stage | — |
| Lu_95CI | Concentrations | Lu 95% CI | µg/g | Optional | Lu_95CI from concentration stage | uncertainty |
| Hf | Concentrations | Hf | µg/g | Default | Hf from concentration stage | — |
| Hf_95CI | Concentrations | Hf 95% CI | µg/g | Optional | Hf_95CI from concentration stage | uncertainty |
| Ta | Concentrations | Ta | µg/g | Default | Ta from concentration stage | — |
| Ta_95CI | Concentrations | Ta 95% CI | µg/g | Optional | Ta_95CI from concentration stage | uncertainty |
| Th_conc_ppm | Concentrations | Th | µg/g | Default | Th concentration | — |
| Th_95CI | Concentrations | Th 95% CI | µg/g | Optional | Th_95CI from concentration stage | uncertainty |
| U_conc_ppm | Concentrations | U | µg/g | Default | U concentration | — |
| U_95CI | Concentrations | U 95% CI | µg/g | Optional | U_95CI from concentration stage | uncertainty |
| La_N | Chondrite-normalized | La |  | Default | La / Sun–McDonough chondrite value | chondrite |
| Ce_N | Chondrite-normalized | Ce |  | Default | Ce / Sun–McDonough chondrite value | chondrite |
| Pr_N | Chondrite-normalized | Pr |  | Default | Pr / Sun–McDonough chondrite value | chondrite |
| Nd_N | Chondrite-normalized | Nd |  | Default | Nd / Sun–McDonough chondrite value | chondrite |
| Sm_N | Chondrite-normalized | Sm |  | Default | Sm / Sun–McDonough chondrite value | chondrite |
| Eu_N | Chondrite-normalized | Eu |  | Default | Eu / Sun–McDonough chondrite value | chondrite |
| Gd_N | Chondrite-normalized | Gd |  | Default | Gd / Sun–McDonough chondrite value | chondrite |
| Tb_N | Chondrite-normalized | Tb |  | Default | Tb / Sun–McDonough chondrite value | chondrite |
| Dy_N | Chondrite-normalized | Dy |  | Default | Dy / Sun–McDonough chondrite value | chondrite |
| Ho_N | Chondrite-normalized | Ho |  | Default | Ho / Sun–McDonough chondrite value | chondrite |
| Er_N | Chondrite-normalized | Er |  | Default | Er / Sun–McDonough chondrite value | chondrite |
| Tm_N | Chondrite-normalized | Tm |  | Default | Tm / Sun–McDonough chondrite value | chondrite |
| Yb_N | Chondrite-normalized | Yb |  | Default | Yb / Sun–McDonough chondrite value | chondrite |
| Lu_N | Chondrite-normalized | Lu |  | Default | Lu / Sun–McDonough chondrite value | chondrite |
| Ti_in_zircon_C | Ti-in-zircon | T | °C | Default | Ferry–Watson temperature from Ti, aSiO2, aTiO2 | ti_in_zircon |
| Ce/Ce* | Trace-element ratios | Ce/Ce* |  | Default | Ce_N / sqrt(La_N × Pr_N) | chondrite |
| Eu/Eu* | Trace-element ratios | Eu/Eu* |  | Default | Eu_N / sqrt(Sm_N × Gd_N) | chondrite |
| sum_REE | Trace-element ratios | ΣREE | µg/g | Default | sum of La–Lu concentrations excluding Pm | chondrite |
| (Gd/Yb)_N | Trace-element ratios | (Gd/Yb)_N |  | Default | Gd_N / Yb_N | chondrite |
| Lu/Hf | Trace-element ratios | Lu/Hf |  | Default | Lu/Hf of µg/g concentrations | chondrite |
| Nb/Ta | Trace-element ratios | Nb/Ta |  | Default | Nb/Ta of µg/g concentrations | chondrite |
| Nb/U | Trace-element ratios | Nb/U |  | Default | Nb/U of µg/g concentrations | chondrite |
| Nb/Th | Trace-element ratios | Nb/Th |  | Default | Nb/Th of µg/g concentrations | chondrite |
| Th/Y | Trace-element ratios | Th/Y |  | Default | Th/Y of µg/g concentrations | chondrite |
| Hf/Y | Trace-element ratios | Hf/Y |  | Default | Hf/Y of µg/g concentrations | chondrite |
| Nb/Yb | Grimes et al. (2015) | Nb/Yb |  | Default | Nb/Yb of µg/g concentrations | grimes2015 |
| Ce/Yb | Grimes et al. (2015) | Ce/Yb |  | Default | Ce/Yb of µg/g concentrations | grimes2015 |
| Gd/Yb | Grimes et al. (2015) | Gd/Yb |  | Default | Gd/Yb of µg/g concentrations | grimes2015 |
| U/Yb | Grimes et al. (2015) | U/Yb |  | Default | U/Yb of µg/g concentrations | grimes2015 |
| Nb/P | Itano '24 | Nb/P |  | Default | Nb/P of µg/g concentrations | itano24 |
| Ce/P | Itano '24 | Ce/P |  | Default | Ce/P of µg/g concentrations | itano24 |
| sum_REE_Y | Itano '24 | ΣREE+Y | µg/g | Default | sum_REE + Y | itano24 |
| Dy/Yb | Lu et al. (2016); Pizarro et al. (2020) | Dy/Yb |  | Default | Dy/Yb of µg/g concentrations | lu_pizarro |
| 10000Eu/Eu*/Y | Lu et al. (2016); Pizarro et al. (2020) | 10,000(Eu/Eu*)/Y |  | Default | 10000 × Eu/Eu* / Y | lu_pizarro |
| (Ce/Nd)/Y | Lu et al. (2016); Pizarro et al. (2020) | (Ce/Nd)/Y |  | Default | Ce / Nd / Y | lu_pizarro |
| Ce/U | Roberts et al. (2024) | Ce/U |  | Default | Ce/U of µg/g concentrations | roberts2024 |
| U_i | Loucks et al. (2024) | U_i | µg/g | Default | U restored for decay at preferred_date | loucks2024 |
| Th_i | Loucks et al. (2024) | Th_i | µg/g | Default | Th restored for decay at preferred_date | loucks2024 |
| 10000Eu/Eu*/Yb_N | Loucks et al. (2024) | 10,000(Eu/Eu*)/Yb_N |  | Default | 10000 × Eu/Eu* / Yb_N | loucks2024 |
| 1000(Ce/Nd)_N/Y | Loucks et al. (2024) | 1000(Ce/Nd)_N/Y |  | Default | 1000 × Ce_N / Nd_N / Y | loucks2024 |

## ExportSettings fields

Frozen, validated settings serialize with `to_dict()` and
`ExportSettings.from_dict(values)`; unknown future keys are ignored. Sequence
fields become JSON arrays. Studio stores them at `workspace.export_settings`
in the `.upbr` project. Old projects without that key use defaults.

| Name | Default | Meaning |
| --- | --- | --- |
| discordance_definition | 7/5-6/8 | 7/5-6/8 or 7/6-6/8 date definition |
| discordance_threshold_percent | 5.0 | Flag when absolute discordance minus 2s exceeds this nonnegative percentage |
| a_sio2 | 1.0 | Positive silica activity for Ti-in-zircon |
| a_tio2 | 0.6 | Positive titania activity for Ti-in-zircon |
| optional_columns | Empty | Enabled optional column keys |
| excluded_columns | Empty | Disabled default column keys |
| csv_full_precision | False | Write full numeric precision in CSV instead of displayed values |
| laboratory | Empty | Laboratory name |
| laser_system | Empty | Laser system |
| spot_diameter_um | Empty | Spot diameter in µm |
| repetition_rate_hz | Empty | Laser repetition rate in Hz |
| fluence_j_cm2 | Empty | Laser fluence in J/cm² |
| operator | Empty | Operator override; otherwise acquisition users |
| table_title | Empty | Custom title; blank uses Table <n>. U-Pb isotope ratios, dates and trace-element concentrations by LA-ICP-MS: <session> |

## Files written

`write_export_xlsx(table, path) -> Path` writes a `Table` sheet and a
`Reference materials` sheet. The latter contains long summaries for all four
systems and the wide 206/238 + 207/206 view. Values retain full precision;
formats control display. The Table sheet has rich isotope/footnote superscripts,
merged groups and sample summaries, a reference-material block, frozen headings
and Analysis column, wrapped notes/footnotes, and landscape printing fit to width.

`write_export_csv(table, path) -> list[Path]` writes a flat `Sample` column
plus visible columns headed `Group: name (unit) [n]`, with no in-table sample
headers. It also writes `<stem>_samples.csv` and `<stem>_footnotes.txt`.
CSV and its companion files use UTF-8 with BOM; no DataFrame index is written.
Missing numeric values are blank. CSV uses displayed values by default.

`write_export_markdown(table, path) -> Path` writes visible columns, bold
sample/section rows, escaped pipes and numbered footnotes. Heading markers use
`<sup>n</sup>`. Markdown always uses displayed values.
`format_value(column, value, *, full_precision=False) -> str` implements the
shared display policy. `export_table_preview(table, n_rows=8) -> dict` returns
`groups`, `headings` (plain), `rows` (displayed), `row_kinds`, `footnotes`, and
`missing_fields` (field names mapped to footnote numbers).
The row limit includes sample and section rows. The preview does not run stages.
`update_export_settings(table, settings) -> ExportTable` returns changed settings,
discordance, temperature and notes over existing quantities, without engine calls.

## Reduction bundle and manifest

`export_reduction_bundle(pipeline, output_directory, *, export_settings=None,
formats=("xlsx", "csv", "md"), excluded_spots=(), ...)` adds
`<session>_results_table.xlsx`, `.csv`, `.md` and the CSV companions to the
existing audit files. `file_stem` overrides the default basename;
`include_audit_files=False` writes only the chosen table formats. An already
prepared `table` can be supplied by Studio to avoid rebuilding the quantities.
The existing `UPbRPy_publication_results.xlsx` remains the audit workbook,
with its companion CSVs, selection/spot/depth files and replay notebook.

The reduction manifest records the settings at `metadata.export_settings`
and exclusions at `metadata.excluded_spots`. These choices describe this export;
they do not modify the reduction pipeline's original configuration. Disabling
audit files also omits the manifest and replay notebook. Legacy cached audit
frames without current publication quantities export identities and exclusions
with an explicit unavailability footnote, rather than inferred dates. Empty
audit results produce only the existing audit outputs, with no publication table.

Footnotes report session/acquisition and laboratory metadata, method, references,
corrections, uncertainty components, constants, software provenance, and the
visible columns' definitions. Missing metadata is explicitly “not recorded”.
Footnote numbers are regenerated after column changes and are shared by the
preview and all file writers.
