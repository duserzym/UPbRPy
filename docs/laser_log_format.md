# Chromium laser experiment logs

`UPbRPy.laserlog` reads Teledyne Chromium experiment files (`.lase`). This
page records the format observed in instrument exports and the stage L1
reader contract. The reader uses only the Python standard library. It does
not match scans to Qtegra acquisitions or change reduction behaviour.
All examples below are synthetic.

## Encoding and sections

Files are plain text, normally ISO-8859-1 with CRLF line endings. In
`Laser.SpotSize`, `µ` is the single byte `0xB5`. The reader tries strict
UTF-8 first, then falls back to ISO-8859-1; UTF-8 with a BOM is accepted.
CR and LF line endings and a final line without a newline are also accepted.

Sections have `[Section]` headers and `key=value` entries. Observed order:

| Section | Contents | Reader destination |
| --- | --- | --- |
| `Scans` | `Header`, then `Scan0`, `Scan1`, … | `scan_header`, `scans` |
| `GasControl:<name>` | Gas-control keys and string values | `gas_control[name]` |
| `Events:<name>` | Event settings, including `All`, `Pass0.Event0`, `Pass1.Event0` | `events[name]` |
| `Laser.ExperimentSettings` | `StabilityMode`, `HighVoltage`, `EnergySetpoint` | `experiment_settings` |
| `SystemInfo` | `SystemType`, `SerialNumber`, `Version`, `WindowsVersion` | `system_info` |
| Any unknown section | All its keys and string values | `other_sections[section]` |

For example, synthetic system metadata can be:

```ini
[SystemInfo]
SystemType=Chromium
SerialNumber=TEST-0000
Version=0.0.0.0
WindowsVersion=TEST
```

## Scan rows

The first section and its header identify the format:

```ini
[Scans]
Header=Scan Type,Description,Selected,Lock Edit,Vertex Count,Vertex List,Preablation Settings,Ablation Settings,Data
Scan0=Spot,"Spot 1",1,0,1,"1000.00,2000.00,300.00","PassEnabled=0","Laser.Fluence=2.20;Laser.RepRate=10.00;Laser.SpotSize=25µm Circle","ExpectedLaseTime=10.0;ScanLength=0.0"
```

`ScanN` supplies a zero-based index. Each value is one CSV row with nine
fields. Fields 2, 6, 7, 8 and 9 are double-quoted and may contain commas
and semicolons. A literal double quote is escaped by doubling it.

| Field | Interpretation |
| --- | --- |
| Scan Type | Observed values `Spot` and `Line`; retained as text |
| Description | Original label, including copies such as `Spot 7 (Copy)` |
| Selected | `0` or `1`, mapped to an optional boolean |
| Lock Edit | `0` or `1`, mapped to an optional boolean |
| Vertex Count | Declared count, retained independently of the vertices |
| Vertex List | `x,y,z` stage coordinates in micrometres, with `;` between vertices |
| Preablation Settings | Semicolon-separated `key=value` settings |
| Ablation Settings | Semicolon-separated `key=value` settings |
| Data | Semicolon-separated `key=value` entries, including any firing records |

A spot has one vertex; a line has two, for example
`1000.00,2000.00,300.00;1100.00,2100.00,300.00`. Vertices retain decimals.
`spot_number` is the trailing integer in the description: `Spot 12` gives
12; `Spot 7 (Copy)` and `Spot 7 (Copy) (Copy)` give `None`. The scan index
and description number are independent.

## Settings

Both settings fields become `LaserSettings`. Their `raw` dictionaries keep
every key in original order and every value string, including unknown keys,
trailing zeros and decoded `µ`. Typed fields are `None` when absent or
unparseable.

| Key | Typed field | Unit or interpretation |
| --- | --- | --- |
| `Laser.Output` | `output_pct` | Percent |
| `Laser.Fluence` | `fluence_j_cm2` | J/cm² |
| `Laser.RepRate` | `rep_rate_hz` | Hz |
| `ShotCount` | `shot_count` | Integer |
| `Laser.SpotSize` | `spot_size_um`, `spot_shape` | `25µm Circle` → 25.0, `Circle`; `20um Circle` also accepted |
| `ScanSpeed` | `scan_speed_um_s` | Assumed µm/s; not independently established |
| `PassEnabled` | `pass_enabled` | `0` or `1` |
| `PassCount` | `pass_count` | Integer |
| `DwellTime` | `dwell_time_s` | Seconds |

