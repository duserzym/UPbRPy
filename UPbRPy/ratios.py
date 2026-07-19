"""Raw isotope-ratio calculations and Tera-Wasserburg projections."""

from __future__ import annotations

import numpy as np
import pandas as pd

def project_tw_238u_206pb(
    corrected_207pb_206pb,
    radiogenic_206pb_238u,
    radiogenic_207pb_206pb,
    common_207pb_206pb,
):
    """Horizontally project a Tera-Wasserburg point onto a known mixing line.

    Parameters
    ----------
    corrected_207pb_206pb : float or array-like
        Pb/Pb-fractionation-corrected total ``207Pb/206Pb`` value(s). This is
        the horizontal coordinate-preserving ordinate in Tera-Wasserburg space.
    radiogenic_206pb_238u : float
        Radiogenic endpoint ``206Pb/238U`` of the reference material.
    radiogenic_207pb_206pb : float
        Radiogenic endpoint ``207Pb/206Pb`` of the reference material.
    common_207pb_206pb : float
        Common-Pb endpoint ``207Pb/206Pb`` at ``238U/206Pb = 0``.

    Returns
    -------
    float or numpy.ndarray
        Projected Tera-Wasserburg x coordinate, ``238U/206Pb``.
    """
    if radiogenic_206pb_238u <= 0:
        raise ValueError("radiogenic_206pb_238u must be positive.")

    radiogenic_238u_206pb = 1 / radiogenic_206pb_238u
    slope = (common_207pb_206pb - radiogenic_207pb_206pb) / (
        0 - radiogenic_238u_206pb
    )
    if slope == 0:
        raise ValueError(
            "Tera-Wasserburg line has zero slope; radiogenic and common "
            "207Pb/206Pb endpoints must differ."
        )

    corrected_207pb_206pb = np.asarray(corrected_207pb_206pb, dtype=float)
    projected = (corrected_207pb_206pb - common_207pb_206pb) / slope
    if projected.ndim == 0:
        return float(projected)
    return projected

