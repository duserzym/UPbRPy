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
    '''
    function for calculating the depth profile of the ratios 
        for each sweep, the 68 and 76 ratios are the downhole and session fractionation corrected ratios
        the associated uncertainties of the ratios are calculated based on propagating raw ratio uncertainties 
        assuming Poisson error of the counting statistics of each analyte and
        the scalar residual scatter of the session correction. The downhole
        correction is treated as systematic and correction correlations are
        not propagated between sweeps.

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
    '''
    depthpro_ratios = pd.DataFrame()
    for spot_name in fractionation_corrected_ratios['spot_name'].unique():
        this_spot_data = fractionation_corrected_ratios[fractionation_corrected_ratios['spot_name'] == spot_name].copy()
        for isotope_ratio in session_correction_results.keys():
            # get the measured mean and std
            this_spot_measured_ratio = this_spot_data[isotope_ratio]
            # use the original measured ratio std for the uncertainty propagation
            this_spot_measured_ratio_se = this_spot_data[isotope_ratio+'_2e']/2 # sweep-wise Poisson error

            # get the session-wide fractionation correction and se at each sweep time
            session_result = session_correction_results[isotope_ratio]
            this_spot_this_ratio_fractionation_mean = evaluate_correction_result(
                session_result, this_spot_data['Time']
            )
            this_ratio_fractionation_se = session_result['std']

            # get the fractionation corrected mean
            this_spot_fractionation_corrected = this_spot_data[isotope_ratio + '_fractionation_corrected']
            # calculate the propagated uncertainty
            this_spot_this_ratio_propagated_uncertainty = this_spot_fractionation_corrected * np.sqrt((this_spot_measured_ratio_se/this_spot_measured_ratio)**2 + (this_ratio_fractionation_se/this_spot_this_ratio_fractionation_mean)**2)

            # print('contributions of error from fractionation correction:', this_ratio_fractionation_std/this_spot_this_ratio_fractionation_mean*100)
            # save results
            this_spot_data[isotope_ratio + '_session_correction_1s'] = this_ratio_fractionation_se
            fit_times = np.asarray(
                session_result.get('fit_observations', {}).get('time', []),
                dtype=float,
            )
            if fit_times.size:
                this_spot_data[isotope_ratio + '_session_correction_extrapolated'] = (
                    (this_spot_data['Time'] < np.nanmin(fit_times))
                    | (this_spot_data['Time'] > np.nanmax(fit_times))
                )
            else:
                this_spot_data[isotope_ratio + '_session_correction_extrapolated'] = False
            this_spot_data[isotope_ratio + '_fractionation_corrected_2e%'] = this_spot_this_ratio_propagated_uncertainty * 2 / this_spot_fractionation_corrected * 100
            this_spot_data[isotope_ratio + '_fractionation_corrected_2e'] = this_spot_this_ratio_propagated_uncertainty * 2 
        depthpro_ratios = pd.concat([depthpro_ratios, this_spot_data])
    # depthpro_ratios = depthpro_ratios.sort_values(by='Time').reset_index(drop=True)

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