Other observed keys are `Dosage`, `LineSpacing`, `LineScanMode`,
`SpotSpacing`, `Laser.SpotRotation` and `ZDepth`. These remain in `raw`.
The reader makes no inference from them.

`LaserSettings.key()` returns the hashable tuple
`(fluence_j_cm2, rep_rate_hz, shot_count, output_pct, spot_size_um,
spot_shape, scan_speed_um_s)`. Pass count, dwell, enabled state and unknown
raw keys do not enter this grouping key.

## Data and firing records

An unfired scan may contain only
`ExpectedLaseTime=10.0;ScanLength=0.0`. Many files contain only placed,
unfired scans. A synthetic fired spot's Data field can contain:

```text
SpotCount=1;Spot0.Pass1.Position.Start=1000,2000,300;Spot0.Pass1.Time.Start=2030/01/02 10:00:00.123;Spot0.Pass1.Time.LaserOn=2030/01/02 10:00:01.456;Spot0.Pass1.Time.LaserOff=2030/01/02 10:00:11.456;Spot0.Pass1.Laser.LastOutputReading=4.90;Spot0.Pass1.Time.End=2030/01/02 10:00:12.000;ExpectedLaseTime=10.0;ScanLength=0.0
```

| Data key | Reader field |
| --- | --- |
| `SpotCount` | `LaserScan.declared_spot_count` |
| `ExpectedLaseTime` | `LaserScan.expected_lase_time_s` |
| `ScanLength` | `LaserScan.scan_length_um` |
| `SpotK.PassP.Position.Start` | `LaserFiring.start_position_um`, a float x/y/z tuple |
| `SpotK.PassP.Time.Start` | `LaserFiring.start` |
| `SpotK.PassP.Time.LaserOn` | `LaserFiring.laser_on` |
| `SpotK.PassP.Time.LaserOff` | `LaserFiring.laser_off` |
| `SpotK.PassP.Time.End` | `LaserFiring.end` |
| `SpotK.PassP.Laser.LastOutputReading` | `LaserFiring.last_output_reading` |

Times are local clock readings without a zone, written as
`YYYY/MM/DD HH:MM:SS.mmm`. They become naive `datetime` objects;
`.123` becomes 123000 microseconds. No zone or clock correction is applied.
`laser_duration_s` is laser-off minus laser-on in seconds, or `None` if
either is missing. `complete` means both are present, regardless of the
other fields.

`K` and `P` are retained as `firing_index` and `pass_index`. Each observed
prefix produces one firing, even if it has only an unknown key. A scan's
firings are ordered by `(firing_index, pass_index)`. `SpotCount` is a
declaration, not a record count: a line scan can declare 2 while containing
only `Spot0.Pass1.Position.Start`, `Time.Start` and `Time.LaserOn`. Missing
fields remain `None`; the reader creates no `Spot1` record. Such an
incomplete firing is valid input and does not itself imply file truncation.
In the observed files every `SpotK` was `Spot0` and every `PassP` was `Pass1`;
two-record scans were not observed, only the declared-2 line case.

`LaserScan.data` keeps all data keys. `LaserFiring.raw` keeps that firing's
keys with the `SpotK.PassP.` prefix removed, including unknown keys.

## Reader API

```python
from UPbRPy import laserlog

log = laserlog.read_lase("synthetic.lase")
uploaded = laserlog.parse_lase(b"[Scans]\nHeader=Scan Type,Description\n")
```

The second example has no scans or SystemInfo and returns a truncated log
with a warning. File signatures, rather than file extensions, determine
acceptance.

