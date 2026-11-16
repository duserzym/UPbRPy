# Reference-material library

`UPbRPy/reference_materials/` bundles a pinned MIT-licensed snapshot of
[upb-standards](https://github.com/duserzym/upb-standards), revision
`47c15a5998db3ccf74a8886c6412ece16e66df20`. The wide CSV library and the
`standard_archive_legacy` / `standard_archive_csv` exports have been removed.

```python
from UPbRPy import standard_archive, standard_measurements
```

Use `standard_archive` with calibration functions. It is the wide DataFrame
built from TOML records. Use `standard_measurements` to inspect source values
and metadata. `standard_archive_v2` remains an alias for `standard_archive`.
`UPSTREAM.toml` records the revision, commit date, license, file list and
hashes. `LICENSE` contains the upstream MIT text. Refresh using
`.venv/bin/python scripts/sync_upb_standards.py ~/Github/upb-standards`
(default: the pinned revision), or `--rev REV` for another commit. The script
reads Git objects, ignoring working-tree edits. Review library changes in
upb-standards before refreshing the snapshot.

Studio can select a shared TOML library. **Check standards** uses
`UPB_STANDARDS_DIR` or `~/UPbRPy/upb-standards`, compares against the recorded
snapshot revision when available, and does not adopt changed values. The
library tooltip shows the active revision.

## File organization

The snapshot data files are:

- `library.toml`: format version and stable material-file loading order.
- `sources.toml`: shared publication citations, DOI values, and optional local
  literature-library filenames.
- `apatite/`, `carbonate/`, `glass/`, `hematite/`, `rutile/`, `titanite/`,
  and `zircon/`: one TOML file per reference-material record.

Keeping one record per file makes routine edits and source review local. The
same material may therefore occur in many manifest entries; each standard ID
must still be unique across the complete library. Manifest paths preserve the
preferred filename capitalization, while the Python loader resolves their
path components case-insensitively for portability across filesystems.

Each standard contains one or more measurement groups. A group defines the
shared role, unit, status, source, and notes; its `values` table contains the
actual source quantities. An individual value becomes an inline table only
when it needs its own uncertainty or a metadata override:

```toml
[standards."KNW"]
aliases = []

[[standards."KNW".measurements]]
role = "concentration"
unit = "ug/g"
status = "recommended"
source = "meng2024"
notes = "Recommended elemental mass fraction assigned to the {quantity} measurement channel. Sufficiently homogeneous."

[standards."KNW".measurements.values]
"51V" = { value = 6622, uncertainty = 625, uncertainty_type = "expanded_95" }
"52Cr" = { value = 1762, uncertainty = 149, uncertainty_type = "expanded_95" }
```

The `{quantity}` placeholder is expanded when the normalized DataFrame is
built. It lets related values share a readable note without losing the channel
name in downstream tables.

Concentration quantities retain the isotope-channel name used by the source,
such as `29Si`, but their values are total elemental mass fractions in
micrograms per gram; they are not multiplied by isotope abundance. At run
time, concentration calibration matches measured and archived channels by
element symbol. A method measuring `151Eu` can therefore use a documented
`153Eu` concentration without duplicating Eu in the library. If a run acquires
two masses of the same element, calibration prefers a measured mass explicitly
documented for all calibration standards and otherwise uses the first
resolvable mass. The result is reported once under the element name.

The optional standard-level `description` and `origin` fields preserve import
context without adding repeated columns to the normalized measurement table.

## Ratio conventions

Initial-Pb inputs are stored in the form directly reported by the source. When
all three canonical common-Pb ratios are available, they are:

- `206Pb/204Pb_i`
- `207Pb/204Pb_i`
- `208Pb/204Pb_i`

The loader calculates reciprocals and cross-ratios when the required inputs
are present. It likewise derives reciprocal or algebraically linked
radiogenic and total ratios. A directly documented ratio always takes
precedence over a calculated value. Where a paper recommends an age rather
than a single pooled ratio, the appropriate radiogenic ratio is calculated
with the decay constants used by UPbRPy.

NIST glass isotope-ratio entries retain the legacy star-column names required
by the present fractionation API. Their notes identify this convention so they
are not confused with pure radiogenic mineral ratios.

## Editing rules

Make data edits in upb-standards, then refresh the committed snapshot here.
For a separately selected local TOML library, the same format rules apply.

1. Add or edit only documented source quantities. Do not add reciprocal or
   age-derived convenience ratios.
2. Define one standard in one TOML file under its material directory, then add
   that relative path to `library.toml`.
3. Add a citation to `sources.toml` once and refer to its stable source ID.
4. Keep one value per standard and quantity; duplicates are rejected.
5. Record all available decimal places from the source.
6. Never enter an unknown uncertainty as zero. Omit both uncertainty fields.
7. Use `provisional` when a value still needs primary-source review.
8. Run `pytest -q tests/test_standards.py` after editing. The loader validates
   document versions, supported fields, sources, roles, units, statuses,
   uncertainty semantics, duplicates, and material assignments.

For one-off migration or auditing, `load_standard_measurements(path)` still
accepts the former normalized CSV layout when `path` ends in `.csv`. TOML is
the packaged source of truth.

## Iolite imports

The converter can reproduce all 29 JSON files from the supplied iolite
reference-material directory. The reviewed manifest currently retains 24 of
those as separate provisional records after removing blank-name and duplicate
unclassified entries. Imported IDs begin with
`IOLITE_`; they are never merged into a curated record or into another iolite
record. For example, curated `91500` and `IOLITE_Z_91500` remain independent.
This is intentional because files can represent different compilations,
analytical purposes, or revisions.

The reproducible converter is
`scripts/import_iolite_reference_materials.py`. It applies only these explicit
normalizations:

- `ppm` values become `ug/g` without a numerical change.
- `wtpc` values are multiplied by 10,000 and stored as `ug/g`.
- elemental concentrations are assigned to the isotope-channel labels used by
  UPbRPy's concentration calibration, without isotope-abundance scaling.
- ages, isotope ratios, delta values, and epsilon values receive distinct
  semantic roles and units.
- a positive reported uncertainty is retained as `reported_unspecified`
  because the JSON schema does not state whether it is 1 SD, 2 SD, or another
  interval.
- exact duplicate result rows are collapsed; the import contained one such
  duplicate Zr entry in `G_NIST610.json`.

Zero values are retained rather than guessed to be missing, because some may
be meaningful while others are clearly placeholders. Suspect records remain
`provisional` and carry their iolite origin. Source-free files cite the
explicit `iolite_unattributed` registry entry. These values should not be
promoted to `recommended` until they have been checked against a primary
publication.

## Principal sources added in this revision

- Deer, Howie, and Zussman (2013), *An Introduction to the Rock-Forming
  Minerals*, Tables 2, 4, 51, and 60, for average mineral compositions used by
  internal elemental normalization.
- Meng et al. (2024), DOI `10.1039/D4JA00190G`, Tables 3 and 5, for KNW rutile
  age and trace-element reference values.
- Axelsson et al. (2018), DOI `10.1111/ggr.12213`, Tables 3 and 5 and the
  conclusions, for R632 trace-element values and the recommended combined age.
- Kennedy et al. (2023), DOI `10.1111/ggr.12477`, for AFB1, BR5, and OL2
  ID-TIMS ages and common-Pb compositions.
- Apen et al. (2022), DOI `10.1016/j.chemgeo.2021.120689`, for the MAD-UCSB
  total Pb-U isochron age and initial-Pb ratio.
- Jochum et al. (2011), Woodhead and Hergt (2001), and the GeoReM publication
  for glass concentration and Pb-isotope reference values.
- Thomson et al. (2012), DOI `10.1029/2011GC003928`, for the imported MAD
  apatite record.
- Schoene and Bowring (2006), DOI `10.1007/s00410-006-0077-4`, for the
  imported McClure Mountain apatite record.
- Woodhead and Hergt (2005), DOI
  `10.1111/j.1751-908X.2005.tb00891.x`, in the imported zircon Hf-isotope
  compilations.

Unpublished lab archive values are marked provisional and identified in the
source registry. Published values retain their source locators, uncertainty
conventions and audit notes from upb-standards.

Legacy-only names `Zirconia`, `MADJCU`, `MOA`, `MOM`, `TR1`, `TC`, `RTFT`,
and `RC` are not available. They should return only through an upstream
record with identifiable provenance and a value-level review.
