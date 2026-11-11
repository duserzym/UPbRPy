# Rich single CSV

!!! abstract "In short"
    The Rich single CSV is Qtegra's most complete export: one file for the whole
    session, one column per laser spot, and one row per isotope, sweep and kind
    of reading. UPbRPy treats it as the main record of a session. This page lists
    every row type, whether it is required, and what is lost without it.

Implementation: [UPbRPy/qtegra.py](https://github.com/duserzym/UPbRPy/blob/main/UPbRPy/qtegra.py).

`read_rich_csv(path, *, cycle_time_s=None)` reads the canonical acquisition
record. Qtegra writes a comma-separated UTF-8 file with a BOM, CRLF line
endings and a final empty cell on each row. The reader uses `utf-8-sig`;
the BOM and CRLF are export conventions, not separately validated requirements.

Columns 1–4 are **group, key, subkey, quantity**. Each acquisition occupies
one column from column 5 onward. Header 1 selects acquisition columns by
nonempty cells; its column 5 must be `Sample 1`. Header 2 supplies labels.
MainRuns rows use a zero-based sweep key, channel subkey and quantity.
Channels retain their order of first appearance. Rows are not separate
files or separate acquisitions.

## Header and row-group contract

This heading and the first two table columns are stable: the consistency
test reads them. `Required` denotes the file contract; `One of` means Time
or StartTime, including both. All metadata keys below are independently
optional. A capability is set by row-group presence, even if cells are empty.
“Sequence” below means the `sequence_fields` capability. Units marked
“unspecified” are not defined or converted by the reader.

Examples are exact short cells from
`tests/fixtures/qtegra/Zircon_04Nov26_slice/Zircon_04Nov26_slice_Rich.csv`.
`empty` denotes an empty fixture cell, not the literal string `empty`.

| Name | Required/optional | Type and definition | Unit | Example | Capability | Model destination | Without it |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Header row 1 | Required | Text; acquisition column markers | — | `Sample 1` | — | Column selection; `Acquisition.sequence_index` is column order, 1-based | Rejected |
| Header row 2 | Required | Text; acquisition labels | — | `NIST612_896` | — | `Acquisition.label` | Rejected (`Missing acquisition label row (header row 2)`) |
| MainRuns Y | Required | Float per sweep/channel; signal used for reduction | cps | `449124.38355127675` | — | `Acquisition.y` | Rejected; every channel seen in MainRuns needs Y rows |
| MainRuns Time | One of | Float per sweep/channel; offset from acquisition start | s | `0.00732` | `per_channel_time` | `Acquisition.time` (2-D) | Reconstruct shared offsets from StartTime; reject if both absent |
| MainRuns Analog | Optional | Float per sweep/channel; analog detector reading | cps (instrument scale) | `7.6677666666666662` | `analog_counter`, only with Counter | `Acquisition.analog` | No fitted detector correction; an unpaired Counter is ignored with warning |
| MainRuns Counter | Optional | Float per sweep/channel; pulse-counting signal, `-33` is saturated | cps | `449124.38355127675` | `analog_counter`, only with Analog | `Acquisition.counter` | No fitted detector correction; an unpaired Analog is ignored with warning |
| MainRuns X [u] | Optional | Float per sweep/channel; first finite mass per channel retained | u | `28.97599983215332` | `mass` | `Acquisition.channels[].mass` | Mass is `None`; names still identify channels |
| TuneSettings | Optional | Setting name in subkey, tune name in key; float if parseable, otherwise text | Setting-dependent, unspecified | `Plasma Power` / `1550` (acquisition 2) | `instrument_state` | `Acquisition.tune_name`, `.tune_settings[subkey]` | No tune settings; merge can fill an empty dictionary |
| StatusReport | Optional | Readback name in subkey, group in key; float or text | Readback-dependent, unspecified | `RF Plasma Lit Readback` / `1` | `instrument_state` | `Acquisition.readbacks[subkey]` | No readbacks; merge can fill an empty dictionary |
| Info UserName | Optional | Text; recorded operator account | — | `BOISESTATE\ERB5110` | `user` | `Acquisition.user` | `None` |
| StartTime | One of | ISO datetime text; acquisition start | Calendar clock | `2026-11-04T15:55:37.4924868` | `timestamps` | `Acquisition.start_text`, `.start` | With Time, build a continuous session axis; companion merge requires starts |
| StopTime | Optional | ISO datetime text; acquisition end, including overhead | Calendar clock | `2026-11-04T15:56:09.7022924` | `stop_times` | `Acquisition.stop_text`, `.stop` | `None`; never used to infer sweep spacing |
| BrigidMS Label | Optional | Text; sample-list label | — | `NIST612_896` | Sequence | `Acquisition.sequence_fields["Label"]` | Key absent; acquisition label still comes from header 2 |
| BrigidMS Duration [s] | Optional | Text; planned duration | s | `30` | Sequence | `Acquisition.sequence_fields["Duration [s]"]` | Key absent; measured sweeps still determine length |
| BrigidMS Comment | Optional | Text; sample-list comment | — | `<Comment>` | Sequence | `Acquisition.sequence_fields["Comment"]` | Key absent |
| BrigidMS TRQuant Evaluate | Optional | Text; exported evaluation flag, not parsed as bool | — | `True` | Sequence | `Acquisition.sequence_fields["Evaluate"]` | Key absent; does not select/reject spots |
| BrigidMS TRQuant Sample Type | Optional | Text; exported sample classification | — | `UNKNOWN` | Sequence | `Acquisition.sequence_fields["Sample Type"]` | Key absent; sample names still derive from labels |
| BrigidMS TRQuant Standard | Optional | Text; exported standard designation | — | empty | Sequence | `Acquisition.sequence_fields["Standard"]` | Key absent; reduction standard choices are separate |
| BrigidMS TRQuant Dilution Factor | Optional | Text; exported dilution | Factor | `1` | Sequence | `Acquisition.sequence_fields["Dilution Factor"]` | Key absent; no automatic dilution correction |
| BrigidMS TRQuant Amount | Optional | Text; exported amount | Unspecified | empty | Sequence | `Acquisition.sequence_fields["Amount"]` | Key absent |
| BrigidMS TRQuant Final Quantity | Optional | Text; exported final quantity | Unspecified | empty | Sequence | `Acquisition.sequence_fields["Final Quantity"]` | Key absent |
| PMIAnalyteLaser Identifier | Optional | Text; laser identifier | — | `<Identifier>` | Sequence | `Acquisition.sequence_fields["Identifier"]` | Key absent |
| PMIAnalyteLaser Laser Scan Position Number | Optional | Integer text; explicit spot number | — | `896` | Sequence | `Acquisition.spot_number` and `.sequence_fields["Laser Scan Position Number"]` | Parse trailing `_` integer from label, otherwise `None` |
| PMIAnalyteLaser Laser Output Setting [%] | Optional | Text; laser output setting | % | `46` | Sequence | `Acquisition.sequence_fields["Laser Output Setting [%]"]` | Key absent; no automatic signal rescaling |

The sample-list dictionaries keep original strings, including empty strings.
Tune settings and readbacks omit empty values and flatten away their group
names. A conflicting laser spot number replaces the label-derived number
and adds a session warning. Arbitrary additional BrigidMS/PMIAnalyteLaser
keys are also retained under `subkey or key`.

Both header rows are checked: row 1 must start with four empty cells and
`Sample 1`; row 2 must start with four empty cells followed by the
acquisition labels. A file missing row 2 is rejected rather than read with a
data row as labels.

Numeric MainRuns cells that are empty or nonnumeric become NaN, never zero.
Trailing sweeps whose Y is NaN for every channel are removed independently
for each acquisition; interior missing cells remain NaN. The exported sweep
72 can therefore be absent for one acquisition and present for another.
Negative sweep indices are rejected. Presence of an optional quantity does
not guarantee complete coverage of every sweep/channel.

## Reduced files and timing

With Time rows, recorded per-channel times win and `cycle_time_s` is ignored.
Without Time rows, a StartTime row permits
`np.arange(n_sweeps) * cycle_time_s`, shared by all channels. `None` selects
`DEFAULT_CYCLE_TIME_S = 0.4255`; an explicit value must be finite and positive.
The session records `times_reconstructed=True`, `nominal_cycle_time_s` and
`No MainRuns Time rows: sweep times reconstructed with cycle_time_s=0.4255 s from StartTime.`
(with the chosen spacing). `read_experiment_data` additionally emits one
`UserWarning` per reconstructed import. StopTime is never a spacing estimate.

With Time but no StartTime, starts remain `None`. Experiment import places
acquisitions back to back in reader order, advancing past the preceding last
finite sweep by its median positive spacing. Without recorded starts the
axis starts at an internal 1970-01-01 anchor; returned times are relative
seconds. A session warning identifies approximate time-dependent drift
corrections. Constant detector fitting can work without starts; drift
detector fitting requires a start for every acquisition.

Timestamps in the fixture are naive. Original text keeps seven fractional
digits; parsing explicitly truncates fractions after six digits. Readers
do not assign a time zone or convert to UTC. A present but empty StartTime
row sets `timestamps`, yet yields `start=None`; row presence is the timing
acceptance check, not proof of valid calendar values for every acquisition.

## Errors and discovery warnings

Paths, channel names and labels below are substituted at runtime. CSV/parser
and datetime conversion errors can also propagate for malformed content.

| Message | Cause / response |
| --- | --- |
| `Unsupported Qtegra export: no time information: <name> has MainRuns Y rows but no MainRuns Time rows and no StartTime row. Export the Rich single CSV (Y, Time, Analog, Counter, X [u] and the metadata rows) or the per-spot folder.` | `UnsupportedQtegraFormat` (a `ValueError`); re-export with time |
| `Not a Qtegra single CSV: <path>` | `read_rich_csv` cannot find the expected initial layout |
| `Missing acquisition label row (header row 2) in <name>: row 2 must have four empty cells followed by one label per acquisition.` | Header row 2 absent or malformed |
| `No MainRuns Y data in <path>` | No Y quantity |
| `Missing MainRuns Y rows for channels: <names>` | A channel present in another quantity has no Y rows |
| `Negative sweep index in <path>` | Invalid sweep key |
| `cycle_time_s must be finite and > 0.` | Invalid nominal spacing when Time is absent |
| `Unrecognized Qtegra format: <path>` | Detection cannot match the first line |
| `No Qtegra MainRuns data: <path>` | Detection finds no usable single-CSV data |
| `More than one Rich CSV in <path>` | Folder discovery is ambiguous |
| `Ambiguous per-spot companion folders in <path>` | Multiple companions, or immediate spots plus a companion subfolder |
| `No Qtegra exports found in <path>` | No usable exports discovered |

Folder discovery skips a Y-only file when usable input exists and records
`Ignored <name>: no time information (Y-only export).` A folder with only
unsupported Y-only files raises the unsupported-file error. Unpaired
detector groups produce `Missing MainRuns Counter rows: ignoring unpaired Analog/Counter data.`
or the equivalent message naming Analog.
