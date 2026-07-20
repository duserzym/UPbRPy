"""Final sweep-level and spot-level ratio reduction and aggregation."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .calibration import evaluate_correction_result
from .constants import common_U238_U235
from .isoplot import (
    calc_68_date,
    calc_68_date_uncertainty,
    calc_75_date,
    calc_75_date_uncertainty,
    calc_76_date,
    calc_76_date_uncertainty,
    propagate_reciprocal_uncertainty,
)


def calc_intercept_std(time, ratio):
    '''
    function for calculating the standard deviation of the measured isotope ratio
        insted of just calculating the mean and std of the sweep-wise ratios,
        we fit a line through the sweep-wise data with a slope and y intercept and calculate the std of the y intercept
        in this way, we account for the systematic ratio variation as a function of time within a spot (i.e. the age variation as a function of space within a spot)

    Parameters
    ----------
    time : numpy.ndarray
        an array containing the time values
    ratio : numpy.ndarray
        an array containing the ratio values

    Returns
    -------
    intercept_std : float
        the standard deviation of the y intercept of the linear fit
    '''
    time = np.array(time)
    ratio = np.array(ratio)
    # fit a line to the data
    p = np.polyfit(time, ratio, 1)
    # calculate the std of the y intercept
    intercept_std = np.std(ratio - np.polyval(p, time))
    return intercept_std

def calc_err_corr_quotient(x, sx, u, su, v, sv):
    '''
    function for calculating the error correlation between two ratios
        this is useful for calculating the error correlation between ratios such as 207Pb/235U-206Pb/238U 
        in the example 75-68 case, x = 207Pb/206Pb, u = 207Pb/235U, v = 206Pb/238U, x = u/v, constant of 58 ratio does not matter
        in terms of relative error (%), we have sx**2 = su**2 + sv**2 - 2*rho*su*sv
        where rho is the error correlation between u and v
        thus rho = (su/u)**2 + (sv/v)**2 - (sx/x)**2 * u * v /2 / (su * sv)
    '''
    return ((su/u)**2 + (sv/v)**2 - (sx/x)**2) * u * v /2 / (su * sv)

def calc_68_76_discordance(t68, t76, s68, s76):
    '''
    function for calculating the discordance between 68 and 76 ratios

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
        the propagated relative 2 standard error of the discordance
    '''
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
        a pandas dataframe containing the fractionation corrected ratios
    session_correction_results : dict
        a dictionary containing the session-wide fractionation correction plots and fits for each isotope ratio

    Returns
    -------
    depthpro_ratios : pandas.DataFrame
        a pandas dataframe containing the depth profile ratios and their associated uncertainties
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
        propagated_1sigma = corrected_ratio * np.sqrt(
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
        depthpro_ratios[
            isotope_ratio + '_fractionation_corrected_2e%'
        ] = propagated_1sigma * 2 / corrected_ratio * 100
        depthpro_ratios[
            isotope_ratio + '_fractionation_corrected_2e'
        ] = propagated_1sigma * 2

    # calculate the 207Pb/235U ratio and error based on the 206Pb/238U and 207Pb/206Pb raw ratio calculations
    depthpro_ratios['207Pb/235U_fractionation_corrected'] = depthpro_ratios['207Pb/206Pb_fractionation_corrected'] * depthpro_ratios['206Pb/238U_fractionation_corrected'] * common_U238_U235
    depthpro_ratios['207Pb/235U_fractionation_corrected_2e%'] = np.sqrt((depthpro_ratios['207Pb/206Pb_fractionation_corrected_2e%']**2 + depthpro_ratios['206Pb/238U_fractionation_corrected_2e%']**2))
    depthpro_ratios['207Pb/235U_fractionation_corrected_2e'] = depthpro_ratios['207Pb/235U_fractionation_corrected'] * depthpro_ratios['207Pb/235U_fractionation_corrected_2e%'] / 100
    
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
        a pandas dataframe containing the fractionation corrected ratios
    session_correction_results : dict
        a dictionary containing the session-wide fractionation correction plots and fits, and std of the fractionation correction factor for each isotope ratio
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
            propagated_1sigma = corrected_mean * np.sqrt(
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
            record[isotope_ratio + '_2e%'] = (
                propagated_1sigma * 2 / corrected_mean * 100
            )
            record[isotope_ratio + '_2e'] = propagated_1sigma * 2

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

        record['238U/206Pb'] = 1 / record['206Pb/238U']
        record['238U/206Pb_2e'] = propagate_reciprocal_uncertainty(
            record['206Pb/238U'], record['206Pb/238U_2e']
        )
        record['238U/206Pb_2e%'] = (
            record['238U/206Pb_2e'] / record['238U/206Pb'] * 100
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

        record['207Pb/206Pb_age'] = calc_76_date(record['207Pb/206Pb'])
        record['207Pb/206Pb_age_2e'] = calc_76_date_uncertainty(
            record['207Pb/206Pb'], record['207Pb/206Pb_2e']
        )
        record['207Pb/206Pb_age_2e%'] = (
            record['207Pb/206Pb_age_2e'] / record['207Pb/206Pb_age'] * 100
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