def project_tw_206pb_238u(
    corrected_207pb_206pb,
    radiogenic_206pb_238u,
    radiogenic_207pb_206pb,
    common_207pb_206pb,
):
    """Return the projected ``206Pb/238U`` target for TW fractionation."""
    projected_238u_206pb = project_tw_238u_206pb(
        corrected_207pb_206pb,
        radiogenic_206pb_238u,
        radiogenic_207pb_206pb,
        common_207pb_206pb,
    )
    projected_array = np.asarray(projected_238u_206pb, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        projected = 1 / projected_array
    if projected.ndim == 0:
        return float(projected)
    return projected

def propagate_raw_ratio_uncertaintiess(nom, denom):
    '''
    function for propagating the uncertainty of the raw ratios
        using Poisson distribution relative uncertainty where
        uncertainty of each individual analyte is sqrt(cps)/cps
        and the uncertainty of the ratio is given by the following formula:
        relative uncertainty of the ratio = sqrt(1/nom + 1/denom)

        return 2 standard errors of the ratio
    '''
    # calculate the relative uncertainty of the numerator and denominator
    eR = np.sqrt(1/nom + 1/denom)

    return 2 * eR # return 2 standard relative errors of the ratio, not in percentage format but in fraction format

def calc_raw_ratios(experiment, total_data, baseline_subtracted_total_data):
    '''
    function for calculating raw isotope ratios (before downhole variability and fractionation correction) 
        based on the initial unprocessed total data and the background-subtracted data
        the reason we need the initial total data is specifically and only because 
        we need the raw 204Pb and 202Hg cps from the baseline to help correct 
        for Hg isobaric inference for 206Pb/204Pb ratio calculations 
        note that we do not use the internally normalized data for this calculation,
        as the normalization is for total elemental concentration analysis

    by default we calculate the following ratios:
    206Pb/204Pb
    208Pb/232Th
    206Pb/238U
    207Pb/206Pb

    The 238U/206Pb ratio and the 207Pb/235U ratios are always calculated

    Parameters
    ----------
    experiment : pandas.DataFrame
        a pandas dataframe containing the experiment metadata
    total_data : pandas.DataFrame
        a pandas dataframe containing the total data
    baseline_subtracted_plateau_data : pandas.DataFrame
        a pandas dataframe containing the background-subtracted plateau data
    
    Returns
    -------
    raw_ratios : pandas.DataFrame
        a pandas dataframe containing the raw isotope ratios
    '''
    plateau_data_with_raw_ratios = pd.DataFrame()
    # raw_ratios = pd.DataFrame()

    for spot_name in baseline_subtracted_total_data['spot_name'].unique():
        # get the baseline indices for this spot
        this_spot_baseline_start = experiment[experiment['spot_names'] == spot_name]['baseline_start_times'].values[0]
        this_spot_baseline_end = experiment[experiment['spot_names'] == spot_name]['baseline_end_times'].values[0]

        # get the baseline subtracted plateau data for actual raw ratio calculations
        this_spot_plateau_start_time = experiment[experiment['spot_names'] == spot_name]['plateau_start_times'].values[0]
        this_spot_plateau_end_time = experiment[experiment['spot_names'] == spot_name]['plateau_end_times'].values[0]
        this_spot_plateau_data = baseline_subtracted_total_data[(baseline_subtracted_total_data['Time'] >= this_spot_plateau_start_time) & (baseline_subtracted_total_data['Time'] < this_spot_plateau_end_time)]

        # get the uncorrected total data for baseline use
        this_spot_total_data = total_data[total_data['spot_name'] == spot_name]
        this_spot_baseline_total_data = this_spot_total_data[(this_spot_total_data['Time'] >= this_spot_baseline_start) & (this_spot_total_data['Time'] < this_spot_baseline_end)]

        # 206/204 ratio of means
        this_spot_baseline_202Hg_mean = this_spot_baseline_total_data['202Hg'].mean() # baseline 202Hg mean
        this_spot_baseline_204Pb_mean = this_spot_baseline_total_data['204Hg'].mean() # baseline 204Pb mean

        # this_spot_plateau_206Pb_mean = this_spot_plateau_data['206Pb'].mean() # sample 206Pb mean
        # this_spot_plateau_204Pb_mean = this_spot_plateau_data['204Hg'].mean() # sample 204Pb mean
        # this_spot_plateau_202Hg_mean = this_spot_plateau_data['202Hg'].mean() # sample 202Hg mean

        # 206/204 mean of ratios, replace all negative or inf values to be 0
        this_spot_R64 = this_spot_plateau_data['206Pb']/(this_spot_plateau_data['204Hg'] - this_spot_plateau_data['202Hg'] * (this_spot_baseline_204Pb_mean/this_spot_baseline_202Hg_mean))
        this_spot_R64[this_spot_R64 < 0] = 0
        this_spot_R64[this_spot_R64 == np.inf] = 0
        
        # 208/232 mean of ratios
        this_spot_R82 = this_spot_plateau_data['208Pb'] / this_spot_plateau_data['232Th']
        this_spot_R82_2e = propagate_raw_ratio_uncertaintiess(this_spot_plateau_data['208Pb'], this_spot_plateau_data['232Th']) * this_spot_R82

        # 206/238 mean of ratios
        this_spot_R68 = this_spot_plateau_data['206Pb'] / this_spot_plateau_data['238U']
        this_spot_R68_2e = propagate_raw_ratio_uncertaintiess(this_spot_plateau_data['206Pb'], this_spot_plateau_data['238U']) * this_spot_R68

        # 207/206 mean of ratios
        this_spot_R76 = this_spot_plateau_data['207Pb'] / this_spot_plateau_data['206Pb']
        this_spot_R76_2e = propagate_raw_ratio_uncertaintiess(this_spot_plateau_data['207Pb'], this_spot_plateau_data['206Pb']) * this_spot_R76

        this_spot_plateau_data['206Pb/204Pb'] = this_spot_R64
        this_spot_plateau_data['206Pb/238U'] = this_spot_R68
        this_spot_plateau_data['206Pb/238U_2e'] = this_spot_R68_2e
        this_spot_plateau_data['207Pb/206Pb'] = this_spot_R76
        this_spot_plateau_data['207Pb/206Pb_2e'] = this_spot_R76_2e
        this_spot_plateau_data['208Pb/232Th'] = this_spot_R82
        this_spot_plateau_data['208Pb/232Th_2e'] = this_spot_R82_2e

        # update the plateau data with the raw ratios
        plateau_data_with_raw_ratios = pd.concat([plateau_data_with_raw_ratios, this_spot_plateau_data])
        
        # now strip away the cps columns and only return the ratio columns
        plateau_data_with_raw_ratios = plateau_data_with_raw_ratios[['sample', 'spot_name', 'Time', '206Pb/204Pb', '206Pb/238U', '207Pb/206Pb', '208Pb/232Th', '206Pb/238U_2e', '207Pb/206Pb_2e', '208Pb/232Th_2e']]
        
    return plateau_data_with_raw_ratios

__all__ = [
    'project_tw_238u_206pb',
    'project_tw_206pb_238u',
    'propagate_raw_ratio_uncertaintiess',
    'calc_raw_ratios',
]
