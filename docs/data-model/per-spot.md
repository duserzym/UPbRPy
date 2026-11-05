# Per-spot CSV and folder

Implementation: [UPbRPy/qtegra.py](https://github.com/duserzym/UPbRPy/blob/main/UPbRPy/qtegra.py).

`read_spot_file(path) -> Acquisition` reads one file, with
`sequence_index=0`. `read_spot_folder(path) -> QtegraSession` orders a set
of files. `open_qtegra(path, *, cycle_time_s=None)` wraps a single per-spot
file as a one-acquisition `spot_folder` session with index 1.

The format has fixed line positions. An optional field can be empty within
its reserved line; deleting that line shifts the table and breaks the layout.
Qtegra writes UTF-8, CRLF, a semicolon-terminated metadata block and a
comma-separated sweep table with a trailing empty column. The decoder
accepts an optional UTF-8 BOM. Header cells are split literally on commas;
use the Qtegra layout, including the trailing comma.

## Per-file contract

Examples are short real values from
`Zircon_demo_1.csv` in the fixture's `zircon_04Nov26/`
folder. `Acquisition` is abbreviated to `A`, `QtegraSession` to `S`.

| Name / position | Required/optional | Type and definition | Unit | Example | Model destination | Without it |
| --- | --- | --- | --- | --- | --- | --- |
| Line 1 | Required | `<label>:<MM/DD/YYYY hh:mm:ss AM/PM>;` | Naive calendar clock | `NIST612_896:11/04/2026 10:55:37 PM;` | `A.label`, `.start_text` (time only), `.start`; complete line in `.sequence_fields["spot_file_start_text"]` | File rejected if line does not match |
| Line 2, Software Name | Optional within reserved line | `key=value;` text | — | `Name=Qtegra;` | `S.software`, combined with Version | Missing part omitted; both absent gives `None` |
| Line 2, Software Version | Optional within reserved line | Text | — | `Version=2.10.4345.64;` | `S.software` | Version omitted |
| Line 2, Software File Version | Optional within reserved line | Text | — | `File Version=1;` | Not retained | Ignored either way |
| Line 3, Configuration Machine | Optional within reserved line | Text | — | `Machine=iCAP RQ;` | `S.machine` | `None` |
| Line 4, tune name and settings | Optional content; reserved line | Prefix before `:`, then semicolon-separated key/value pairs; numeric values become floats | Setting-dependent, unspecified | `Laser_STDS`, `Plasma Power=1550;` | `A.tune_name`, `.tune_settings` | Empty dictionary if no pairs; prefix still read as tune name |
| Line 5, RF Generator | Optional content; reserved line | Readback pairs; float or text | Readback-dependent, unspecified | `RF Plasma Lit Readback=1;` | `A.readbacks` | No pairs added |
| Line 6, Ion Optics | Optional content; reserved line | Readback pairs; float or text | Readback-dependent, unspecified | `Sampling Depth Readback=5;` | `A.readbacks` | No pairs added |
| Line 7, Vacuum | Optional content; reserved line | Readback pairs; float or text | Readback-dependent, unspecified | `Analyzer Vacuum Ok Readback=1;` | `A.readbacks` | No pairs added |
| Line 8, Detector | Optional content; reserved line | Readback pairs; float or text | Readback-dependent, unspecified | `Detector Voltage (Counting) Readback=1599.21798631476;` | `A.readbacks` | No pairs added |
| Line 9, Cooling System | Optional content; reserved line | Readback pairs; float or text | Readback-dependent, unspecified | `Quad Board Temperature Readback=0;` | `A.readbacks` | No pairs added |
| Line 10, Power Supply | Optional content; reserved line | Readback pairs; float or text | Readback-dependent, unspecified | `Supply Voltage 500 V Readback=-538.856304985337;` | `A.readbacks` | No pairs added |
| Line 11, Gas Supply | Optional content; reserved line | Readback pairs; float or text | Readback-dependent, unspecified | `CCT2 Flow Readback=0;` | `A.readbacks` | No pairs added |
| Line 12, Pulse Counting Threshold | Optional within reserved line | Float | cps | `Threshold=2500000;` | `S.pulse_counting_threshold` | `None`; detector fitting uses its separate window default |
| Line 13 | Required position | Separator, normally blank | — | empty line | Not retained | Do not remove: line 14 must remain the table header |
| Line 14, Time | Required | Literal first header cell `Time` | — | `Time` | Names the sweep time column | File rejected unless line starts `Time,` |
| Line 14, channel names | Required for usable signals | Text in instrument order | — | `29Si`, `238U` | `A.channels[].name` | No usable Y for absent channel |
| Line 15, dwell time | Optional value; metadata row required | Float per channel, `dwell time=<s>` | s | `0.005` (29Si) | `A.channels[].dwell_s` | `None` |
| Line 15, xcal factor | Optional value; metadata row required | Float per channel, `xcal factor=<f>` | Multiplier on analog reading | `65695.80659` (29Si) | `A.channels[].xcal` | `None`; cannot recover it from per-spot Y alone |
| Lines 16+, Time column | Required for usable timing | Numeric offset from acquisition start; one per sweep | s | `0.00732` | `A.time` (1-D), `.sweep_time` | Invalid numeric cells become NaN; no nominal reconstruction for this format |
| Lines 16+, each channel column | Required for requested channel | Numeric Y used for reduction | cps | `449124.383551277` (29Si) | `A.y` | Empty/nonnumeric cells become NaN; all-empty trailing Y sweeps removed |

The reader uses line positions, not the expected metadata prefixes, to assign
software, machine, settings, readbacks and threshold. Readbacks flatten all
seven groups into one dictionary; later duplicate names overwrite earlier
ones. Empty state values are omitted. `read_spot_file` returns only the
acquisition; the session wrapper retains software, machine and threshold.
No per-spot mass, analog, counter, stop or user fields are synthesized.

## Folder discovery

| Rule | Type / example | Required/optional | Behavior without it |
| --- | --- | --- | --- |
| Immediate CSV files | Files with suffix `.csv`, case-insensitive | At least one detected per-spot file for `read_spot_folder` | `No Qtegra per-spot CSV files found.` |
| Recognizable line 1 | Same pattern as above | Required for detection as per-spot | Other CSV layouts ignored; a detected file with a malformed body can still fail reading |
| Start time, then file name | Parsed datetime, filename tie-break | Required line 1; no filename sequence convention | Ordering does not use the numeric filename suffix |
| Non-CSV files and subdirectories | For example notes and nested folders | Optional | Ignored by `read_spot_folder`; no recursive scan |
| Other CSV layouts | Rich, unrecognized, or Y-only | Optional | Non-spot CSVs ignored; Y-only files add an ignored-file warning |
| Session metadata | Software, machine, threshold from first chronological file | Optional values | `None`; later files are not checked for agreement |

Each session acquisition receives a 1-based `sequence_index` after ordering.
Per-spot sessions always advertise `timestamps`, `instrument_state` and
`dwell_xcal`; these are format-level declarations, even if optional cells
are empty. They do not advertise `sequence_fields`, although the preserved
line 1 is stored in that dictionary.

For `open_qtegra` on a general folder, immediate Rich/per-spot CSVs are
discovered. With one Rich file, companions can be immediate per-spot files
or per-spot files in exactly one immediate subfolder. Multiple Rich files,
multiple companion subfolders, or immediate spots plus a companion
subfolder are errors. With no Rich, only immediate spot files are read.
Opening a CSV file directly never discovers companions.

## Rich + per-spot merge

`merge(rich, folder, *, rtol=1e-12) -> QtegraSession` returns a deep copy.
It requires formats `rich` and `spot_folder` and a parsed Rich start for
every acquisition. Original clocks and Rich source paths are retained.

| Rule / addition | Type / unit | Example or check | Without it / failure |
| --- | --- | --- | --- |
| Pair by exact label | Text | `NIST612_896` | Duplicate labels rejected; every Rich label must have a companion; extra companion labels warn |
| Channel order | Tuple of names | `29Si`, `91Zr`, … | Unequal names/order rejected |
| Sweep count and Y | Float array, cps | Same shape and NaN masks; `allclose(rtol=rtol, atol=0, equal_nan=True)` | Mismatch raises `ValueError` naming label/channel |
| First-channel Time | Float array, s | Same shape and relative tolerance as Y | Mismatch rejected for recorded Rich times; check skipped for reconstructed Rich times, which are retained |
| Dwell/xcal | Optional float per channel | `0.005`, `65695.80659` | Copy companion values, including `None`; Rich has no exported dwell/xcal |
| Software, machine, threshold | Text, text, float cps | `Qtegra 2.10.4345.64`, `iCAP RQ`, `2500000` | Copy session values from folder, including missing ones |
| Instrument state fill | Dictionaries | Fixture acquisition 1 has empty Rich tune settings | Fill only wholly empty Rich dictionaries; warn for each acquisition filled |
| Companion line 1 | Text | `NIST612_896:11/04/2026 10:55:37 PM;` | Retained in `sequence_fields["spot_file_start_text"]` |
| Clock offset | Float hours and seconds | `clock_offset_hours=7.0` for fixture | Median companion minus Rich starts; hours rounded to nearest 0.25; exact median in `clock_offset_seconds_median`; neither clock shifted |
| Sources / capabilities | Tuples / sets | Format becomes `rich+spot_folder` | Append companion sources and union capabilities; retain both sessions' warnings |

Rich Y retains its greater printed precision; companions are checked with
a tolerance. Per-spot Time is shared across channels and corresponds to
the first Rich channel, not each channel's offset.