| Public function | Result |
| --- | --- |
| `detect_format(path: str or Path) -> str` | `"lase"` when the first nonblank lines, after an optional BOM, are `[Scans]` and `Header=Scan Type,...`; reads only those lines |
| `read_lase(path: str or Path) -> LaserLog` | Detects then loads the file; records its `Path` |
| `parse_lase(data: bytes or str, *, path: str or Path or None = None) -> LaserLog` | Same parser for in-memory uploads; an optional path is recorded without file access |

`UnsupportedLaserLogFormat` subclasses `ValueError` and is raised by all
three functions for a missing signature. File-system errors propagate.
Content problems after a valid signature produce `LaserLogWarning`
records, not exceptions.

The public records are `LaserSettings`, `LaserFiring`, `LaserScan`,
`LaserLogWarning` and `LaserLog`. The first four are frozen dataclasses;
their dictionary fields remain ordinary dictionaries. `LaserLog` is mutable
and contains `path`, the section dictionaries listed above, `scan_header`,
`scans` (a tuple sorted by index), `warnings` (a tuple) and `truncated`.
Each `LaserScan` records its 1-based `line_number`; warnings contain
`line_number` and `message`, with `None` for file-level warnings.

| Property or method | Result |
| --- | --- |
| `LaserScan.fired` | Any firing has `laser_on` |
| `LaserScan.fired_count` | Number of firings with `laser_on`, independent of declared count |
| `LaserLog.fired_scans`, `unfired_scans` | Tuples in scan-index order |
| `LaserLog.firings` | All firings in laser-on order, missing laser-on last; ties keep scan/firing order |
| `LaserLog.first_laser_on`, `last_laser_on` | Earliest/latest recorded laser-on, or `None` |
| `LaserLog.distinct_ablation_settings()` | List of `(LaserSettings, count)` grouped by `key()`, counting all scans, including unfired scans, in first-seen scan-index order |
| `LaserLog.scans_by_spot_number()` | Dictionary from description number to a list of scans; omits `None` numbers and retains duplicates |

## Tolerance rules

- Wrong field counts, bad CSV quoting or an unparseable vertex cause that
  scan row to be skipped with a warning naming its source line. Later rows
  still load.
- Unparseable typed numbers, booleans, sizes or times become `None`, with
  a warning naming the key and line. Floating values must be finite. The
  original settings and data strings remain in their raw dictionaries.
- A declared vertex count that differs from the parsed vertices is kept
  with a warning. Missing optional keys, including firing keys, are allowed.
- Duplicate scan indices keep the first successfully parsed scan and warn
  on subsequent occurrences.
- Unknown keys and sections are retained. Additional keys in `[Scans]`
  are stored in `other_sections["Scans"]`. Repeated sections merge keys;
  repeated dictionary keys take the last value without changing key order.
- Malformed non-scan lines or semicolon entries without `=` produce a
  warning; they cannot be represented as dictionary entries.
- An incomplete final scan row (unfinished quoted field or too few fields)
  sets `truncated=True`; complete rows still load. Missing `[SystemInfo]`
  also sets this flag and emits a file-level warning. A final line without
  a newline is valid and does not by itself set the flag. Truncation is
  detectable only through these signals, not guaranteed to be detectable
  after every possible interrupted metadata write.

## Not understood

- What `SpotK` indexes: it may be a point within a scan rather than a repeat
  firing. Why `SpotCount` can exceed the records present is also unresolved.
- What `Pass0` and `Pass1` mean. Pre-ablation versus ablation is a guess;
  event sections include both Pass0 and Pass1. The reader does not assign
  either interpretation.
- Whether Qtegra's **Laser Scan Position Number** is the description number
  or the scan position (`index + 1`). These differ for copied scans; the
  stage L1 reader performs no join.
- The meaning of `Dosage`, `LineScanMode`, `ZDepth`, `ExpectedLaseTime` and
  `ScanLength`. The latter two have provisional typed names in the API,
  without an inferred physical interpretation.
- The units of `ScanSpeed`, currently assumed to be µm/s.
- Why `Position.Start` uses integer coordinates while placed vertices have
  decimals. They agree within 1 µm in the observed files; the reader retains
  both rather than rounding or reconciling them.
