"""Final sweep-level and spot-level ratio reduction and aggregation."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .calibration import evaluate_correction_result
from .constants import common_U238_U235
from .ratios import (
    aggregate_ratio,
    normalize_ratio_aggregation_method,
    ratio_of_means_206pb_204pb,
    ratio_standard_error,
)
from .age import (
    calc_68_date,
    calc_68_date_uncertainty,
    calc_82_date,
    calc_82_date_uncertainty,
    calc_75_date,
    calc_75_date_uncertainty,
    calc_76_date,
    calc_76_date_uncertainty,
    propagate_reciprocal_uncertainty,
)


def calc_intercept_std(time, ratio):
    """Return residual scatter about a sweep-wise linear ratio trend.

    Parameters
    ----------
    time, ratio
        Equal-length arrays of sweep time and isotope ratio.

    Returns
    -------
    float
        Population standard deviation of residuals about the least-squares
        line.

    Notes
    -----
    Removing the fitted trend before calculating scatter prevents a genuine
    downhole ratio gradient from being counted as random analytical noise.
    The historical function name is retained for API compatibility even
    though the returned quantity is residual scatter, not the formal standard
    error of the fitted intercept.
    """
    time = np.array(time)
    ratio = np.array(ratio)
    # fit a line to the data
    p = np.polyfit(time, ratio, 1)
    # calculate the std of the y intercept
    intercept_std = np.std(ratio - np.polyval(p, time))
    return intercept_std

def calc_err_corr_quotient(x, sx, u, su, v, sv):
    """Infer correlation between two ratios that share a quotient.

    Parameters
    ----------
    x, sx
        Quotient ratio and its absolute one-sigma uncertainty. For a Wetherill
        point this is ``207Pb/206Pb``.
    u, su
        First plotted ratio and its absolute one-sigma uncertainty, normally
        ``207Pb/235U``.
    v, sv
        Second plotted ratio and its absolute one-sigma uncertainty, normally
        ``206Pb/238U``.

    Returns
    -------
    array-like
        Error-correlation coefficient between ``u`` and ``v``.

    Notes
    -----
    Because ``x`` is proportional to ``u / v``, relative variances satisfy
    ``(sx/x)^2 = (su/u)^2 + (sv/v)^2 - 2*rho*(su/u)*(sv/v)``. Rearranging that
    identity gives the returned correlation.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        return (
            ((su/u)**2 + (sv/v)**2 - (sx/x)**2)
            * u
            * v
            / 2
            / (su * sv)
        )


