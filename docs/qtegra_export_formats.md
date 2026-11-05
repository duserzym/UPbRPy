# Qtegra export formats: which one is the record (2026-11-04)

Decision from the Zircon_demo session (Boise, iCAP RQ,
Qtegra 2.10.4345.64), three exports of the same 245 acquisitions:

| | Rich single CSV | per-spot folder (245 CSV) | UPbR single CSV |
|---|---|---|---|
| size | 36 MB | 13 MB total | 5.7 MB |
| layout | one column per acquisition, one row per (run type, sweep, channel, quantity) | one file per acquisition, one row per sweep | as Rich, Y rows only |
| cps per channel per sweep (Y) | yes | yes | yes (byte-identical to Rich Y rows) |
| analog reading per channel per sweep | **yes** | no | no |
| pulse-counter reading per channel per sweep | **yes** (sentinel −33 when saturated) | no | no |
| mass per channel (X [u]) | yes | no | no |
| sweep time | **per channel** | one per sweep (= first channel's time) | no |
| acquisition start and stop time | yes, ISO with sub-ms, local clock | start only, `MM/DD/YYYY hh:mm:ss AM/PM`, 7 h ahead of Rich (UTC) | no |
| tune settings (36) and readbacks (43) per acquisition | yes | yes | no |
| user name | yes | no | no |
| sample-list fields (duration, comment, evaluate, sample type, dilution, laser scan position, laser output %) | **yes** | no | no |
| dwell time per channel | no | **yes** | no |
| cross-calibration (xcal) factor per channel | no (recoverable exactly from Y/Analog on saturated sweeps) | **yes** | no |
| pulse-counting threshold, software version, machine name | no | yes | no |

Facts checked on this session:

- Y = Counter wherever the counter is valid (0 mismatches in 497,679 cells);
  Y = Analog × xcal where the counter reads −33 (13,997 cells). The xcal factor
  is one constant per channel for the whole session (29 channels, 245 files,
  one distinct value each), so Qtegra applies a tuning-time cross-calibration,
  not a time-resolved one.
- The counter saturates on 91Zr and 177Hf for every zircon, on 29Si for
  glasses and some zircons, on 238U for 9 acquisitions (4 S513, 3 LTSE, 2
  unknowns), on 208Pb for 1 NIST610. Pb isotopes stayed in counting mode.
- From the Rich file the counter-vs-analog relation can be fitted inside the
  overlap window (1e5 to 2.4e6 cps). 238U on S513 (22 spots, 24 to 26 sweeps
  each): slope 97k to 107k, pooled per half hour 104.0k, 103.8k, 104.2k,
  103.8k, 102.5k, against the applied 109,642. 29Si: 63k to 70k per spot,
  applied 65,696. So the applied 238U factor is about 5 percent high for this
  session; it only bites where 238U is in analog mode (high-U zircon) while the
  primary standard is in counting mode, which is exactly the case a
  within-session calibration must handle. The UPbR and per-spot formats cannot
  show this at all.

## Decision

1. **The Rich single CSV is the canonical acquisition record.** UPbRPy's
   reader, UPbRPy Studio, and laicpmsdb ingest are built on it.
2. **The per-spot folder is a companion, not an alternative.** When present
   next to a Rich file it supplies dwell time, xcal factor, the pulse-counting
   threshold, software version and machine; acquisitions are matched by label,
   and Y values are checked equal. A folder alone is still a complete-enough
   input for reduction (what UPbRPy does today).
3. **The UPbR single CSV is a derived view** (its content is the Rich file's Y
   rows). It is accepted as a degraded input for old sessions and for Mark's
   spreadsheet workflow, never exported as the archive.
4. **Ask the lab** whether the Rich export can be configured to include dwell
   and xcal (Qtegra export options), and whether the LabBook file itself can
   be kept with the session; if it can, it is the raw archive and the CSVs are
   its exports.

## Contract (one in-memory model, three readers)

`Acquisition`: label, sequence index, start, stop (optional), channels in
instrument order; per channel: mass (optional), dwell (optional), xcal
(optional), and per sweep: time, y, analog (optional), counter (optional).
Instrument state: tune settings and readbacks as two flat dicts (optional).
Sequence fields from the sample list (optional). Source: file name, SHA-256,
format name, and a `capabilities` set:

`timestamps`, `stop_times`, `per_channel_time`, `analog_counter`, `mass`,
`dwell_xcal`, `instrument_state`, `sequence_fields`, `user`.

| reader | yields | capabilities |
|---|---|---|
| `read_rich_csv(path)` | whole session | all but `dwell_xcal` |
| `read_spot_folder(dir)` | whole session, files ordered by time | `timestamps`, `instrument_state`, `dwell_xcal` |
| `read_upbr_csv(path)` | whole session, sweeps only | none (sweep times reconstructed from sweep index × nominal cycle time, flagged) |
| `merge(rich, folder)` | Rich plus `dwell_xcal` and threshold from the folder | union |

Rules every consumer follows:

- Reduction uses `y`. Where `analog_counter` is present, UPbRPy offers a
  detector cross-calibration step with three modes: Qtegra factor (as
  exported, default and identical to today's results), session-constant fit,
  and time-drift fit (per-channel linear fit of counter on analog inside the
  overlap window, on all acquisitions or on the primary standard only, smoothed
  along time); the chosen mode and its factors go in the reduction report.
- Time: both exports are naive. The Rich `StartTime` is the instrument PC's
  local clock; the per-spot line-1 time was 7 h ahead in this session (UTC).
  Store the original text of both and resolve to an aware UTC instant with the
  session's time zone setting; never silently reinterpret.
- Labels are the join key to samples and spots (`sep='_'`, aliases as today);
  the Rich file's `Laser Scan Position Number` is stored as the spot number
  and checked against the number parsed from the label.
- Sweep index 72 is empty for acquisitions with 72 sweeps and present for
  those with 73: empty cells are missing values, not zeros.

## Where it lands

- **UPbRPy** (`UPbRPy/io.py`): new module `qtegra.py` with the three readers
  and `merge`; `read_experiment_data` accepts a folder, a Rich file, a UPbR
  file, or a folder containing any of them, and builds today's experiment
  object unchanged; `calibration.py` gains the cross-calibration modes; Studio
  opens all three and shows the capabilities badge of the loaded session.
  Fixtures: a trimmed copy of this session (first 12 acquisitions, Rich slice,
  12 per-spot files, UPbR slice) generated by a script from the full files on
  the laptop; the full files stay off git.
- **laicpmsdb**: `POST /v1/sessions/ingest` and the watcher accept a Rich
  file, a folder, or both; the Parquet sweep schema gains nullable `analog`,
  `counter`, and per-channel `time` columns; `Acquisition.channels` carries
  mass, dwell, xcal when known; `instrument_state` carries the two dicts;
  a `sequence_fields` JSON and a session-level `capabilities` list are added;
  the Rich file is a `RawAsset` like any per-spot file. The parser is imported
  from UPbRPy (the plan already says the Python reader is UPbRPy's).
- **UPb Studio**: nothing beyond the file types the shell opens.
- **bracketeer**: unchanged; its sample-list columns are exactly the
  `BrigidMS`/`PMIAnalyteLaser` rows of the Rich file, which is how a planned
  sequence will be reconciled with what was run.

Source data on this laptop:
`/path/to/LAICPMS_data/`
(`Zircon_demo_Rich.csv`, `..._UPbR.csv`, folder
`zircon_04Nov26/`).
