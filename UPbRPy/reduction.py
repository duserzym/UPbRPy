"""Final sweep-level and spot-level ratio reduction and aggregation."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .calibration import evaluate_correction_result
from .constants import common_U238_U235
from .isoplot import (
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
    return ((su/u)**2 + (sv/v)**2 - (sx/x)**2) * u * v /2 / (su * sv)

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

    # calculate the 207Pb/235U ratio and error based on the 206Pb/238U and 207Pb/206Pb raw ratio calculations
    depthpro_ratios['207Pb/235U_fractionation_corrected'] = depthpro_ratios['207Pb/206Pb_fractionation_corrected'] * depthpro_ratios['206Pb/238U_fractionation_corrected'] * common_U238_U235
    depthpro_ratios['207Pb/235U_fractionation_corrected_2e%'] = np.sqrt((depthpro_ratios['207Pb/206Pb_fractionation_corrected_2e%']**2 + depthpro_ratios['206Pb/238U_fractionation_corrected_2e%']**2))
    depthpro_ratios['207Pb/235U_fractionation_corrected_2e'] = depthpro_ratios['207Pb/235U_fractionation_corrected'] * depthpro_ratios['207Pb/235U_fractionation_corrected_2e%'] / 100
    for uncertainty_mode in ("no_calibration", "with_calibration"):
        percent_column = (
            f"207Pb/235U_fractionation_corrected_2e_{uncertainty_mode}%"
        )
        depthpro_ratios[percent_column] = np.sqrt(
            depthpro_ratios[
                f"207Pb/206Pb_fractionation_corrected_2e_{uncertainty_mode}%"
            ] ** 2
            + depthpro_ratios[
                f"206Pb/238U_fractionation_corrected_2e_{uncertainty_mode}%"
            ] ** 2
        )
        depthpro_ratios[
            f"207Pb/235U_fractionation_corrected_2e_{uncertainty_mode}"
        ] = (
            depthpro_ratios["207Pb/235U_fractionation_corrected"]
            * depthpro_ratios[percent_column]
            / 100
        )
    
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

    # calculate error correlations for the 207Pb/235U-206Pb/238U ratios
    x = depthpro_ratios['207Pb/206Pb_fractionation_corrected'] # 76 ratio
    u = depthpro_ratios['207Pb/235U_fractionation_corrected']  # 75 ratio
    v = depthpro_ratios['206Pb/238U_fractionation_corrected']  # 68 ratio

    sx = depthpro_ratios['207Pb/206Pb_fractionation_corrected_2e']/2 # 76 std
    su = depthpro_ratios['207Pb/235U_fractionation_corrected_2e']/2 # 75 std
    sv = depthpro_ratios['206Pb/238U_fractionation_corrected_2e']/2 # 68 std

    depthpro_ratios['err_corr'] = calc_err_corr_quotient(x, sx, u, su, v, sv)

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

    depthpro_ratios["207Pb/206Pb_age"] = [
        calc_76_date(value) if np.isfinite(value) and value > 0 else np.nan
        for value in depthpro_ratios[
            "207Pb/206Pb_fractionation_corrected"
        ]
    ]
    for uncertainty_mode in ("no_calibration", "with_calibration"):
        age_uncertainty_column = (
            f"207Pb/206Pb_age_2e_{uncertainty_mode}"
        )
        depthpro_ratios[age_uncertainty_column] = [
            calc_76_date_uncertainty(ratio, uncertainty)
            if np.isfinite(ratio) and ratio > 0
            else np.nan
            for ratio, uncertainty in zip(
                depthpro_ratios["207Pb/206Pb_fractionation_corrected"],
                depthpro_ratios[
                    "207Pb/206Pb_fractionation_corrected"
                    + f"_2e_{uncertainty_mode}"
                ],
            )
        ]
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
    records = []
    for spot_name, spot_data in fractionation_corrected_ratios.groupby(
        'spot_name', sort=False
    ):
        record = {
            'spot_name': spot_name,
            'sample': spot_data['sample'].iloc[0],
        }
        spot_mean_time = spot_data['Time'].mean()
        spot_size = spot_data.shape[0]

        # Preserve the mass-204 and Pb/Pb measurements needed by the
        # conventional 204-method correction. They are not session calibrated,
        # so their spot uncertainties contain counting/within-spot scatter only.
        for auxiliary_ratio in ("206Pb/204Pb", "206Pb/208Pb"):
            if auxiliary_ratio not in spot_data.columns:
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
            record[auxiliary_ratio] = valid_values.mean()
            auxiliary_sem = valid_values.std() / np.sqrt(valid_values.size)
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
            measured_mean = spot_data[isotope_ratio].mean()
            measured_sem = spot_data[isotope_ratio].std() / np.sqrt(spot_size)
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
            corrected_mean = spot_data[
                isotope_ratio + '_fractionation_corrected'
            ].mean()
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
        record['207Pb/235U_2e%'] = np.sqrt(
            record['207Pb/206Pb_2e%'] ** 2
            + record['206Pb/238U_2e%'] ** 2
        )
        record['207Pb/235U_2e'] = (
            record['207Pb/235U'] * record['207Pb/235U_2e%'] / 100
        )
        for uncertainty_mode in ("no_calibration", "with_calibration"):
            percent_column = f"207Pb/235U_2e_{uncertainty_mode}%"
            record[percent_column] = np.sqrt(
                record[f"207Pb/206Pb_2e_{uncertainty_mode}%"] ** 2
                + record[f"206Pb/238U_2e_{uncertainty_mode}%"] ** 2
            )
            record[f"207Pb/235U_2e_{uncertainty_mode}"] = (
                record["207Pb/235U"] * record[percent_column] / 100
            )

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
        record['err_corr'] = calc_err_corr_quotient(
            record['207Pb/206Pb'],
            record['207Pb/206Pb_2e'] / 2,
            record['207Pb/235U'],
            record['207Pb/235U_2e'] / 2,
            record['206Pb/238U'],
            record['206Pb/238U_2e'] / 2,
        )
        records.append(record)

    final_ratios = pd.DataFrame.from_records(records)
    # now let's reorganize the columns
    final_ratios_columns = final_ratios.columns.tolist()

    if '208Pb/232Th' in session_correction_results.keys():
        first_columns = ['spot_name', 'sample', 
                        '207Pb/235U', '207Pb/235U_2e%', 
                        '206Pb/238U', '206Pb/238U_2e%', 'err_corr', 
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
                '206Pb/238U', '206Pb/238U_2e%', 'err_corr', 
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
    'calc_68_76_discordance',
    'calc_depth_profile',
    'calc_ratio_mean_and_uncertainty',
]
