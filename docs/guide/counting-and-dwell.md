# Counting and dwell time

!!! abstract "In short"
    Qtegra reports **ions per second**, but the detector only listened to each
    isotope for a small fraction of a second in each sweep. Counting
    uncertainty depends on the number of ions actually detected, which is
    much smaller than the per-second number. UPbRPy now multiplies by the
    listening time (the dwell time) before calculating counting
    uncertainties. Before 2026-11-11 it did not, so sweep-level uncertainties
    were several times too small.

## An everyday comparison

Imagine counting cars on a road to estimate traffic. You count 3 cars in 5
seconds and report "36 cars per minute". Someone else counts 36 cars in a full
minute and also reports "36 cars per minute". The two numbers are the same,
but the second estimate is far more reliable, because it rests on 36 cars
rather than 3. The rate alone hides how many cars were actually seen.

The mass spectrometer works the same way. In each sweep it listens to each
isotope for a short **dwell time**, typically 5 to 100 milliseconds, then
reports the result scaled up to one second.

## A worked example

One sweep measures 206Pb at **10,000 counts per second** with a dwell time of
**0.075 s**.

|  | Treating cps as ions (old) | Using the dwell time (now) |
| --- | --- | --- |
| Ions assumed | 10,000 | 10,000 × 0.075 = **750** |
| Counting uncertainty (1σ) | √10,000 = 100 ions → **1.0 %** | √750 ≈ 27 ions → **3.65 %** |

The second column is correct. Counting uncertainty follows the square root of
the number of ions detected (Poisson statistics), so using the rate instead of
the real number of ions underestimates it by a factor of
1 / √(dwell time in seconds):

| Dwell time | Uncertainty was too small by |
| --- | --- |
| 0.100 s | 3.2× |
| 0.075 s | 3.65× |
| 0.025 s | 6.3× |
| 0.005 s | 14× |

## What this changes, and what it does not

**Affected** (anything built from the counting uncertainty of single sweeps):

- uncertainties of individual sweeps in depth profiles;
- the error correlations between ratios in each sweep (error ellipses);
- the counting part of the 206Pb/204Pb uncertainty;
- uncertainties of trace-element concentrations in depth profiles;
- any fit that weights sweeps by their uncertainty, such as a regression line
  through all sweeps of a grain.

**Not affected:**

- the ratios, dates and concentrations themselves;
- spot averages and their uncertainties, because those come from how much the
  sweeps actually scatter, not from counting statistics.

## How UPbRPy finds the dwell time

You do not need to type it in. UPbRPy takes it from the data in this order:

1. **Written in the file.** Qtegra's per-spot export records the dwell time of
   every isotope. This is the most reliable source.
2. **Worked out from the numbers.** Qtegra's Rich export does not record the
   dwell time, but it leaves a fingerprint. When very few ions arrive, as in
   the gas blank, a channel can only show 0, 1, 2, 3 … ions, which in counts
   per second become 0, 13.33, 26.67, 40 … for a 0.075 s dwell. The step
   between these values is 1 / dwell time. On a session exported both ways,
   this recovered the recorded dwell time of every pulse-counting isotope to
   within 0.1 %.
3. **Estimated from timing.** Isotopes measured only in analog mode (very
   high signals, such as the internal-standard element) never show single-ion
   steps. UPbRPy then uses the time between one isotope's reading and the
   next. This includes the detector's settling time, so it is an
   overestimate.

So both the Rich CSV and the per-spot export work. Keeping the per-spot export
alongside the Rich CSV is still recommended, because it records the dwell time
directly.

## Effect on real data

For an apatite session (dwell 0.075 s for Pb, 0.025 s for U), we compared how
far each sweep's ratio lies from its spot's median, measured in units of its
stated uncertainty. If the uncertainties are right, this spread should be
about 1.

| Reference apatite | 207Pb/206Pb before | after | 238U/206Pb before | after |
| --- | --- | --- | --- | --- |
| Tuve | 4.0 | 1.15 | 8.1 | 1.8 |
| MADUCSB | 3.4 | 1.0 | 5.3 | 1.4 |
| OL2 | 4.1 | 1.15 | 9.8 | 2.4 |

207Pb/206Pb now behaves as counting statistics predict. 238U/206Pb still
scatters 1.4 to 2.4 times more than counting alone explains; that extra
scatter is real (for example, small fluctuations in the plasma and in the
laser aerosol), so counting uncertainty is a minimum, not the whole story.

## For developers

The [acquisition model](../data-model/acquisition-model.md#counting-dwell)
documents `UPbRPy.qtegra.counting_dwell(session)`,
`experiment.attrs["counting_dwell"]` and the `dwell_s` arguments.
