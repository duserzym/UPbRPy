# Discordia and isochron regression

`UPbRPy.regression` fits straight lines through U-Pb analyses with correlated
x-y uncertainties and turns them into concordia-intercept ages. Studio's
**Fit** control in the spot-mean and depth-profile concordia views uses it;
scripts can call it directly. The module has no GUI dependency.

```python
from UPbRPy.regression import Anchor, fit_discordia, bootstrap_discordia

fit = fit_discordia(x, y, sx, sy, rho, space="tera_wasserburg", model=1)
fit.lower.age_ma, fit.lower.two_sigma_ma        # lower intercept, Ma
fit.common_pb_76, fit.common_pb_76_sigma         # y-intercept (1 s)
fit.line.mswd, fit.line.p_value, fit.n_used, fit.n_total

boot = bootstrap_discordia(x, y, sx, sy, rho, fit=fit, n_resamples=2000, seed=1)
boot.interval("lower_ma")   # median, mean, sd, 2.5 and 97.5 percentiles
```

Inputs are ratios with one-sigma absolute uncertainties and their error
correlation, in the coordinates of the chosen diagram:

| `space` | x | y | correlation column in UPbRPy results |
|---|---|---|---|
| `tera_wasserburg` | 238U/206Pb | 207Pb/206Pb | `tw_rho` |
| `wetherill` | 207Pb/235U | 206Pb/238U | `err_corr` |

Rows with non-finite values or non-positive uncertainties are dropped and
counted: `n_used` of `n_total`.

## The line

Each analysis *i* has covariance Σᵢ. For a line y = a + b x the residual
rᵢ = yᵢ − a − b xᵢ has variance vᵢ(b) = σyᵢ² + b²σxᵢ² − 2bρᵢσxᵢσyᵢ, and

S(a, b) = Σ rᵢ² / vᵢ(b)

is twice the profile negative log-likelihood. MSWD = S/(n − k), with k free
line parameters, and p is the chi-square survival probability.

**Model 1** (default) minimises S. Unanchored fits use the York et al. (2004)
iteration. The iteration is damped per data set when its slope update changes
sign: with error correlations close to one (one Tuve-gc1 spot has ρ = 0.99997
in Wetherill space) the undamped fixed point alternates between two slopes and
never converges, and the result is not the minimum of S. Line uncertainties are
the inverse Hessian of S/2, which is what IsoplotR's discordia reports; York's
closed-form covariance is kept as `line.cov_york` (they differ by up to about
7 % when correlations approach one, and agree otherwise). As in IsoplotR, when
p < 0.05 the reported covariance (`line.cov_reported`) is multiplied by the
MSWD; `line.overdispersed` says so.

**Model 2** ignores the analytical uncertainties: orthogonal (total least
squares) regression, with uncertainties from the scatter of the perpendicular
residuals (the curvature of n/2·log Σd²). Orthogonal regression depends on the
axis scales, so, as IsoplotR does, Wetherill data are regressed in
Tera-Wasserburg coordinates and the line is mapped back (a straight line in one
diagram is a straight line in the other).

