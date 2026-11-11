# Choosing an export

!!! tip "Recommendation"
    Export **both** the Rich single CSV and the per-spot folder, and keep them
    together. Either one alone can be reduced; together they hold everything.

Qtegra can export the same session in several ways. They all contain the
measured signal, but they differ in what else they keep.

| What you need | Rich single CSV | Per-spot folder | UPbR single CSV |
| --- | :---: | :---: | :---: |
| Signal (count rate) of every isotope in every sweep | :material-check: | :material-check: | :material-check: |
| Time of every sweep | :material-check: | :material-check: | :material-close: |
| When each spot started and stopped | :material-check: both | start only | :material-close: |
| Dwell time per isotope | worked out from the data | :material-check: recorded | :material-close: |
| Separate pulse-counting and analog detector readings | :material-check: | :material-close: | :material-close: |
| Sample-list notes, laser settings, user name | :material-check: | :material-close: | :material-close: |
| Can UPbRPy reduce it? | Yes | Yes | **No** |

## Why the UPbR single CSV is not accepted

It has no timing: no sweep times and no start times. Without them UPbRPy
cannot tell where the gas blank ends, how the signal changes with depth, or
how the instrument drifted across the session. Opening one gives an error
that says which export to use instead.

## Why both together

- The **Rich CSV** is the most complete record. It is the only export with
  both detector readings, which UPbRPy can use to check the instrument's
  conversion between its two detector modes.
- The **per-spot folder** records the dwell time and the detector conversion
  factor directly, so nothing has to be worked out from the data.

Put both in one folder (or the per-spot files in a subfolder) and open the
folder. UPbRPy matches them by spot name and checks that the signals agree.

For the full file layouts, see [Rich single CSV](../data-model/rich-csv.md) and
[Per-spot CSV and folder](../data-model/per-spot.md). The reasoning behind
this choice is in the [decision record](../qtegra_export_formats.md).
