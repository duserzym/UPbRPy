# Manuscript workflow compatibility

This audit maps the methods-paper workflow to the shared numerical core used
by notebooks and UPbRPy Studio. It records scientific behavior, rather than a
second implementation of the workflow.

| Manuscript stage | Shared implementation | Audited behavior |
|---|---|---|
| Baseline, on-peak, and total-signal review | `UPbRPy.selection`, `UPbRPy.io` | On-peak and total-signal limits remain separate, nested selections. |
| Baseline subtraction | `UPbRPy.io.subtract_baseline` | Isotope ratios use baseline-subtracted signals, not internally normalized signals. |
| Element concentrations | `UPbRPy.concentration` | Spot means use the on-peak interval; depth concentrations use the complete reviewed signal interval. The configured mineral-specific internal channel is passed explicitly. |
| Ratio construction | `UPbRPy.ratios.calc_raw_ratios` | Plateau filtering affects session standards and spot means but never removes sweeps from downhole fitting or depth profiles. Hg-corrected 204Pb and the 206Pb/204Pb ratio-of-means path are retained. |
| Downhole correction | `UPbRPy.fractionation` | Primary-standard full signals are aligned at reviewed signal start. Downhole-model uncertainty is treated as systematic and is not independently added to every sweep. |
| Session correction | `UPbRPy.fractionation` | Downhole-corrected primary-standard plateau means define absolute-session-time correction. Scalar residual session scatter is available as the calibration-inclusive uncertainty branch; coefficient covariance is not propagated. |
| Arithmetic/log-ratio choice | `UPbRPy.ratios`, `UPbRPy.reduction`, `UPbRPy.pipeline` | One explicit selector is propagated consistently through downhole pooling, session-standard means, and final spot means. |
| Wetherill covariance | `UPbRPy.reduction` | Sweep counting statistics cancel the shared 206Pb denominator in derived 207Pb/235U. Spot covariance is calculated from paired accepted sweeps in the selected arithmetic or log-ratio space. |
| Tera–Wasserburg covariance | `UPbRPy.reduction` | Sweep correlation is calculated from the shared 206Pb denominator; spot correlation is calculated from paired 238U/206Pb and 207Pb/206Pb sweeps. Independent session scatter attenuates the calibration-inclusive correlation. |
| Common-Pb decisions | `UPbRPy.isochron`, `UPbRPy.results` | Uncorrected and 204-, 207-, and 208-method results remain parallel until the recorded preferred-result decision. |
| Physical depth | `UPbRPy.depth` | Signal-relative time remains primary. Physical depth is added only by an explicit pulse–depth calibration with sweep bounds, uncertainty, and extrapolation flags. |
| Publication and audit exports | `UPbRPy.results`, `UPbRPy.reproducibility` | Concise tables, complete audit tables, project manifests, and replay notebooks use the same pipeline configuration. |

## Intentional uncertainty scope

- Sweep isotope-ratio uncertainty begins with integrated-count Poisson terms.
- Spot-mean uncertainty is based on the observed variation of accepted sweeps.
- The downhole correction curve is systematic within a reduction and is not
  added as independent point-to-point noise.
- The calibration-inclusive branch adds residual session scatter but does not
  propagate fitted session-coefficient covariance.
- A user-supplied common-Pb composition can carry its stated uncertainty;
  a Stacey–Kramers composition carries the explicitly configured model
  uncertainty.
- Physical depth uncertainty is absent until a depth calibration is supplied.

The automated regression suite exercises these invariants. Representative
zircon, apatite, rutile, and titanite notebooks in the companion database are
also executed as end-to-end release checks.
