# Key ideas

This page explains the words used throughout this site. Each term has a short
definition and, where useful, an everyday comparison. Hover over an underlined
term on any page to see its definition.

## How one measurement is organized

A laser fires at one spot on a grain. The mass spectrometer then measures a
fixed list of isotopes, one after another, again and again, for about a
minute. Every pass through the list takes a fraction of a second.

```text
Acquisition "91500_12"  (one laser spot, about 60 s)
│
├── sweep 0   202Hg 204Pb 206Pb 207Pb 208Pb 232Th 238U ...   (≈ 0.4 s)
├── sweep 1   202Hg 204Pb 206Pb 207Pb 208Pb 232Th 238U ...
├── sweep 2   ...
│     ▲ laser off: gas blank (background)
│     ▼ laser on: sample signal
└── sweep 140 ...
```

| Term | Plain meaning | Example |
| --- | --- | --- |
| **Session** | Everything measured in one sitting, usually one day of analyses. | `Zircon_04Nov26` |
| **Acquisition** (or **spot**) | One laser spot: all the data recorded while the laser hit one place. | `91500_12` |
| **Label** | The name typed for an acquisition. The part before the underscore is the sample, the number after it is the spot. | `91500_12` → sample `91500`, spot 12 |
| **Channel** (or **analyte**) | One isotope the instrument measures. | `206Pb`, `238U` |
| **Sweep** | One pass through every channel. An acquisition is a few hundred sweeps. A depth profile has one value per sweep. | sweep 37 |
| **Dwell time** | How long the detector listens to one channel during one sweep. | 0.075 s for Pb |
| **Gas blank** (baseline) | The sweeps before the laser fires. They measure the background, which is subtracted from the signal. | first ≈ 20 s |
| **Signal window** | The sweeps chosen as real sample signal after review. | 25–55 s |
| **Count rate (cps)** | Ions per second. This is what Qtegra writes in its files, in the row called `Y`. | 10,000 cps |
| **Counts** | The actual number of ions detected in one sweep on one channel: count rate × dwell time. | 10,000 cps × 0.075 s = 750 ions |
| **Pulse counting / analog** | The detector's two modes. At low signal it counts single ions (pulse counting). At high signal it switches to measuring a current (analog), which is then converted to cps. | 238U in a U-rich zircon may be analog |
| **Reference material** (standard) | A material of known composition, measured in the same session, used to correct the unknowns. | 91500 zircon, NIST 612 glass |

## Two kinds of uncertainty

UPbRPy reports two different uncertainties, and they answer different questions.

- **Sweep uncertainty** — how precise one sweep can be, given how few ions
  were counted in it. This comes from counting statistics, so it depends on
  the number of ions, not on the count rate. See
  [Counting and dwell time](counting-and-dwell.md).
- **Spot uncertainty** — how well the average of many sweeps is known. This
  comes from how much the sweeps actually scatter around their average, so
  it does not need to know how many ions were counted.

Depth profiles use sweep uncertainties. Spot dates and concentrations use
spot uncertainties.