**Model 3** adds a dispersion term w, estimated with the line by maximum
likelihood (IsoplotR's model 3, Vermeesch 2018). Each analysis's covariance
becomes Σᵢ + w² Jᵢ Jᵢᵀ, where Jᵢ is the displacement of that analysis per unit
of the dispersing quantity, and the objective adds ½ Σ log(1 + w² Jᵢᵀ Σᵢ⁻¹ Jᵢ):

- unanchored, age-anchored and Stacey-Kramers fits: w is a dispersion of the
  concordia (radiogenic end-member) age, in Ma. In Wetherill space Jᵢ is the
  concordia tangent; in Tera-Wasserburg space it is the tangent mapped to each
  analysis, Jᵢ = (−y′ₜ Xᵢ², Xᵢ (x′ₜ/U − Yᵢ y′ₜ)), with (xₜ, yₜ) the Wetherill
  concordia point and U = 238U/235U;
- fits anchored to a common 207Pb/206Pb: w is a dispersion of that ratio.

When the fitted w is zero, the line uncertainties are those of the fit with w
fixed at zero.

## Anchors

`Anchor(kind, value, sigma)`:

- `"common_pb"`: through a common 207Pb/206Pb (the Tera-Wasserburg
  y-intercept; in Wetherill space the slope 1/(r76·U)). With `sigma > 0` the
  value is a Gaussian prior for models 1 and 2 and the fixed dispersion for
  model 3, as in IsoplotR.
- `"stacey_kramers"`: through Stacey and Kramers (1975) common Pb evaluated at
  the fitted lower-intercept age. The age and the line are solved together as
  one parameter (as IsoplotR's anchor 3); this is the converged point of
  "fit, evaluate Stacey-Kramers at the new age, refit".
- `"age"`: through concordia at `value` Ma; `sigma > 0` makes the age a prior
  (models 1 and 2) or the fixed age dispersion (model 3).

## Intercepts and their uncertainties

Intercepts are the roots of F(t) = a + b X(t) − Y(t) on concordia, searched
between 0 and 5,000 Ma (Wetherill also between −3,000 and 0 Ma, so a line that
passes below the origin gives a negative lower intercept, flagged `negative
age`; IsoplotR parameterises log t and cannot return one). Ages above
4,567 Ma are flagged `older than the Earth`. A line that misses concordia
returns no intercepts and a note.

Age uncertainties follow from implicit differentiation,
∂t/∂(a, b) = −(1, X(t)) / F′(t), applied to the reported line covariance. With
`include_decay_uncertainty=True` the λ238, λ235 (and, in Tera-Wasserburg,
238U/235U) uncertainties are added the same way; both versions are always
computed (`sigma_ma`, `sigma_with_decay_ma`). Defaults are UPbRPy's constants:
λ238 = 1.55125e-10 /a ± 0.11 %, λ235 = 9.8485e-10 /a ± 0.14 % (2 s; Jaffey et
al. 1971), 238U/235U = 137.818 ± 0.045 (2 s; Hiess et al. 2012). In Wetherill
space the common 207Pb/206Pb is 1/(b·U).

## Depth-profile intervals

In the depth-profile view the fitted points are the per-interval (sweep)
ellipses of the chosen acquisitions: one fit per sample by default, or one
through all selected acquisitions. Successive intervals of one acquisition are
not independent (the signal drifts smoothly with depth), so treating them as
independent would understate the uncertainty. Pass `groups` (the acquisition of
each interval) and `order` (its time in the ablation): the pooled lag-1
autocorrelation φ of the standardised residuals within acquisitions is
estimated (not centred per acquisition, so an acquisition sitting off the line
also counts), and when φ > 0 the line covariance is multiplied by
(1 + φ)/(1 − φ), the long-run variance factor of an AR(1) process. The
chi-square probability uses the effective number of points n(1 − φ)/(1 + φ).
The bootstrap resamples whole acquisitions (`method="resample", groups=...`);
with fewer than three acquisitions it resamples blocks of ceil(n^(1/3))
successive intervals within each acquisition (moving-block bootstrap,
Künsch 1989).

## Bootstrap

`bootstrap_discordia` refits many resampled data sets at once:

- `method="parametric"` (default for spot means): as Isoplot.jl does, every
  analysis is redrawn from its bivariate normal error ellipse (plus the fitted
  model-3 dispersion) and the line is refitted. Isoplot.jl refits each draw by
  ordinary least squares and summarises the intercept draws by their mean and
  standard deviation; here each draw is refitted with the same estimator as
  the analytical fit (York, or orthogonal regression for model 2), and results
  are summarised by percentiles as well as mean and standard deviation.
- `method="resample"`: analyses (or whole acquisitions with `groups`) drawn
  with replacement, keeping their own uncertainties. Model 2 always resamples,
  because its fit ignores the analytical uncertainties.
- Anchors with an uncertainty are refitted as the same estimator: a prior on
  the Tera-Wasserburg intercept is exactly an extra observation at x = 0 with
  that y uncertainty, and a prior on an age is, to first order, an extra
  observation on concordia with its error ellipse along the curve; their
  centres are drawn from the prior in every resample. (Model 2 with a prior
  draws the anchor and holds it fixed, an approximation.) Stacey-Kramers
  anchors are iterated to convergence in every resample; with decay-constant
  uncertainty the constants are drawn too.
- The generator is `numpy.random.default_rng(seed)`; the seed and the number
  of resamples are stored on the result and in exported records.

Everything is vectorised over resamples: York iterations run as one array
operation with a per-row convergence mask (converged rows are dropped from
later iterations), and concordia intersections are bracketed on an age grid and
refined by safeguarded Newton steps for all resamples together.
`benchmarks/bootstrap_timing.py` on an Apple M4 Pro (median of five):

| points | resamples | method | time |
|---|---|---|---|
| 50 | 2,000 | parametric | 19-25 ms |
| 200 | 10,000 | parametric | 180-300 ms |
| 2,400 intervals in 86 acquisitions | 2,000 | by acquisition | about 0.2-0.3 s |

## Validation

**IsoplotR 6.6** (R, `ludwig()`, format 1), on the 14 synthetic Wetherill
analyses in `tests/test_regression.py` (600 Ma radiogenic Pb mixed with common
Pb of 207Pb/206Pb = 0.86), with IsoplotR's constants set to UPbRPy's. IsoplotR
reports unscaled model-1 uncertainties from `ludwig()`:

| model | anchor | IsoplotR t ± 1 s (Ma) | UPbRPy | IsoplotR a0 | UPbRPy | IsoplotR MSWD | UPbRPy |
|---|---|---|---|---|---|---|---|
| 1 | none | 590.117 ± 5.740 | 590.116 ± 5.740 | 0.84439 | 0.84439 | 1.2295 | 1.2294 |
| 2 | none | 587.560 ± 7.652 | 587.561 ± 7.607 | 0.83868 | 0.83869 | | |
| 3 | none | 590.118 ± 5.740 | 590.116 ± 5.740 | 0.84439 | 0.84439 | | |
| 1 | common 0.86 | 597.635 ± 3.860 | 597.634 ± 3.860 | 0.86 | 0.86 | 1.3798 | 1.3797 |
| 1 | Stacey-Kramers | 605.417 ± 3.981 | 605.387 ± 3.981 | 0.87760 | 0.87754 | 2.2271 | 2.2226 |
| 1 | age 600 | 600 | 600 | 0.85549 | 0.85549 | 1.3645 | 1.3644 |

The York fit reproduces IsoplotR's `york()` on the Pearson-York data to nine
significant figures (slope, intercept, MSWD) and six (uncertainties).

**Isoplot.jl** (its own test suite): two analyses at 22.6602 ± 0.0175 /
0.40864 ± 0.00017 and 33.6602 / 0.50864 (ρ = 0.83183), with Isoplot.jl's decay
constants, give a lower intercept of 1318.12 ± 2.04 Ma and an upper intercept
of 4601.82 ± 1.53 Ma, both analytically and by bootstrap, as Isoplot.jl's
tests require.

Differences from IsoplotR found on real apatite data, and why:

- **Tera-Wasserburg fits are made in Tera-Wasserburg coordinates.** IsoplotR
  converts every two-dimensional U-Pb data set to Wetherill coordinates and
  fits there, so its "Tera-Wasserburg" result is a Wetherill fit. Because error
  propagation between the diagrams is first order, the two differ for
  common-Pb-rich data (by about 2 % in age and 15 % in uncertainty on one
  apatite sample). Fitting
  UPbRPy's Wetherill ratios in Wetherill space reproduces IsoplotR to 0.001 %
  in age and 0.04 % in uncertainty.
- **Anchors with an uncertainty.** On the same apatite data, IsoplotR 6.6
  returned, for a common-Pb prior and for an age prior, points whose negative
  log-likelihood (S/2 plus the prior) is higher than at UPbRPy's solution
  (17.72 against 16.07, and 18.51 against 15.33), and that lie outside the
  range between the prior value and the free fit. UPbRPy reports the minimum.
- **Model 3** depends on how the dispersion enters each coordinate system
  (above); with the dispersion at zero it reproduces model 1, as IsoplotR's
  Wetherill fits do.
- **Degenerate data** (a concordant cluster): the intercepts are not defined
  by the data; IsoplotR and UPbRPy return different, equally meaningless
  values with very large uncertainties.

## Exporting fits from Python

Studio exports data only. Fits are for viewing and are saved with the project.
The following library functions remain available to notebook users.

`fit_record(fit, boot)` returns a JSON-safe record;
`write_isochron_fits(records, folder, stem)` writes `<stem>.csv`, `<stem>.md`
and `<stem>_footnotes.txt` (method, model, anchor, decay constants, bootstrap
method, seed and resamples, n of N). `export_reduction_bundle(...,
isochron_fits=records)` adds them to a reduction bundle and its manifest.

## References

- Hiess, J., Condon, D. J., McLean, N. and Noble, S. R. (2012) 238U/235U
  systematics in terrestrial uranium-bearing minerals. Science 335, 1610-1614.
- Jaffey, A. H. et al. (1971) Precision measurement of half-lives and specific
  activities of 235U and 238U. Physical Review C 4, 1889-1906.
- Künsch, H. R. (1989) The jackknife and the bootstrap for general stationary
  observations. Annals of Statistics 17, 1217-1241.
- Ludwig, K. R. (1998) On the treatment of concordant uranium-lead ages.
  Geochimica et Cosmochimica Acta 62, 665-676.
- Ludwig, K. R. (2003) Isoplot 3.00. Berkeley Geochronology Center Special
  Publication 4.
- Stacey, J. S. and Kramers, J. D. (1975) Approximation of terrestrial lead
  isotope evolution by a two-stage model. Earth and Planetary Science Letters
  26, 207-221.
- Vermeesch, P. (2018) IsoplotR: a free and open toolbox for geochronology.
  Geoscience Frontiers 9, 1479-1493.
- Vermeesch, P. (2020) Unifying the U-Pb and Th-Pb methods: joint isochron
  regression and common Pb correction. Geochronology 2, 119-131.
- York, D., Evensen, N. M., Martínez, M. L. and De Basabe Delgado, J. (2004)
  Unified equations for the slope, intercept, and standard errors of the best
  straight line. American Journal of Physics 72, 367-375.
- IsoplotR (P. Vermeesch, GPL-3) and Isoplot.jl (JuliaGeochronology, MIT) were
  used as references for validation; no code was translated from them.
