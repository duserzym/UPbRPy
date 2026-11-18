# Future opt-in bias correction: threshold options

Requested at the 2026-11-18 Studio demo: a later bias correction of unknowns
should use a threshold so it is not extrapolated too far. This correction is not built. The Bias graph projects unknowns onto a trend
for inspection and shows implied corrections; it changes no date or result.

A future implementation would require an explicit choice per session and per
x attribute. Candidate limits include:

- Restrict x to the reference-material range, with a configurable small margin
  in the fitted coordinate (log10 for a log fit).
- Cap the absolute predicted correction as a percentage of the unknown date.
- Require sufficient trend significance (a maximum p-value), enough controls,
  and an acceptable MSWD; overdispersion would inflate trend uncertainty.
- Enable only attributes with a justified physical interpretation and adequate
  control coverage. A limit suitable for U concentration need not suit alpha dose.

These limits could be combined. Unknowns failing a limit would retain only the
uncorrected date and an explanation. Positive measured-minus-accepted bias
implies a younger corrected date: date / (1 + bias/100).

Reporting would retain uncorrected dates and add a separate corrected-date
column. A footnote would name the attribute, linear or log fit, slope ± 2σ,
reference-material x-range, enabled thresholds, and the propagated trend
uncertainty alongside the original analytical uncertainty. The correction
choice and fit would be recorded for reproducibility.

Everything stays unapplied by default. Correction would be off unless enabled
per session, and would never be silently applied to reference materials.
