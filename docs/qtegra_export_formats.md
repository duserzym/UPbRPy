---
icon: material/scale-balance
---

# Qtegra export formats: which one is the record (2026-11-04)

!!! abstract "In short"
    This is the record of why UPbRPy uses the Qtegra Rich CSV (with the
    per-spot folder as a companion) and stopped accepting the UPbR CSV. For a
    practical summary, see [Choosing an export](guide/choosing-an-export.md).
    The comparison tables below are historical; the
    [reference pages](data-model/index.md) describe current behaviour.

## Decision 2026-11-05

The UPbR single CSV is dropped because it has no time information: it
contains MainRuns Y rows but no Time rows or StartTime row. Export the
**Rich single CSV** (Y, Time, Analog, Counter, X [u] and metadata rows) or
the **per-spot folder**. Y-only exports raise `UnsupportedQtegraFormat`;
folder discovery ignores them with a warning when usable exports exist.
Reduced Rich files are accepted: the minimum is the two header rows,
MainRuns Y rows for every channel, and either MainRuns Time rows or a
StartTime row. All other row groups are optional.

The comparison below is historical.

Decision from the Zircon_demo session (Boise, iCAP RQ,
Qtegra 2.10.4345.64), three exports of the same 245 acquisitions:

| | Rich single CSV | per-spot folder (245 CSV) | UPbR single CSV (dropped 2026-11-05) |
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
3. **Y-only exports are unsupported** as of 2026-11-05 because they have
   no time information. Use Rich or per-spot exports for reduction.
4. **Ask the lab** whether the Rich export can be configured to include dwell
   and xcal (Qtegra export options), and whether the LabBook file itself can
   be kept with the session; if it can, it is the raw archive and the CSVs are
   its exports.
5. **Counting statistics need the dwell.** Y is in cps; Poisson errors use
   `Y * dwell`. Without a recorded dwell (Rich alone) the reader recovers it
   from the cps value of one pulse count (see "Counting dwell" in
   `docs/data-model/acquisition-model.md`). Until 2026-11-11 the ratio code
   treated cps as counts, which made sweep errors `1 / sqrt(dwell)` too
   small (3.65x at 0.075 s).

## Contract (one in-memory model, Rich and per-spot readers)

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
| `read_rich_csv(path, *, cycle_time_s=None)` | whole session, including reduced Rich files with the minimum above | capabilities from row groups present; never `dwell_xcal` |
| `read_spot_folder(dir)` | whole session, files ordered by time | `timestamps`, `instrument_state`, `dwell_xcal` |
| `merge(rich, folder)` | Rich plus `dwell_xcal` and threshold from the folder | union |

Rules every consumer follows:

- Optional Rich groups set capabilities only when exported: Time gives
  `per_channel_time`; X [u] gives `mass`; TuneSettings or StatusReport gives
  `instrument_state`; Info/UserName gives `user`; StartTime gives `timestamps`;
  StopTime gives `stop_times`; BrigidMS or PMIAnalyteLaser gives
  `sequence_fields` (any subset of their rows). Both Analog and Counter are
  needed for `analog_counter`; an unpaired group is ignored with a warning
  naming the missing group. Missing fields remain None or empty.
- Without Time rows, StartTime supplies calendar starts and sweep offsets
  are `np.arange(n_sweeps) * cycle_time_s` (1-D). None uses
  `DEFAULT_CYCLE_TIME_S = 0.4255`; explicit spacing must be finite and positive.
  StopTime includes acquisition overhead and is never used to estimate spacing.
  The session records `times_reconstructed=True`, `nominal_cycle_time_s`, and
  one warning. `open_qtegra` and `read_experiment_data` forward this keyword;
  recorded Time rows and per-spot input ignore it. Experiment import emits
  one reconstructed-times UserWarning.
- With Time but no StartTime, experiment import places acquisitions in
  sequence order on a continuous axis, advancing past the previous last
  sweep by its median spacing. Calendar gaps are unavailable. Merge requires
  StartTime for every Rich acquisition and otherwise raises ValueError.
  Merge retains reconstructed Rich times and skips their equality check
  against recorded companion times; channel order, Y and NaN checks remain.
- Checked on the full Zircon_04Nov26 session (245 acquisitions, 29 channels,
  2026-11-05; 91500 primary, NIST612 concentration standard, reduced to spot
  means): Y + Time + StartTime gives results bit-identical to the full Rich
  file. Y + Time alone (no StartTime) shifts session-drift-corrected values by
  at most 0.010 % in 206Pb/238U age and 0.055 % in 207Pb/235U age; Y +
  StartTime alone (reconstructed sweep times) by at most 0.003 % and 0.011 %.
  A Rich file without StartTime carries a session warning saying so.
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
  a session manifest's optional per-acquisition `sample_label` can correct
  the sample independently of the raw label. Both names use the same alias
  map; a case-insensitive match to the prefix or derived sample preserves
  the derived name. Actual overrides are recorded in experiment attrs and
  the reduction manifest as `sample_overrides`.
  The Rich file's `Laser Scan Position Number` is stored as the spot number
  and checked against the number parsed from the label.
- Sweep index 72 is empty for acquisitions with 72 sweeps and present for
  those with 73: empty cells are missing values, not zeros.

## Where it lands

- **UPbRPy** (`UPbRPy/io.py`): `qtegra.py` supplies Rich and per-spot readers
  and `merge`; `read_experiment_data` accepts a Rich CSV, per-spot folder or
  file, a folder containing these, a session bundle root, or a QtegraSession,
  and builds the experiment object. `calibration.py` supplies detector
  cross-calibration modes; Studio shows the loaded session's capabilities.
  Fixtures: a trimmed copy of this session (first 12 acquisitions, Rich slice,
  12 per-spot files) generated by a script from the full files on the laptop;
  the full files stay off git.
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
(`Zircon_demo_Rich.csv`, folder `zircon_04Nov26/`; the
historical Y-only export is no longer used).