def calc_ratio_mean_and_uncertainty(fractionation_corrected_ratios, session_correction_results, sep='_'):
    '''
    function for calculating the mean isotope ratios and associated propagated uncertainties
        for highly radiogenic phases, we only consider the measurement ratio uncertainty (std) 
        and the std of the session-wide fractionation correction factor for the uncertainty propagation

    Parameters
    ----------
    fractionation_corrected_ratios : pandas.DataFrame
        a pandas dataframe containing the fractionation corrected ratios
    session_correction_results : dict
        a dictionary containing the session-wide fractionation correction plots and fits, and std of the fractionation correction factor for each isotope ratio
    '''
    final_ratios = pd.DataFrame()
    for spot_name in fractionation_corrected_ratios['spot_name'].unique():
        this_spot_data = fractionation_corrected_ratios[fractionation_corrected_ratios['spot_name'] == spot_name]
        for isotope_ratio in session_correction_results.keys():
            # get the measured mean and std
            this_spot_measured_ratio_mean = this_spot_data[isotope_ratio].mean()
            # use the original measured ratio std for the uncertainty propagation
            this_spot_measured_ratio_se = this_spot_data[isotope_ratio].std() / np.sqrt(this_spot_data.shape[0]) # standard error of the mean
            if not np.isfinite(this_spot_measured_ratio_se):
                if isotope_ratio + '_2e' in this_spot_data.columns:
                    this_spot_measured_ratio_se = this_spot_data[isotope_ratio + '_2e'].mean() / 2
                else:
                    this_spot_measured_ratio_se = 0.0

            # get the session-wide fractionation correction mean and se at the mean spot time
            session_result = session_correction_results[isotope_ratio]
            this_spot_mean_time = this_spot_data['Time'].mean()
            this_spot_this_ratio_fractionation_mean = float(evaluate_correction_result(
                session_result, this_spot_mean_time
            ))
            this_ratio_fractionation_se = session_result['std']
            # later will add mean and std of the blank correction

            # get the fractionation corrected mean
            this_spot_fractionation_correction_mean = this_spot_data[isotope_ratio + '_fractionation_corrected'].mean()
            # calculate the propagated uncertainty
            this_spot_this_ratio_propagated_uncertainty = this_spot_fractionation_correction_mean * np.sqrt((this_spot_measured_ratio_se/this_spot_measured_ratio_mean)**2 + (this_ratio_fractionation_se/this_spot_this_ratio_fractionation_mean)**2)
            # print('contributions of error from fractionation correction:', this_ratio_fractionation_std/this_spot_this_ratio_fractionation_mean*100)
            # save results
            final_ratios.loc[spot_name, isotope_ratio] = this_spot_fractionation_correction_mean
            final_ratios.loc[spot_name, isotope_ratio + '_session_correction_1s'] = this_ratio_fractionation_se
            fit_times = np.asarray(
                session_result.get('fit_observations', {}).get('time', []),
                dtype=float,
            )
            final_ratios.loc[spot_name, isotope_ratio + '_session_correction_extrapolated'] = bool(
                fit_times.size
                and (
                    this_spot_mean_time < np.nanmin(fit_times)
                    or this_spot_mean_time > np.nanmax(fit_times)
                )
            )
            final_ratios.loc[spot_name, isotope_ratio + '_2e%'] = this_spot_this_ratio_propagated_uncertainty * 2 / this_spot_fractionation_correction_mean * 100
            final_ratios.loc[spot_name, isotope_ratio + '_2e'] = this_spot_this_ratio_propagated_uncertainty * 2 
        # calculate the 207Pb/235U ratio and error
        final_ratios.loc[spot_name, '207Pb/235U'] = final_ratios.loc[spot_name,'207Pb/206Pb'] * final_ratios.loc[spot_name,'206Pb/238U'] * common_U238_U235
        final_ratios.loc[spot_name, '207Pb/235U_2e%'] = np.sqrt((final_ratios.loc[spot_name,'207Pb/206Pb_2e%']**2 + final_ratios.loc[spot_name,'206Pb/238U_2e%']**2))
        final_ratios.loc[spot_name, '207Pb/235U_2e'] = final_ratios.loc[spot_name, '207Pb/235U'] * final_ratios.loc[spot_name, '207Pb/235U_2e%'] / 100
        
        # calculate the 238U/206Pb ratio and error where the 238U/206Pb ratio is the inverse of the 206Pb/238U ratio
        final_ratios.loc[spot_name, '238U/206Pb'] = 1 / final_ratios.loc[spot_name, '206Pb/238U']
        final_ratios.loc[spot_name, '238U/206Pb_2e'] = propagate_reciprocal_uncertainty(
            final_ratios.loc[spot_name, '206Pb/238U'],
            final_ratios.loc[spot_name, '206Pb/238U_2e'],
        )
        final_ratios.loc[spot_name, '238U/206Pb_2e%'] = (
            final_ratios.loc[spot_name, '238U/206Pb_2e']
            / final_ratios.loc[spot_name, '238U/206Pb']
            * 100
        )
        
        # calculate the 207Pb/235U age
        final_ratios.loc[spot_name, '207Pb/235U_age'] = calc_75_date(final_ratios.loc[spot_name, '207Pb/235U'])
        final_ratios.loc[spot_name, '207Pb/235U_age_2e'] = calc_75_date_uncertainty(
            final_ratios.loc[spot_name, '207Pb/235U'],
            final_ratios.loc[spot_name, '207Pb/235U_2e'],
        )
        final_ratios.loc[spot_name, '207Pb/235U_age_2e%'] = (
            final_ratios.loc[spot_name, '207Pb/235U_age_2e']
            / final_ratios.loc[spot_name, '207Pb/235U_age']
            * 100
        )

        # calculate the 206Pb/238U age
        final_ratios.loc[spot_name, '206Pb/238U_age'] = calc_68_date(final_ratios.loc[spot_name, '206Pb/238U'])
        final_ratios.loc[spot_name, '206Pb/238U_age_2e'] = calc_68_date_uncertainty(
            final_ratios.loc[spot_name, '206Pb/238U'],
            final_ratios.loc[spot_name, '206Pb/238U_2e'],
        )
        final_ratios.loc[spot_name, '206Pb/238U_age_2e%'] = (
            final_ratios.loc[spot_name, '206Pb/238U_age_2e']
            / final_ratios.loc[spot_name, '206Pb/238U_age']
            * 100
        )

        # calculate the 207Pb/206Pb age
        final_ratios.loc[spot_name, '207Pb/206Pb_age'] = calc_76_date(final_ratios.loc[spot_name, '207Pb/206Pb'])
        final_ratios.loc[spot_name, '207Pb/206Pb_age_2e'] = calc_76_date_uncertainty(
            final_ratios.loc[spot_name, '207Pb/206Pb'],
            final_ratios.loc[spot_name, '207Pb/206Pb_2e'],
        )
        final_ratios.loc[spot_name, '207Pb/206Pb_age_2e%'] = (
            final_ratios.loc[spot_name, '207Pb/206Pb_age_2e']
            / final_ratios.loc[spot_name, '207Pb/206Pb_age']
            * 100
        )

        # calculate discordance and uncertainty
        final_ratios.loc[spot_name, 'disc%'], final_ratios.loc[spot_name, 'disc_2e%'] = calc_68_76_discordance(final_ratios.loc[spot_name, '206Pb/238U_age'], final_ratios.loc[spot_name, '207Pb/206Pb_age'], final_ratios.loc[spot_name, '206Pb/238U_age_2e']/2, final_ratios.loc[spot_name, '207Pb/206Pb_age_2e']/2)


        # calculate error correlations for the 207Pb/235U-206Pb/238U ratios
        x = final_ratios.loc[spot_name,'207Pb/206Pb'] # 76 ratio
        u = final_ratios.loc[spot_name,'207Pb/235U']  # 75 ratio
        v = final_ratios.loc[spot_name,'206Pb/238U']  # 68 ratio

        sx = final_ratios.loc[spot_name,'207Pb/206Pb_2e']/2 # 76 std
        su = final_ratios.loc[spot_name,'207Pb/235U_2e']/2 # 75 std
        sv = final_ratios.loc[spot_name,'206Pb/238U_2e']/2 # 68 std

        this_75_68_err_corr = calc_err_corr_quotient(x, sx, u, su, v, sv)

        final_ratios.loc[spot_name, 'err_corr'] = this_75_68_err_corr


        final_ratios.loc[spot_name, 'sample'] = this_spot_data['sample'].unique()[0] # get the sample name from the first row of the spot data
    final_ratios['spot_name'] = final_ratios.index
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