def calc_tw_err_corr_from_counts(n238, n206, n207):
    """Return sweep-level Tera--Wasserburg error correlation.

    Parameters
    ----------
    n238, n206, n207 : array-like
        Baseline-subtracted integrated counts for the three isotope channels.

    Returns
    -------
    numpy.ndarray
        Correlation between ``238U/206Pb`` and ``207Pb/206Pb`` under the
        independent-Poisson counting model.

    Notes
    -----
    Both Tera--Wasserburg coordinates contain the same ``206Pb`` denominator,
    so its counting fluctuation contributes positive covariance.  In relative
    form, ``Cov(x, y) / (x y) = 1 / n206``.  Nonpositive or nonfinite counts
    cannot support the Poisson ratio approximation and return ``NaN``.
    """
    n238, n206, n207 = np.broadcast_arrays(
        np.asarray(n238, dtype=float),
        np.asarray(n206, dtype=float),
        np.asarray(n207, dtype=float),
    )
    valid = (
        np.isfinite(n238)
        & np.isfinite(n206)
        & np.isfinite(n207)
        & (n238 > 0)
        & (n206 > 0)
        & (n207 > 0)
    )
    result = np.full(n206.shape, np.nan, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        denominator = np.sqrt(
            (1 / n238 + 1 / n206)
            * (1 / n207 + 1 / n206)
        )
        result[valid] = (1 / n206[valid]) / denominator[valid]
    return np.clip(result, -1.0, 1.0)


def calc_wetherill_err_corr_from_counts(n207, n206, n238):
    """Return sweep-level Wetherill correlation from integrated counts.

    The Wetherill coordinates are proportional to ``207Pb/238U`` and
    ``206Pb/238U``.  Their shared ``238U`` denominator contributes positive
    covariance, giving ``Cov(x, y) / (x y) = 1 / n238``.
    """
    n207, n206, n238 = np.broadcast_arrays(
        np.asarray(n207, dtype=float),
        np.asarray(n206, dtype=float),
        np.asarray(n238, dtype=float),
    )
    valid = (
        np.isfinite(n207)
        & np.isfinite(n206)
        & np.isfinite(n238)
        & (n207 > 0)
        & (n206 > 0)
        & (n238 > 0)
    )
    result = np.full(n238.shape, np.nan, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        denominator = np.sqrt(
            (1 / n207 + 1 / n238)
            * (1 / n206 + 1 / n238)
        )
        result[valid] = (1 / n238[valid]) / denominator[valid]
    return np.clip(result, -1.0, 1.0)


def calc_paired_ratio_correlation(ratio_x, ratio_y, method="arithmetic"):
    """Return the correlation of paired sweep ratios in estimator space.

    Arithmetic mode calculates the sample correlation of the paired corrected
    ratios.  Log-ratio mode calculates the correlation after taking natural
    logarithms, matching the space in which geometric spot means and their
    standard errors are estimated.
    """
    aggregation = normalize_ratio_aggregation_method(method)
    ratio_x, ratio_y = np.broadcast_arrays(
        np.asarray(ratio_x, dtype=float),
        np.asarray(ratio_y, dtype=float),
    )
    valid = np.isfinite(ratio_x) & np.isfinite(ratio_y)
    if aggregation == "logratio":
        valid &= (ratio_x > 0) & (ratio_y > 0)
    if np.count_nonzero(valid) < 2:
        return np.nan
    x = ratio_x[valid]
    y = ratio_y[valid]
    if aggregation == "logratio":
        x = np.log(x)
        y = np.log(y)
    if np.std(x, ddof=1) == 0 or np.std(y, ddof=1) == 0:
        return np.nan
    return float(np.clip(np.corrcoef(x, y)[0, 1], -1.0, 1.0))


def calc_tw_err_corr_from_pairs(ratio_86, ratio_76, method="arithmetic"):
    """Return spot-level Tera--Wasserburg correlation from paired sweeps."""
    return calc_paired_ratio_correlation(ratio_86, ratio_76, method)


def attenuate_correlation_for_independent_error(
    correlation,
    x_measurement_1s,
    y_measurement_1s,
    x_total_1s,
    y_total_1s,
):
    """Rescale a measurement correlation after independent errors are added.

    The measurement covariance is retained, while independently added session
    calibration scatter enlarges one or both marginal variances.  This is the
    production uncertainty policy described in the manuscript: no new
    cross-ratio calibration covariance is introduced.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        covariance = (
            np.asarray(correlation, dtype=float)
            * np.asarray(x_measurement_1s, dtype=float)
            * np.asarray(y_measurement_1s, dtype=float)
        )
        result = covariance / (
            np.asarray(x_total_1s, dtype=float)
            * np.asarray(y_total_1s, dtype=float)
        )
    return np.clip(result, -1.0, 1.0)

def calc_68_76_discordance(t68, t76, s68, s76):
    """Calculate conventional 206Pb/238U–207Pb/206Pb discordance.

    Parameters
    ----------
    t68 : float
        the 68 age
    t76 : float
        the 76 age
    s68 : float
        the uncertainty of the 68 age
    s76 : float
        the uncertainty of the 76 age

    Returns
    -------
    discordance : float
        the discordance between the 68 and 76 ages in percent
    discordance_2s : float
        Propagated relative two-sigma uncertainty of discordance, in percent.

    Notes
    -----
    Discordance is ``100 * (1 - t68 / t76)``. The age uncertainties are
    treated as independent; no cross-system covariance term is introduced.
    """
    disc = 1 - (t68 / t76)
    # calculate the propagated uncertainty of the discordance
    s = np.sqrt((1/t76)**2 * s68**2 + (t68/t76**2)**2 * s76**2)

    return disc * 100, s * 2 / disc * 100

def calc_depth_profile(fractionation_corrected_ratios, session_correction_results):
    """Calculate corrected sweep-wise ratios and their uncertainties.

    The input already contains downhole- and session-corrected ratios. For each
    sweep, this function propagates the raw counting-statistics uncertainty and
    the scalar residual scatter of the session correction. The downhole model
    is treated as systematic, and correlations introduced by either correction
    are not propagated between sweeps.

    Parameters
    ----------
    fractionation_corrected_ratios : pandas.DataFrame
        Sweep-wise ratios after downhole and session correction. Each
        calibrated ratio must retain its raw ``*_2e`` counting uncertainty.
    session_correction_results : dict
        Session correction models and their residual one-sigma scatter.

    Returns
    -------
    depthpro_ratios : pandas.DataFrame
        Depth-profile ratios, dates, and explicit uncertainty branches.

    Notes
    -----
    ``*_2e_no_calibration`` contains counting uncertainty only.
    ``*_2e_with_calibration`` adds the session residual scatter in quadrature.
    The historical ``*_2e`` columns alias the calibration-inclusive branch.
    """
    # Every operation below is row-wise; no per-spot DataFrame split is needed.
    # Evaluating whole columns avoids repeated copies and concatenations while
    # preserving the input row order used by the depth-profile explorer.
    depthpro_ratios = fractionation_corrected_ratios.copy()
    session_time = depthpro_ratios['Time'].to_numpy(dtype=float)
    for isotope_ratio, session_result in session_correction_results.items():
        measured_ratio = depthpro_ratios[isotope_ratio]
        measured_ratio_1sigma = depthpro_ratios[isotope_ratio + '_2e'] / 2
        session_factor = evaluate_correction_result(session_result, session_time)
        session_scatter = session_result['std']
        corrected_ratio = depthpro_ratios[
            isotope_ratio + '_fractionation_corrected'
        ]
        no_calibration_1sigma = np.abs(
            corrected_ratio * measured_ratio_1sigma / measured_ratio
        )
        with_calibration_1sigma = corrected_ratio * np.sqrt(
            (measured_ratio_1sigma / measured_ratio) ** 2
            + (session_scatter / session_factor) ** 2
        )

        depthpro_ratios[isotope_ratio + '_session_correction_1s'] = session_scatter
        fit_times = np.asarray(
            session_result.get('fit_observations', {}).get('time', []),
            dtype=float,
        )
        if fit_times.size:
            depthpro_ratios[
                isotope_ratio + '_session_correction_extrapolated'
            ] = (
                (session_time < np.nanmin(fit_times))
                | (session_time > np.nanmax(fit_times))
            )
        else:
            depthpro_ratios[
                isotope_ratio + '_session_correction_extrapolated'
            ] = False
        for suffix, uncertainty_1sigma in (
            ("_2e_no_calibration", no_calibration_1sigma),
            ("_2e_with_calibration", with_calibration_1sigma),
        ):
            depthpro_ratios[
                isotope_ratio + "_fractionation_corrected" + suffix
            ] = uncertainty_1sigma * 2
            depthpro_ratios[
                isotope_ratio + "_fractionation_corrected" + suffix + "%"
            ] = uncertainty_1sigma * 2 / corrected_ratio * 100

        # The historical ``_2e`` columns remain aliases for uncertainties that
        # include session-calibration scatter. New code can make the choice
        # explicit by selecting one of the two named variants above.
        depthpro_ratios[
            isotope_ratio + '_fractionation_corrected_2e%'
        ] = depthpro_ratios[
            isotope_ratio + '_fractionation_corrected_2e_with_calibration%'
        ]
        depthpro_ratios[
            isotope_ratio + '_fractionation_corrected_2e'
        ] = depthpro_ratios[
            isotope_ratio + '_fractionation_corrected_2e_with_calibration'
        ]

    # The product 207Pb/206Pb * 206Pb/238U is exactly 207Pb/238U at the
    # measurement level.  Its Poisson uncertainty must therefore omit the
    # cancelling 206Pb term instead of treating the two component ratios as
    # independent.  Independent session-scatter terms are added afterwards.
    ratio_75_column = '207Pb/235U_fractionation_corrected'
    ratio_76_column = '207Pb/206Pb_fractionation_corrected'
    ratio_68_column = '206Pb/238U_fractionation_corrected'
    depthpro_ratios[ratio_75_column] = (
        depthpro_ratios[ratio_76_column]
        * depthpro_ratios[ratio_68_column]
        * common_U238_U235
    )
    count_columns = {'238U', '206Pb', '207Pb'}
    relative_76_no = np.abs(
        depthpro_ratios[f'{ratio_76_column}_2e_no_calibration']
        / 2
        / depthpro_ratios[ratio_76_column]
    )
    relative_68_no = np.abs(
        depthpro_ratios[f'{ratio_68_column}_2e_no_calibration']
        / 2
        / depthpro_ratios[ratio_68_column]
    )
    if count_columns.issubset(depthpro_ratios.columns):
        with np.errstate(divide='ignore', invalid='ignore'):
            relative_75_no = np.sqrt(
                1 / depthpro_ratios['207Pb']
                + 1 / depthpro_ratios['238U']
            )
        wetherill_rho_measurement = calc_wetherill_err_corr_from_counts(
            depthpro_ratios['207Pb'],
            depthpro_ratios['206Pb'],
            depthpro_ratios['238U'],
        )
    else:
        # Legacy ratio-only intermediates cannot recover the cancelling 206Pb
        # covariance.  Retain their historical independent-input uncertainty.
        relative_75_no = np.sqrt(relative_76_no**2 + relative_68_no**2)
        wetherill_rho_measurement = calc_err_corr_quotient(
            depthpro_ratios[ratio_76_column],
            relative_76_no * depthpro_ratios[ratio_76_column],
            depthpro_ratios[ratio_75_column],
            relative_75_no * depthpro_ratios[ratio_75_column],
            depthpro_ratios[ratio_68_column],
            relative_68_no * depthpro_ratios[ratio_68_column],
        )
    relative_76_with = np.abs(
        depthpro_ratios[f'{ratio_76_column}_2e_with_calibration']
        / 2
        / depthpro_ratios[ratio_76_column]
    )
    relative_68_with = np.abs(
        depthpro_ratios[f'{ratio_68_column}_2e_with_calibration']
        / 2
        / depthpro_ratios[ratio_68_column]
    )
    added_session_variance = (
        np.clip(relative_76_with**2 - relative_76_no**2, 0.0, None)
        + np.clip(relative_68_with**2 - relative_68_no**2, 0.0, None)
    )
    relative_75_with = np.sqrt(relative_75_no**2 + added_session_variance)
    for uncertainty_mode, relative_75 in (
        ('no_calibration', relative_75_no),
        ('with_calibration', relative_75_with),
    ):
        absolute_column = f'{ratio_75_column}_2e_{uncertainty_mode}'
        depthpro_ratios[absolute_column] = (
            2 * depthpro_ratios[ratio_75_column] * relative_75
        )
        depthpro_ratios[absolute_column + '%'] = 200 * relative_75
    depthpro_ratios[ratio_75_column + '_2e'] = depthpro_ratios[
        ratio_75_column + '_2e_with_calibration'
    ]
    depthpro_ratios[ratio_75_column + '_2e%'] = depthpro_ratios[
        ratio_75_column + '_2e_with_calibration%'
    ]
    
    # calculate the 238U/206Pb ratio and error based on the 206Pb/238U ratio and error
    depthpro_ratios['238U/206Pb_fractionation_corrected'] = 1 / depthpro_ratios['206Pb/238U_fractionation_corrected']
    depthpro_ratios['238U/206Pb_fractionation_corrected_2e'] = propagate_reciprocal_uncertainty(
        depthpro_ratios['206Pb/238U_fractionation_corrected'],
        depthpro_ratios['206Pb/238U_fractionation_corrected_2e'],
    )
    depthpro_ratios['238U/206Pb_fractionation_corrected_2e%'] = (
        depthpro_ratios['238U/206Pb_fractionation_corrected_2e']
        / depthpro_ratios['238U/206Pb_fractionation_corrected']
        * 100
    )
    for uncertainty_mode in ("no_calibration", "with_calibration"):
        uncertainty_column = (
            f"206Pb/238U_fractionation_corrected_2e_{uncertainty_mode}"
        )
        reciprocal_column = (
            f"238U/206Pb_fractionation_corrected_2e_{uncertainty_mode}"
        )
        depthpro_ratios[reciprocal_column] = propagate_reciprocal_uncertainty(
            depthpro_ratios["206Pb/238U_fractionation_corrected"],
            depthpro_ratios[uncertainty_column],
        )
        depthpro_ratios[reciprocal_column + "%"] = (
            depthpro_ratios[reciprocal_column]
            / depthpro_ratios["238U/206Pb_fractionation_corrected"]
            * 100
        )

    # Fixed corrections preserve the counting-statistical Wetherill
    # covariance. Independent session scatter enlarges the marginal errors but
    # does not add cross-ratio calibration covariance in the production model.
    depthpro_ratios['err_corr_no_calibration'] = np.clip(
        wetherill_rho_measurement, -1.0, 1.0
    )
    depthpro_ratios['err_corr_with_calibration'] = (
        attenuate_correlation_for_independent_error(
            wetherill_rho_measurement,
            depthpro_ratios[
                ratio_75_column + '_2e_no_calibration'
            ] / 2,
            depthpro_ratios[
                ratio_68_column + '_2e_no_calibration'
            ] / 2,
            depthpro_ratios[
                ratio_75_column + '_2e_with_calibration'
            ] / 2,
            depthpro_ratios[
                ratio_68_column + '_2e_with_calibration'
            ] / 2,
        )
    )
    depthpro_ratios['err_corr'] = depthpro_ratios[
        'err_corr_with_calibration'
    ]

    # Tera--Wasserburg sweep covariance comes directly from the shared 206Pb
    # denominator.  Fixed fractionation corrections preserve this correlation.
    # Independent session scatter increases the marginal errors without adding
    # covariance, so the inclusive correlation is attenuated accordingly.
    if count_columns.issubset(depthpro_ratios.columns):
        tw_rho_measurement = calc_tw_err_corr_from_counts(
            depthpro_ratios['238U'],
            depthpro_ratios['206Pb'],
            depthpro_ratios['207Pb'],
        )
    else:
        # Legacy intermediate tables may contain ratios without the original
        # integrated counts.  Keep those tables usable, but do not invent a
        # Tera--Wasserburg covariance that the available data cannot support.
        tw_rho_measurement = np.full(len(depthpro_ratios), np.nan)
    depthpro_ratios['tw_rho_no_calibration'] = tw_rho_measurement
    depthpro_ratios['tw_rho_with_calibration'] = (
        attenuate_correlation_for_independent_error(
            tw_rho_measurement,
            depthpro_ratios[
                '238U/206Pb_fractionation_corrected_2e_no_calibration'
            ] / 2,
            depthpro_ratios[
                '207Pb/206Pb_fractionation_corrected_2e_no_calibration'
            ] / 2,
            depthpro_ratios[
                '238U/206Pb_fractionation_corrected_2e_with_calibration'
            ] / 2,
            depthpro_ratios[
                '207Pb/206Pb_fractionation_corrected_2e_with_calibration'
            ] / 2,
        )
    )
    depthpro_ratios['tw_rho'] = depthpro_ratios[
        'tw_rho_with_calibration'
    ]

    # age calculations
    depthpro_ratios['207Pb/235U_age'] = calc_75_date(depthpro_ratios['207Pb/235U_fractionation_corrected'])
    depthpro_ratios['207Pb/235U_age_2e'] = calc_75_date_uncertainty(
        depthpro_ratios['207Pb/235U_fractionation_corrected'],
        depthpro_ratios['207Pb/235U_fractionation_corrected_2e'],
    )
    depthpro_ratios['207Pb/235U_age_2e%'] = (
        depthpro_ratios['207Pb/235U_age_2e'] / depthpro_ratios['207Pb/235U_age'] * 100
    )
    depthpro_ratios['206Pb/238U_age'] = calc_68_date(depthpro_ratios['206Pb/238U_fractionation_corrected'])
    depthpro_ratios['206Pb/238U_age_2e'] = calc_68_date_uncertainty(
        depthpro_ratios['206Pb/238U_fractionation_corrected'],
        depthpro_ratios['206Pb/238U_fractionation_corrected_2e'],
    )
    depthpro_ratios['206Pb/238U_age_2e%'] = (
        depthpro_ratios['206Pb/238U_age_2e'] / depthpro_ratios['206Pb/238U_age'] * 100
    )
    for isotope_ratio, uncertainty_function in (
        ("207Pb/235U", calc_75_date_uncertainty),
        ("206Pb/238U", calc_68_date_uncertainty),
    ):
        ratio_column = isotope_ratio + "_fractionation_corrected"
        age_column = isotope_ratio + "_age"
        for uncertainty_mode in ("no_calibration", "with_calibration"):
            age_uncertainty_column = (
                isotope_ratio + f"_age_2e_{uncertainty_mode}"
            )
            depthpro_ratios[age_uncertainty_column] = uncertainty_function(
                depthpro_ratios[ratio_column],
                depthpro_ratios[
                    ratio_column + f"_2e_{uncertainty_mode}"
                ],
            )
            depthpro_ratios[age_uncertainty_column + "%"] = (
                depthpro_ratios[age_uncertainty_column]
                / depthpro_ratios[age_column]
                * 100
            )
    if '208Pb/232Th_fractionation_corrected' in depthpro_ratios.columns:
        ratio_82 = depthpro_ratios['208Pb/232Th_fractionation_corrected']
        ratio_82_2e = depthpro_ratios[
            '208Pb/232Th_fractionation_corrected_2e'
        ]
        depthpro_ratios['208Pb/232Th_age'] = calc_82_date(ratio_82)
        depthpro_ratios['208Pb/232Th_age_2e'] = calc_82_date_uncertainty(
            ratio_82,
            ratio_82_2e,
        )
        depthpro_ratios['208Pb/232Th_age_2e%'] = (
            depthpro_ratios['208Pb/232Th_age_2e']
            / depthpro_ratios['208Pb/232Th_age']
            * 100
        )
        for uncertainty_mode in ("no_calibration", "with_calibration"):
            age_uncertainty_column = (
                f"208Pb/232Th_age_2e_{uncertainty_mode}"
            )
            depthpro_ratios[age_uncertainty_column] = calc_82_date_uncertainty(
                ratio_82,
                depthpro_ratios[
                    f"208Pb/232Th_fractionation_corrected_2e_{uncertainty_mode}"
                ],
            )
            depthpro_ratios[age_uncertainty_column + "%"] = (
                depthpro_ratios[age_uncertainty_column]
                / depthpro_ratios["208Pb/232Th_age"]
                * 100
            )

    ratio_76 = depthpro_ratios[
        "207Pb/206Pb_fractionation_corrected"
    ].to_numpy(dtype=float)
    valid_ratio_76 = np.isfinite(ratio_76) & (ratio_76 > 0)
    age_76 = np.full(ratio_76.shape, np.nan, dtype=float)
    age_76[valid_ratio_76] = calc_76_date(ratio_76[valid_ratio_76])
    depthpro_ratios["207Pb/206Pb_age"] = age_76
    for uncertainty_mode in ("no_calibration", "with_calibration"):
        age_uncertainty_column = (
            f"207Pb/206Pb_age_2e_{uncertainty_mode}"
        )
        uncertainty_76 = depthpro_ratios[
            "207Pb/206Pb_fractionation_corrected"
            + f"_2e_{uncertainty_mode}"
        ].to_numpy(dtype=float)
        age_uncertainty_76 = np.full(
            ratio_76.shape,
            np.nan,
            dtype=float,
        )
        age_uncertainty_76[valid_ratio_76] = calc_76_date_uncertainty(
            ratio_76[valid_ratio_76],
            uncertainty_76[valid_ratio_76],
        )
        depthpro_ratios[age_uncertainty_column] = age_uncertainty_76
        depthpro_ratios[age_uncertainty_column + "%"] = (
            depthpro_ratios[age_uncertainty_column]
            / depthpro_ratios["207Pb/206Pb_age"]
            * 100
        )
    depthpro_ratios["207Pb/206Pb_age_2e"] = depthpro_ratios[
        "207Pb/206Pb_age_2e_with_calibration"
    ]
    depthpro_ratios["207Pb/206Pb_age_2e%"] = depthpro_ratios[
        "207Pb/206Pb_age_2e_with_calibration%"
    ]

    depthpro_ratios.attrs = {}
    return depthpro_ratios

def calc_ratio_mean_and_uncertainty(
    fractionation_corrected_ratios,
    session_correction_results,
    sep='_',
    ratio_aggregation="arithmetic",
):
    """Aggregate corrected sweeps into spot ratios, ages, and uncertainties.

    For highly radiogenic phases, uncertainty combines the measurement standard
    error within each spot with the residual scatter of the session correction.
    Groups are traversed once and accumulated as records, avoiding incremental
    DataFrame insertion and fragmentation.

    Parameters
    ----------
    fractionation_corrected_ratios : pandas.DataFrame
        Plateau sweeps after downhole and session correction. Sweeps rejected
        by the saved plateau filter must already be absent.
    session_correction_results : dict
        Session correction models and their residual one-sigma scatter.
    sep : str
        Retained for backward compatibility with older notebooks; column names
        use the package's canonical slash notation.
    ratio_aggregation : str
        ``"arithmetic"`` preserves the historical mean-of-ratios estimator.
        ``"logratio"`` calculates geometric means and log-space standard
        errors following the compositional treatment of McLean et al. (2016).
        The reported ``206Pb/204Pb`` spot value is an intentional exception:
        it always uses the ratio of the integrated 206Pb and Hg-corrected
        mass-204 plateau means because individual mass-204 sweeps are commonly
        count limited.

    Returns
    -------
    pandas.DataFrame
        One row per spot containing calibrated ratios, dates, discordance,
        correlation, and uncertainty branches with and without session
        calibration scatter.

    Notes
    -----
    The analytical branch uses the standard error of the retained plateau
    sweeps. The calibration-inclusive branch adds the scalar session residual
    scatter in quadrature. Coefficient covariance and cross-ratio calibration
    covariance are deliberately not propagated.
    """
    aggregation = normalize_ratio_aggregation_method(ratio_aggregation)
    records = []
    for spot_name, spot_data in fractionation_corrected_ratios.groupby(
        'spot_name', sort=False
    ):
        record = {
            'spot_name': spot_name,
            'sample': spot_data['sample'].iloc[0],
            'ratio_aggregation': aggregation,
        }
        spot_mean_time = spot_data['Time'].mean()

        # Preserve the mass-204 and Pb/Pb measurements needed by the
        # conventional 204-method correction. They are not session calibrated,
        # so their spot uncertainties contain counting/within-spot scatter only.
        for auxiliary_ratio in ("206Pb/204Pb", "206Pb/208Pb"):
            if auxiliary_ratio not in spot_data.columns:
                continue
            if auxiliary_ratio == "206Pb/204Pb":
                required_mass_columns = {"206Pb", "204Pb"}
                missing_mass_columns = required_mass_columns - set(
                    spot_data.columns
                )
                if missing_mass_columns:
                    raise KeyError(
                        "Ratio-of-means 206Pb/204Pb requires the underlying "
                        "206Pb and Hg-corrected 204Pb signals. Recalculate raw "
                        "ratios with the current pipeline instead of reusing a "
                        "legacy intermediate ratio table."
                    )
                ratio_value, ratio_1e = ratio_of_means_206pb_204pb(
                    spot_data["206Pb"],
                    spot_data["204Pb"],
                    spot_data.get("204Pb_2e"),
                )
                record[auxiliary_ratio] = ratio_value
                record[auxiliary_ratio + "_2e"] = 2 * ratio_1e
                record[auxiliary_ratio + "_aggregation"] = "ratio_of_means"
                continue
            auxiliary_values = pd.to_numeric(
                spot_data[auxiliary_ratio], errors="coerce"
            )
            valid_values = auxiliary_values[
                np.isfinite(auxiliary_values) & (auxiliary_values > 0)
            ]
            if valid_values.empty:
                record[auxiliary_ratio] = np.nan
                record[auxiliary_ratio + "_2e"] = np.nan
                continue
            record[auxiliary_ratio] = aggregate_ratio(
                valid_values,
                aggregation,
            )
            auxiliary_sem = ratio_standard_error(
                valid_values,
                aggregation,
            )
            if not np.isfinite(auxiliary_sem):
                uncertainty_column = auxiliary_ratio + "_2e"
                auxiliary_sem = (
                    pd.to_numeric(
                        spot_data.loc[valid_values.index, uncertainty_column],
                        errors="coerce",
                    ).mean()
                    / 2
                    if uncertainty_column in spot_data.columns
                    else np.nan
                )
            record[auxiliary_ratio + "_2e"] = 2 * auxiliary_sem

        for isotope_ratio, session_result in session_correction_results.items():
            measured_mean = aggregate_ratio(
                spot_data[isotope_ratio],
                aggregation,
            )
            measured_sem = ratio_standard_error(
                spot_data[isotope_ratio],
                aggregation,
            )
            if not np.isfinite(measured_sem):
                uncertainty_column = isotope_ratio + '_2e'
                measured_sem = (
                    spot_data[uncertainty_column].mean() / 2
                    if uncertainty_column in spot_data.columns
                    else 0.0
                )

            session_factor = float(
                evaluate_correction_result(session_result, spot_mean_time)
            )
            session_scatter = session_result['std']
            corrected_mean = aggregate_ratio(
                spot_data[isotope_ratio + '_fractionation_corrected'],
                aggregation,
            )
            no_calibration_1sigma = np.abs(
                corrected_mean * measured_sem / measured_mean
            )
            with_calibration_1sigma = corrected_mean * np.sqrt(
                (measured_sem / measured_mean) ** 2
                + (session_scatter / session_factor) ** 2
            )
            fit_times = np.asarray(
                session_result.get('fit_observations', {}).get('time', []),
                dtype=float,
            )

            record[isotope_ratio] = corrected_mean
            record[isotope_ratio + '_session_correction_1s'] = session_scatter
            record[isotope_ratio + '_session_correction_extrapolated'] = bool(
                fit_times.size
                and (
                    spot_mean_time < np.nanmin(fit_times)
                    or spot_mean_time > np.nanmax(fit_times)
                )
            )
            for suffix, uncertainty_1sigma in (
                ("_2e_no_calibration", no_calibration_1sigma),
                ("_2e_with_calibration", with_calibration_1sigma),
            ):
                record[isotope_ratio + suffix] = uncertainty_1sigma * 2
                record[isotope_ratio + suffix + "%"] = (
                    uncertainty_1sigma * 2 / corrected_mean * 100
                )
            record[isotope_ratio + '_2e%'] = record[
                isotope_ratio + "_2e_with_calibration%"
            ]
            record[isotope_ratio + '_2e'] = record[
                isotope_ratio + "_2e_with_calibration"
            ]

        # Derived ratios and dates are calculated in a plain record before the
        # final DataFrame is constructed, avoiding fragmented scalar inserts.
        record['207Pb/235U'] = (
            record['207Pb/206Pb']
            * record['206Pb/238U']
            * common_U238_U235
        )
        paired_76 = pd.to_numeric(
            spot_data['207Pb/206Pb_fractionation_corrected'],
            errors='coerce',
        )
        paired_68 = pd.to_numeric(
            spot_data['206Pb/238U_fractionation_corrected'],
            errors='coerce',
        )
        rho_76_68_measurement = calc_paired_ratio_correlation(
            paired_76,
            paired_68,
            aggregation,
        )
        relative_76_no = (
            record['207Pb/206Pb_2e_no_calibration']
            / 2
            / record['207Pb/206Pb']
        )
        relative_68_no = (
            record['206Pb/238U_2e_no_calibration']
            / 2
            / record['206Pb/238U']
        )
        covariance_term = (
            0.0
            if not np.isfinite(rho_76_68_measurement)
            else 2 * rho_76_68_measurement * relative_76_no * relative_68_no
        )
        relative_75_no = np.sqrt(max(
            relative_76_no**2 + relative_68_no**2 + covariance_term,
            0.0,
        ))
        relative_76_with = (
            record['207Pb/206Pb_2e_with_calibration']
            / 2
            / record['207Pb/206Pb']
        )
        relative_68_with = (
            record['206Pb/238U_2e_with_calibration']
            / 2
            / record['206Pb/238U']
        )
        relative_75_with = np.sqrt(
            relative_75_no**2
            + max(relative_76_with**2 - relative_76_no**2, 0.0)
            + max(relative_68_with**2 - relative_68_no**2, 0.0)
        )
        for uncertainty_mode, relative_75 in (
            ('no_calibration', relative_75_no),
            ('with_calibration', relative_75_with),
        ):
            record[f'207Pb/235U_2e_{uncertainty_mode}'] = (
                2 * record['207Pb/235U'] * relative_75
            )
            record[f'207Pb/235U_2e_{uncertainty_mode}%'] = 200 * relative_75
        record['207Pb/235U_2e'] = record[
            '207Pb/235U_2e_with_calibration'
        ]
        record['207Pb/235U_2e%'] = record[
            '207Pb/235U_2e_with_calibration%'
        ]

        # For R75 = k R76 R68, the measurement covariance with R68 is
        # k[R68 Cov(R76,R68) + R76 Var(R68)].  Expressing this in relative
        # terms yields the correlation below without inventing a calibration
        # covariance between the two independently fitted session corrections.
        if np.isfinite(rho_76_68_measurement) and relative_75_no > 0:
            relative_covariance_75_68 = (
                rho_76_68_measurement * relative_76_no * relative_68_no
                + relative_68_no**2
            )
            wetherill_rho_measurement = (
                relative_covariance_75_68
                / (relative_75_no * relative_68_no)
                if relative_68_no > 0
                else np.nan
            )
        else:
            wetherill_rho_measurement = np.nan

        record['238U/206Pb'] = 1 / record['206Pb/238U']
        record['238U/206Pb_2e'] = propagate_reciprocal_uncertainty(
            record['206Pb/238U'], record['206Pb/238U_2e']
        )
        record['238U/206Pb_2e%'] = (
            record['238U/206Pb_2e'] / record['238U/206Pb'] * 100
        )
        for uncertainty_mode in ("no_calibration", "with_calibration"):
            uncertainty_column = f"238U/206Pb_2e_{uncertainty_mode}"
            record[uncertainty_column] = propagate_reciprocal_uncertainty(
                record["206Pb/238U"],
                record[f"206Pb/238U_2e_{uncertainty_mode}"],
            )
            record[uncertainty_column + "%"] = (
                record[uncertainty_column] / record["238U/206Pb"] * 100
            )

        record['207Pb/235U_age'] = calc_75_date(record['207Pb/235U'])
        record['207Pb/235U_age_2e'] = calc_75_date_uncertainty(
            record['207Pb/235U'], record['207Pb/235U_2e']
        )
        record['207Pb/235U_age_2e%'] = (
            record['207Pb/235U_age_2e'] / record['207Pb/235U_age'] * 100
        )

        record['206Pb/238U_age'] = calc_68_date(record['206Pb/238U'])
        record['206Pb/238U_age_2e'] = calc_68_date_uncertainty(
            record['206Pb/238U'], record['206Pb/238U_2e']
        )
        record['206Pb/238U_age_2e%'] = (
            record['206Pb/238U_age_2e'] / record['206Pb/238U_age'] * 100
        )
        for isotope_ratio, uncertainty_function in (
            ("207Pb/235U", calc_75_date_uncertainty),
            ("206Pb/238U", calc_68_date_uncertainty),
        ):
            age_column = isotope_ratio + "_age"
            for uncertainty_mode in ("no_calibration", "with_calibration"):
                age_uncertainty_column = (
                    isotope_ratio + f"_age_2e_{uncertainty_mode}"
                )
                record[age_uncertainty_column] = uncertainty_function(
                    record[isotope_ratio],
                    record[f"{isotope_ratio}_2e_{uncertainty_mode}"],
                )
                record[age_uncertainty_column + "%"] = (
                    record[age_uncertainty_column] / record[age_column] * 100
                )

        if '208Pb/232Th' in record:
            record['208Pb/232Th_age'] = calc_82_date(
                record['208Pb/232Th']
            )
            record['208Pb/232Th_age_2e'] = calc_82_date_uncertainty(
                record['208Pb/232Th'],
                record['208Pb/232Th_2e'],
            )
            record['208Pb/232Th_age_2e%'] = (
                record['208Pb/232Th_age_2e']
                / record['208Pb/232Th_age']
                * 100
            )
            for uncertainty_mode in ("no_calibration", "with_calibration"):
                age_uncertainty_column = (
                    f"208Pb/232Th_age_2e_{uncertainty_mode}"
                )
                record[age_uncertainty_column] = calc_82_date_uncertainty(
                    record["208Pb/232Th"],
                    record[f"208Pb/232Th_2e_{uncertainty_mode}"],
                )
                record[age_uncertainty_column + "%"] = (
                    record[age_uncertainty_column]
                    / record["208Pb/232Th_age"]
                    * 100
                )

        record['207Pb/206Pb_age'] = calc_76_date(record['207Pb/206Pb'])
        record['207Pb/206Pb_age_2e'] = calc_76_date_uncertainty(
            record['207Pb/206Pb'], record['207Pb/206Pb_2e']
        )
        record['207Pb/206Pb_age_2e%'] = (
            record['207Pb/206Pb_age_2e'] / record['207Pb/206Pb_age'] * 100
        )
        for uncertainty_mode in ("no_calibration", "with_calibration"):
            age_uncertainty_column = (
                f"207Pb/206Pb_age_2e_{uncertainty_mode}"
            )
            record[age_uncertainty_column] = calc_76_date_uncertainty(
                record["207Pb/206Pb"],
                record[f"207Pb/206Pb_2e_{uncertainty_mode}"],
            )
            record[age_uncertainty_column + "%"] = (
                record[age_uncertainty_column]
                / record["207Pb/206Pb_age"]
                * 100
            )

        record['disc%'], record['disc_2e%'] = calc_68_76_discordance(
            record['206Pb/238U_age'],
            record['207Pb/206Pb_age'],
            record['206Pb/238U_age_2e'] / 2,
            record['207Pb/206Pb_age_2e'] / 2,
        )
        record['err_corr_no_calibration'] = float(np.clip(
            wetherill_rho_measurement, -1.0, 1.0
        ))
        record['err_corr_with_calibration'] = float(
            attenuate_correlation_for_independent_error(
                wetherill_rho_measurement,
                record['207Pb/235U_2e_no_calibration'] / 2,
                record['206Pb/238U_2e_no_calibration'] / 2,
                record['207Pb/235U_2e_with_calibration'] / 2,
                record['206Pb/238U_2e_with_calibration'] / 2,
            )
        )
        record['err_corr'] = record['err_corr_with_calibration']

        # Estimate spot-level Tera--Wasserburg covariance from the paired
        # corrected sweeps, rather than assuming zero correlation.  In
        # log-ratio mode the pairing is evaluated in log space, matching the
        # estimator used for the reported geometric mean.
        paired_86 = 1 / pd.to_numeric(
            spot_data['206Pb/238U_fractionation_corrected'],
            errors='coerce',
        )
        paired_76 = pd.to_numeric(
            spot_data['207Pb/206Pb_fractionation_corrected'],
            errors='coerce',
        )
        tw_rho_measurement = calc_tw_err_corr_from_pairs(
            paired_86,
            paired_76,
            aggregation,
        )
        record['tw_rho_no_calibration'] = tw_rho_measurement
        record['tw_rho_with_calibration'] = float(
            attenuate_correlation_for_independent_error(
                tw_rho_measurement,
                record['238U/206Pb_2e_no_calibration'] / 2,
                record['207Pb/206Pb_2e_no_calibration'] / 2,
                record['238U/206Pb_2e_with_calibration'] / 2,
                record['207Pb/206Pb_2e_with_calibration'] / 2,
            )
        )
        record['tw_rho'] = record['tw_rho_with_calibration']
        records.append(record)

    final_ratios = pd.DataFrame.from_records(records)
    # now let's reorganize the columns
    final_ratios_columns = final_ratios.columns.tolist()

    if '208Pb/232Th' in session_correction_results.keys():
        first_columns = ['spot_name', 'sample', 
                        '207Pb/235U', '207Pb/235U_2e%', 
                        '206Pb/238U', '206Pb/238U_2e%', 'err_corr', 'tw_rho',
                        '207Pb/235U_age', '207Pb/235U_age_2e%', '207Pb/235U_age_2e',
                        '206Pb/238U_age', '206Pb/238U_age_2e%', '206Pb/238U_age_2e',
                        '208Pb/232Th_age', '208Pb/232Th_age_2e%', '208Pb/232Th_age_2e',
                        'disc%', 'disc_2e%',
                        '238U/206Pb', '238U/206Pb_2e%',
                        '207Pb/206Pb', '207Pb/206Pb_2e%',
                        '208Pb/232Th', '208Pb/232Th_2e%',
                        '207Pb/235U_2e', 
                        '206Pb/238U_2e',
                        '238U/206Pb_2e',  
                        '207Pb/206Pb_2e',
                        '208Pb/232Th_2e']
    else:
        first_columns = ['spot_name', 'sample', 
                '207Pb/235U', '207Pb/235U_2e%', 
                '206Pb/238U', '206Pb/238U_2e%', 'err_corr', 'tw_rho',
                '207Pb/235U_age', '207Pb/235U_age_2e%', '207Pb/235U_age_2e',
                '206Pb/238U_age', '206Pb/238U_age_2e%', '206Pb/238U_age_2e',
                'disc%', 'disc_2e%',
                '238U/206Pb', '238U/206Pb_2e%',
                '207Pb/206Pb', '207Pb/206Pb_2e%',
                '207Pb/235U_2e', 
                '206Pb/238U_2e',
                '238U/206Pb_2e',  
                '207Pb/206Pb_2e',]

    other_columns = [i for i in final_ratios_columns if i not in first_columns]
    final_ratios = final_ratios[first_columns + other_columns]
    final_ratios = final_ratios.sort_values(by='spot_name').reset_index(drop=True)
    final_ratios.attrs = {}
    return final_ratios

__all__ = [
    'calc_intercept_std',
    'calc_err_corr_quotient',
    'calc_tw_err_corr_from_counts',
    'calc_wetherill_err_corr_from_counts',
    'calc_paired_ratio_correlation',
    'calc_tw_err_corr_from_pairs',
    'attenuate_correlation_for_independent_error',
    'calc_68_76_discordance',
    'calc_depth_profile',
    'calc_ratio_mean_and_uncertainty',
]
