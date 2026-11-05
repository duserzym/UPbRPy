# Reference-material TOML

Implementation: [standards.py](https://github.com/duserzym/UPbRPy/blob/main/UPbRPy/standards.py).
Packaged examples: [library.toml](https://github.com/duserzym/UPbRPy/blob/main/UPbRPy/reference_materials/library.toml),
[sources.toml](https://github.com/duserzym/UPbRPy/blob/main/UPbRPy/reference_materials/sources.toml),
[zircon/91500.toml](https://github.com/duserzym/UPbRPy/blob/main/UPbRPy/reference_materials/zircon/91500.toml).

`UPbRPy.standards.load_standard_measurements(path=None)` loads the packaged
`UPbRPy/reference_materials/` by default. `path` can be another library
directory, its `library.toml`, or a former normalized long-form CSV.
`build_standard_archive(measurements=None, *, include_derived=True)`
constructs the wide calibration table. The reference-library format version
is **2**, independently of this data-model document's version.

## library.toml

| Key | Required/optional | Type | Unit | Example | Meaning / without it |
| --- | --- | --- | --- | --- | --- |
| format_version | Required | Integer, must equal 2 | — | `2` | Version check fails if absent or different |
| version | Optional | String | — | `"2026-11-05"` (illustrative) | Human library version; identity uses `format-2` if absent and appends a hash suffix |
| sources | Required | Nonempty string | — | `"sources.toml"` | Registry filename, relative to library root; missing is an error |
| material_files | Required | Nonempty array of TOML filenames | — | `"zircon/91500.toml"` | Manifest order controls stable archive row order; unlisted files are not loaded |

Resource lookup prefers exact spelling, then matches each component
case-insensitively. Ambiguous case-only matches or missing files fail.
The manifest is not a glob: material directory names do not determine the
material value, which is declared inside each file.

## Material files

Examples below are from `zircon/91500.toml` unless marked illustrative.

| Key / table | Required/optional | Type | Unit | Example | Meaning / without it |
| --- | --- | --- | --- | --- | --- |
| format_version | Required | Integer equal to 2 | — | `2` | Missing/different version fails |
| material | Required | Nonempty string | — | `"zircon"` | Material attached to every measurement; missing fails |
| standards | Required | Nonempty table | — | `[standards."91500"]` | Names are standard identifiers; missing fails |
| standards.NAME.aliases | Optional | Array of nonblank strings | — | `[]` | Alternate names; defaults to empty array; normalized as semicolon-separated text |
| standards.NAME.description | Optional | String | — | `"Reference zircon"` (illustrative) | Validated as text, not included in normalized measurement rows; default empty |
| standards.NAME.origin | Optional | String | — | `"laboratory collection"` (illustrative) | Validated as text, not included in normalized rows; default empty |
| standards.NAME.measurements | Required | Nonempty array of tables | — | `[[standards."91500".measurements]]` | Groups values with shared provenance; missing fails |
| measurements.role | Required | Enumerated string | See role table | `"concentration"` | Measurement meaning; not inferred from quantity name |
| measurements.unit | Required | Role-specific string | See role table | `"ug/g"` | Must match role exactly; no unit conversion |
| measurements.status | Required | `recommended` or `provisional` | — | `"provisional"` | Reference-value status; no default |
| measurements.source | Required | Registry ID string | — | `"wiedenbeck1995"` | Resolves to registry display citation; unknown resolved ID fails |
| measurements.locator | Optional | Text | — | `"Table 2"` (illustrative) | Page/table locator becomes `source_locator`; default empty |
| measurements.notes | Optional | Text | — | `"Canonical initial/common-Pb ratio; reciprocal and cross-ratios are derived."` | `{quantity}` expands per value; default empty |
| measurements.values | Required | Nonempty table mapping quantity to number or value table | Role-specific | `"206Pb/238U*" = 0.17917` | One normalized record per quantity; missing/empty fails |

Value-level overrides do not remove the requirement that each group declare
role, unit, status, source and values. Inline value tables accept only the
following keys. Role and unit cannot be overridden at value level.

| Value key | Required/optional | Type | Unit | Example | Meaning / without it |
| --- | --- | --- | --- | --- | --- |
| value | Required in a value table | Integer or float, not bool | Group unit | `"238U" = { value = 80.0 }` | Stored measurement; bare numeric values also accepted |
| uncertainty | Optional | Converted to numeric | Same as value | `{ value = 80.0, uncertainty = 2.0, uncertainty_type = "1sd" }` (illustrative) | Missing becomes NaN, never assumed zero |
| uncertainty_type | Required with numeric uncertainty | Enumerated string | — | `"1sd"` | Both uncertainty and type must be provided together or both empty |
| status | Optional | Status string | — | `"recommended"` | Overrides group status; otherwise inherits |
| source | Optional | Registry ID | — | `"deer2013"` | Overrides group source; otherwise inherits |
| locator | Optional | Text | — | `"Table 2"` (illustrative) | Overrides group locator; otherwise inherits |
| notes | Optional | Text | — | `"Measured {quantity}"` (illustrative) | Overrides group notes; otherwise inherits |

Allowed uncertainty types are `1sd`, `2sd`, `expanded_95`,
`reported_unspecified`, or empty when uncertainty is absent. The loader
checks the paired presence; it does not convert these conventions to 1sd.

## Roles, units and quantity keys

| Role | Required unit | Quantity convention / example | Meaning |
| --- | --- | --- | --- |
| concentration | `ug/g` | `29Si = 151443.22960004435` | Elemental mass fraction assigned to an isotope channel; not multiplied by isotope abundance |
| initial_ratio | `ratio` | `"206Pb/204Pb_i" = 16.95` | Initial/common-Pb ratio, suffix `_i` |
| radiogenic_ratio | `ratio` | `"206Pb/238U*" = 0.17917` | Radiogenic ratio, suffix `*` |
| total_ratio | `ratio` | `206Pb/238U_total` | Measured total ratio, suffix `_total` |
| isotope_ratio | `ratio` | Isotope-ratio quantity name | Isotope ratio not otherwise classified |
| reference_age | `Ma` | `concordia_age = 1065.4`; also system-specific `206Pb/238U_age`, `207Pb/235U_age`, `207Pb/206Pb_age` | Accepted age; system-specific ages take precedence in corresponding derivations |
| delta_value | `permil` | Delta quantity name | Delta isotope value |
| epsilon_value | `dimensionless` | Epsilon quantity name | Epsilon isotope value |

Quote TOML keys containing `/` or `*`. Quantity names must be nonempty,
but are not checked against a fixed vocabulary or parsed to validate their
role. The suffix conventions above are used by the derivation code; arbitrary
nonempty quantity names can load without gaining those derived links.

## sources.toml

| Key / table | Required/optional | Type | Unit | Example | Meaning / without it |
| --- | --- | --- | --- | --- | --- |
| format_version | Required | Integer equal to 2 | — | `2` | Version check fails otherwise |
| sources | Required | Nonempty table of source-ID tables | — | `[sources."deer2013"]` | Shared registry; a source ID must resolve for each measurement |
| sources.ID.display | Required | Nonblank string, unique after stripping | — | `Deer, Howie & Zussman (2013), An Introduction to the Rock-Forming Minerals, 3rd ed.` | Citation stored in normalized `source`; missing/duplicate fails |
| sources.ID.kind | Optional | Metadata, conventionally string | — | `"book"`, `"journal_article"` | Retained in registry; loader does not enumerate kinds or enforce its type |
| sources.ID.doi | Optional | Metadata, conventionally string | — | `"10.1111/j.1751-908X.2005.tb00904.x"` | Retained without DOI validation or lookup; absent is allowed |
| sources.ID.url | Optional | Metadata, conventionally string | — | `"https://doi.org/10.1111/j.1751-908X.2005.tb00904.x"` | Retained; no fetching or URL validation |
| sources.ID.file | Optional | Metadata, conventionally string | — | `"Deer2013An introduction to the rock-forming minerals.pdf"` | Citation file label only; file existence is not checked |
| sources.ID.notes | Optional | Metadata, conventionally string | — | `"Publication describing the GeoReM compilation used for USGS glass reference values."` | Retained; absence allowed |

These are all source keys currently accepted. Except for display, their
types and contents are not validated. The registry is attached as
`measurements.attrs["sources"]`; normalized rows store display citations,
not source IDs.

**Planned:** source keys `no_doi_reason` and `status`, and an additional
source-ID check, belong to a later milestone. These two keys are currently
rejected as unsupported. Resolution of measurement source IDs against the
registry is already enforced; no further source-ID naming rule is imposed.

## Derived values and validation limits

The normalized table contains only documented inputs: `standard`, `aliases`,
`material`, `quantity`, `value`, `unit`, `uncertainty`, `uncertainty_type`,
`role`, `status`, `source`, `source_locator`, `notes`. The wide archive pivots
values in manifest order, adding material. With `include_derived=True` it
fills missing common-Pb reciprocals/cross-ratios, radiogenic ratios from
system ages or concordia age, linked U-Pb ratios using natural 238U/235U,
radiogenic reciprocals and total-ratio reciprocals. Direct values always
win. It does not create new normalized source measurements or propagate
uncertainties for these derived archive values.

The loader rejects unknown keys at every declared TOML level, duplicate
standard definitions across material files, invalid aliases/descriptions/
origins, missing required groups or values, booleans as measurement values,
unknown resolved sources, empty required text, nonnumeric/NaN values,
unsupported roles/statuses/uncertainty types, unit/role mismatches, unpaired
uncertainty/type, duplicate `(standard, quantity)` rows and multiple
materials for one standard. It does not enforce positive values, finite
infinity-free values, positive uncertainties, DOI validity, or a controlled
quantity vocabulary. Nonnumeric uncertainty coerces to NaN and then fails
only if it leaves a nonempty uncertainty type orphaned.
