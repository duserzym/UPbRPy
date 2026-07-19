"""Downhole and session fractionation correction workflows."""

from __future__ import annotations

from bokeh.plotting import figure
import numpy as np
import pandas as pd
from bokeh.palettes import Category20
from scipy.interpolate import interp1d

from .ratios import project_tw_206pb_238u
from .uncertainty import (
    CovarianceStore,
    inverse_standard_deviation_weights,
    relative_time_by_spot,
    build_downhole_fractionation_metadata,
    evaluate_downhole_fractionation,
    evaluate_session_fractionation_fit,
    fit_weighted_polynomial_with_covariance,
    polynomial_prediction_covariance,
    propagate_downhole_corrected_ratio_covariance,
)
_DENSE_COVARIANCE_MAX_ROWS = 512

def calc_DH_nominal_fractionation(known_ratio, primary_standard_ratios, ratio_name, return_metadata=False):
    '''
    function for calculating nominal fractionation factor by comparing a given known ratio with the measured ratio
        use this function when the dealing with a single known ratio value

    '''
    primary_standard = primary_standard_ratios['sample'].unique()[0]
    # initialize a time variable and a ratio fractionation variable to store the interpolated time and fractionation for all primary standard spots
    all_times = [] # relative time within each spot, starts with zero
    all_ratios = [] # the fractionation of the measured ratio from the known standard ratio

    plot = figure(
        width=800, height=400,
        title=ratio_name + ' fractionation factor for primary standard ' + primary_standard,
        x_axis_label='Time (s)',
        y_axis_label=ratio_name + ' fractionation factor',
        tools='pan,wheel_zoom,box_zoom,reset,save', active_scroll='wheel_zoom',
    )
    for i, spot_name in enumerate(primary_standard_ratios['spot_name'].unique()):
        this_spot = primary_standard_ratios[primary_standard_ratios['spot_name'] == spot_name]
        # these are already plateau data so we just need to adjust the time so that every spot starts at 0
        this_spot['relative_time'] = this_spot['Time'] - this_spot['Time'].iloc[0]

        # store the measured time and ratios for this spot
        all_times.append(this_spot['relative_time'].values)
        all_ratios.append(this_spot[ratio_name].values)
        plot.line(
            this_spot['relative_time'].values,
            known_ratio / this_spot[ratio_name].values,
            color=Category20[20][i % 20], line_width=1, alpha=0.6,
        )

    # now interpolate the time and fractionation for all spots, unify the time axis, and calculate the mean and std * student's t multiplier
    min_time = 0
    max_time = np.max(np.array(all_times))
    unified_time = np.linspace(min_time, max_time, len(all_times[0])) # generate a unified time axis that has the same amount of points as the original time axis

    interpolated_ratios = []
    for time, ratio in zip(all_times, all_ratios):
        interpolated_ratio = np.interp(unified_time, time, ratio)
        interpolated_ratios.append(interpolated_ratio)
    
    # calculate the stacked mean sweep-wise ratio vs. relative time
    mean_ratio = np.mean(interpolated_ratios, axis=0)
    # calculate the fractionation factor as known ratio / mean_ratio
    mean_fractionation_factor = known_ratio / mean_ratio
    interpolated_fractionations = known_ratio / np.asarray(interpolated_ratios, dtype=float)
    downhole_metadata = build_downhole_fractionation_metadata(
        unified_time,
        interpolated_fractionations,
        mean_fractionation=mean_fractionation_factor,
    )
    # construct the downhole fractionation function
    downhole_fractionation = interp1d(unified_time, mean_fractionation_factor, kind='linear', fill_value='extrapolate')

    # calculate the std of the mean_fractionation_factor based on the difference between the mean and the individual ratios
    all_times_array = np.array(all_times).flatten()
    all_fractionations_array = known_ratio/np.array(all_ratios).flatten()
    # calculate the standard deviation of the mean_fractionation_factor
    std_fractionation_factor = np.std([all_fractionations_array[i] - downhole_fractionation(all_times_array[i]) for i in range(len(all_times_array))])

    plot.varea(
        unified_time, mean_fractionation_factor - std_fractionation_factor,
        mean_fractionation_factor + std_fractionation_factor,
        fill_color='grey', fill_alpha=0.12,
    )
    plot.line(unified_time, mean_fractionation_factor, color='black', line_width=2, line_dash='dashed', legend_label='mean fractionation factor')
    plot.line(unified_time, mean_fractionation_factor + std_fractionation_factor, color='grey', line_dash='dashed', line_width=1, legend_label='1 sigma')
    plot.line(unified_time, mean_fractionation_factor - std_fractionation_factor, color='grey', line_dash='dashed', line_width=1)
    plot.legend.location = 'top_right'

    if return_metadata:
        return plot, downhole_fractionation, downhole_metadata
    return plot, downhole_fractionation

def calc_DH_fractionations_rad(primary_standard_ratios, 
                                standard_archive, 
                                ratio_names = ['206Pb/238U', '207Pb/206Pb', '208Pb/232Th'], 
                                fit='linear'):
    '''
    function for stacking the downhole primary standard isotope ratios through the session,
        interpolating the ratio as a function of relative time within each spot analysis
        calculating the fractionation of the mean of the primary standard downhole ratios against the known radiogenic ratios
        - use this function if your primary standard is highly radiogenic and homogenous
        - we do the fractionation calculation as sweep-wise knonwnratio/measured ratio 
        - we allow user to choose the fit function as a means of smoothing
        - note that because we do the downhole fractionation correction on the mean of the primary standard,
            there will be residual scatter in the spot mean ratios as compared to the known standard ratios
            and this bias is corrected in the next step of session-wide correction

    Parameters
    ----------
    primary_standard_ratios : pd.DataFrame
        a pandas dataframe containing the primary standard ratios
    ratio_name : str
        the name of the ratio to be plotted
    standard_archive : pd.DataFrame
        a pandas dataframe containing the standard archive
    fit : str
        the type of fit to be used for the interpolation
        here for now we use a linear inerpolation
        in the future we can implement polynomial or spline or exponential fits
    '''

    # first get the known standard ratios from the standard archive
    primary_standard = primary_standard_ratios['sample'].unique()[0]
    # store the plots and interpolations in a dictionary
    DH_correction_results = dict()

    for ratio_name in ratio_names:
        known_PR_ratio = standard_archive.loc[primary_standard, ratio_name+'*'] # for highly radiogenic primary standards
        plot, downhole_fractionation, downhole_metadata = calc_DH_nominal_fractionation(
            known_PR_ratio,
            primary_standard_ratios,
            ratio_name,
            return_metadata=True,
        )
        # append the results to the dictionary
        DH_correction_results.update({ratio_name: {
            'plot': plot,
            'interpolation': downhole_fractionation,
            'metadata': downhole_metadata,
            'fit_covariance': downhole_metadata['covariance'],
            'fit_prediction_x': downhole_metadata['time'],
            'fit_prediction_1sigma': np.sqrt(np.clip(np.diag(downhole_metadata['covariance']), 0, None)),
            'fit_residuals': downhole_metadata['residuals'],
            'fit_degrees_of_freedom': downhole_metadata['degrees_of_freedom'],
        }})
    return DH_correction_results

def calc_DH_fractionations_TW(primary_68_standard_ratios, 
                                primary_76_standard_ratios, 
                                primary_82_standard_ratios, 
                                standard_archive, fit='linear'):
    '''
    function for stacking the downhole primary standard isotope ratios through the session,
        interpolating the ratio as a function of relative time within each spot analysis
        calculating the fractionation of the mean of the TW lower intercept primary standard downhole ratios 
        against the known radiogenic ratios
        - use this function if your primary standard is heterogeneous
        - we do the fractionation calculation as sweep-wise knonwnratio/TW intercept ratio 
        - we allow user to choose the fit function as a means of smoothing
        - note that because we do the downhole fractionation correction on the mean of the primary standard,
            there will be residual scatter in the spot mean ratios as compared to the known standard ratios
            and this bias is corrected in the next step of session-wide correction

    Parameters
    ----------
    primary_standard_ratios : pd.DataFrame
        a pandas dataframe containing the primary standard ratios
    ratio_name : str
        the name of the ratio to be plotted
    standard_archive : pd.DataFrame
        a pandas dataframe containing the standard archive
    fit : str
        the type of fit to be used for the interpolation
        here for now we use a linear inerpolation
        in the future we can implement polynomial or spline or exponential fits
    '''

    # first get the known standard ratios from the standard archive
    primary_68_standard = primary_68_standard_ratios['sample'].unique()[0]
    primary_76_standard = primary_76_standard_ratios['sample'].unique()[0]
    primary_82_standard = primary_82_standard_ratios['sample'].unique()[0]

    # store the plots and interpolations in a dictionary
    DH_correction_results = dict()

    # let's first get the fractionation factor for the 7/6 ratio
    known_PR_76_ratio = standard_archive.loc[primary_76_standard, '207Pb/206Pb*'] # for the radiogenic 76 ratio (usually use NIST glass standard)
    plot_76, downhole_fractionation_76, downhole_metadata_76 = calc_DH_nominal_fractionation(
        known_PR_76_ratio,
        primary_76_standard_ratios,
        '207Pb/206Pb',
        return_metadata=True,
    )
    DH_correction_results.update({'207Pb/206Pb': {
        'plot': plot_76,
        'interpolation': downhole_fractionation_76,
        'metadata': downhole_metadata_76,
        'fit_covariance': downhole_metadata_76['covariance'],
        'fit_prediction_x': downhole_metadata_76['time'],
        'fit_prediction_1sigma': np.sqrt(np.clip(np.diag(downhole_metadata_76['covariance']), 0, None)),
        'fit_residuals': downhole_metadata_76['residuals'],
        'fit_degrees_of_freedom': downhole_metadata_76['degrees_of_freedom'],
    }})

    # then use this fractionation factor to correct the 7/6 ratio of the primary 68 standard
    primary_68_standard_ratios = apply_DH_correction(primary_68_standard_ratios, DH_correction_results)

    # horizontally project the corrected 7/6 ratio to get the TW intercept point
    PR_68_rad = standard_archive.loc[primary_68_standard, '206Pb/238U*'] # for the radiogenic 68 ratio
    PR_76_i = standard_archive.loc[primary_68_standard, '207Pb/206Pb_i'] # for the initial 76 ratio
    PR_76_rad = standard_archive.loc[primary_68_standard, '207Pb/206Pb*'] # for the radiogenic 76 ratio

    # calculate the TW projections
    primary_68_standard_ratios['206Pb/238U_TW_projected'] = project_tw_206pb_238u(
        primary_68_standard_ratios['207Pb/206Pb_DH_corrected'],
        PR_68_rad,
        PR_76_rad,
        PR_76_i,
    )
    # now calculate the fractionation factor for the 6/8 ratio
    plot_68, downhole_fractionation_68, downhole_metadata_68 = calc_DH_dynamic_fractionation(
        primary_68_standard_ratios,
        '206Pb/238U',
        '206Pb/238U_TW_projected',
        return_metadata=True,
    )
    DH_correction_results.update({'206Pb/238U': {
        'plot': plot_68,
        'interpolation': downhole_fractionation_68,
        'metadata': downhole_metadata_68,
        'fit_covariance': downhole_metadata_68['covariance'],
        'fit_prediction_x': downhole_metadata_68['time'],
        'fit_prediction_1sigma': np.sqrt(np.clip(np.diag(downhole_metadata_68['covariance']), 0, None)),
        'fit_residuals': downhole_metadata_68['residuals'],
        'fit_degrees_of_freedom': downhole_metadata_68['degrees_of_freedom'],
    }})

    # add the 8/2 ratio correction
    known_PR_82_ratio = standard_archive.loc[primary_82_standard, '208Pb/232Th*'] # for the radiogenic 82 ratio
    plot_82, downhole_fractionation_82, downhole_metadata_82 = calc_DH_nominal_fractionation(
        known_PR_82_ratio,
        primary_82_standard_ratios,
        '208Pb/232Th',
        return_metadata=True,
    )
    DH_correction_results.update({'208Pb/232Th': {
        'plot': plot_82,
        'interpolation': downhole_fractionation_82,
        'metadata': downhole_metadata_82,
        'fit_covariance': downhole_metadata_82['covariance'],
        'fit_prediction_x': downhole_metadata_82['time'],
        'fit_prediction_1sigma': np.sqrt(np.clip(np.diag(downhole_metadata_82['covariance']), 0, None)),
        'fit_residuals': downhole_metadata_82['residuals'],
        'fit_degrees_of_freedom': downhole_metadata_82['degrees_of_freedom'],
    }})
    return DH_correction_results

def calc_DH_dynamic_fractionation(primary_standard_ratios, measured_ratio_name, projected_ratio_name, return_metadata=False):
    '''
    function for calculating dynamic fractionation, this is used for cases where we need to do TW projection to get
        the TW 86 ratios for each sweep and then calculate the projected ratios against the measured ratios
    '''
    primary_standard = primary_standard_ratios['sample'].unique()[0]
    # initialize a time variable and a ratio fractionation variable to store the interpolated time and fractionation for all primary standard spots
    all_times = [] # relative time within each spot, starts with zero
    all_fractionations = [] # the fractionation of the measured ratio from the known standard ratio

    plot = figure(
        width=800, height=400,
        title=measured_ratio_name + ' fractionation factor for primary standard ' + primary_standard,
        x_axis_label='Time (s)',
        y_axis_label=measured_ratio_name + ' fractionation factor',
        tools='pan,wheel_zoom,box_zoom,reset,save', active_scroll='wheel_zoom',
    )
    for i, spot_name in enumerate(primary_standard_ratios['spot_name'].unique()):
        this_spot = primary_standard_ratios[primary_standard_ratios['spot_name'] == spot_name]
        # these are already plateau data so we just need to adjust the time so that every spot starts at 0
        this_spot['relative_time'] = this_spot['Time'] - this_spot['Time'].iloc[0]
        this_spot[measured_ratio_name +'_fractionation_factor'] = this_spot[projected_ratio_name] / this_spot[measured_ratio_name]
        # store the measured time and fractionation factors for this spot
        all_times.append(this_spot['relative_time'].values)
        all_fractionations.append(this_spot[measured_ratio_name +'_fractionation_factor'].values)
        plot.line(
            this_spot['relative_time'].values,
            this_spot[measured_ratio_name + '_fractionation_factor'].values,
            color=Category20[20][i % 20], line_width=1, alpha=0.6,
        )

    # now interpolate the time and fractionation for all spots, unify the time axis, and calculate the mean and std * student's t multiplier
    min_time = 0
    max_time = np.max(np.array(all_times))
    unified_time = np.linspace(min_time, max_time, len(all_times[0])) # generate a unified time axis that has the same amount of points as the original time axis

    interpolated_fractionations = []
    for time, fractionation in zip(all_times, all_fractionations):
        interpolated_fractionation = np.interp(unified_time, time, fractionation)
        interpolated_fractionations.append(interpolated_fractionation)
    
    # calculate the stacked mean sweep-wise fractionation vs. relative time
    mean_fractionation_factor = np.mean(interpolated_fractionations, axis=0)
    downhole_metadata = build_downhole_fractionation_metadata(
        unified_time,
        interpolated_fractionations,
    )
    # calculate the std of the mean_fractionation_factor based on the difference between the mean and the individual ratios
    all_times_array = np.array(all_times).flatten()
    all_fractionations_array = np.array(all_fractionations).flatten()
    downhole_fractionation = interp1d(unified_time, mean_fractionation_factor, kind='linear', fill_value='extrapolate')
    # calculate the standard deviation of the mean_fractionation_factor
    std_fractionation_factor = np.std([all_fractionations_array[i] - downhole_fractionation(all_times_array[i]) for i in range(len(all_times_array))])

    plot.varea(
        unified_time, mean_fractionation_factor - std_fractionation_factor,
        mean_fractionation_factor + std_fractionation_factor,
        fill_color='grey', fill_alpha=0.12,
    )
    plot.line(unified_time, mean_fractionation_factor, color='black', line_width=2, line_dash='dashed', legend_label='mean fractionation factor')
    plot.line(unified_time, mean_fractionation_factor + std_fractionation_factor, color='grey', line_dash='dashed', line_width=1, legend_label='1 sigma')
    plot.line(unified_time, mean_fractionation_factor - std_fractionation_factor, color='grey', line_dash='dashed', line_width=1)
    plot.legend.location = 'top_right'

    if return_metadata:
        return plot, downhole_fractionation, downhole_metadata
    return plot, downhole_fractionation

def apply_DH_correction(
    plateau_data_with_raw_ratios,
    DH_correction_results,
    *,
    covariance_mode='auto',
):
    '''
    function for applying a DH correction scheme derived from the primary standard to the entire raw ratios plateau data
        note that this function only serves as a first step in the two-step fractionation correction
        which effectively should just 'flatten' the ratios as a function of time
        this is not the final step where we actually correct for fractionation 
        
    Parameters
    ----------
    plateau_data_with_raw_ratios : pandas.DataFrame
        a pandas dataframe containing the raw ratios for the plateau data
    DH_correction_results : dict
        a dictionary containing the DH correction plots and interpolations for each isotope ratio
    covariance_mode : {'auto', 'full', 'deferred'}, optional
        ``'auto'`` retains dense covariance only for tables up to 512 rows.
        ``'full'`` retains the full sweep-by-sweep covariance matrix, which is
        useful for small targeted analyses but scales quadratically in memory.
        ``'deferred'`` retains lightweight provenance and per-row uncertainty
        columns without materializing those dense matrices.
    
    Returns
    -------
    corrected_plateau_data : pandas.DataFrame
        a pandas dataframe containing the DH corrected raw ratios
    '''
    corrected_plateau_data = pd.DataFrame()
    for spot_name in plateau_data_with_raw_ratios['spot_name'].unique():
        this_spot_data = plateau_data_with_raw_ratios[plateau_data_with_raw_ratios['spot_name'] == spot_name].copy()
        for isotope_ratio in DH_correction_results.keys():
            this_spot_DH_correction = DH_correction_results[isotope_ratio]['interpolation']
            relative_time = this_spot_data['Time'] - this_spot_data['Time'].iloc[0]
            if 'metadata' in DH_correction_results[isotope_ratio]:
                fit_evaluation = evaluate_downhole_fractionation(
                    DH_correction_results[isotope_ratio]['metadata'],
                    relative_time,
                )
                correction_factor = fit_evaluation['prediction']
                this_spot_data[isotope_ratio + '_DH_correction_1s'] = fit_evaluation['prediction_1sigma']
                this_spot_data[isotope_ratio + '_DH_correction_extrapolated'] = fit_evaluation['is_extrapolated']
            else:
                correction_factor = this_spot_DH_correction(relative_time)
            # All UPbRPy fractionation factors use the convention
            # C = R_target / R_measured, so R_corrected = C * R_measured.
            this_spot_data[isotope_ratio + '_DH_correction_factor'] = correction_factor
            this_spot_data[isotope_ratio + '_DH_corrected'] = (
                this_spot_data[isotope_ratio] * correction_factor
            )

        corrected_plateau_data = pd.concat([corrected_plateau_data, this_spot_data])

    if covariance_mode not in {'auto', 'full', 'deferred'}:
        raise ValueError("covariance_mode must be 'auto', 'full', or 'deferred'.")

    retain_dense_covariance = (
        covariance_mode == 'full'
        or (
            covariance_mode == 'auto'
            and len(corrected_plateau_data) <= _DENSE_COVARIANCE_MAX_ROWS
        )
    )
    corrected_plateau_data.attrs["downhole_corrected_covariance"] = CovarianceStore()
    relative_time = relative_time_by_spot(corrected_plateau_data)
    for isotope_ratio, downhole_result in DH_correction_results.items():
        if isotope_ratio not in corrected_plateau_data.columns:
            continue
        metadata = {
            "row_index": corrected_plateau_data.index.to_numpy(),
            "spot_name": corrected_plateau_data['spot_name'].to_numpy(),
            "time": corrected_plateau_data['Time'].to_numpy(dtype=float),
            "relative_time": relative_time,
            "covariance_mode": 'full' if retain_dense_covariance else 'deferred',
        }
        if not retain_dense_covariance:
            corrected_plateau_data.attrs["downhole_corrected_covariance"][isotope_ratio] = metadata
            continue
        raw_ratio_1sigma = None
        if isotope_ratio + '_2e' in corrected_plateau_data.columns:
            raw_ratio_1sigma = corrected_plateau_data[isotope_ratio + '_2e'].to_numpy(dtype=float) / 2
        covariance_result = propagate_downhole_corrected_ratio_covariance(
            corrected_plateau_data[isotope_ratio].to_numpy(dtype=float),
            relative_time,
            downhole_result,
            raw_ratio_1sigma=raw_ratio_1sigma,
        )
        corrected_plateau_data.attrs["downhole_corrected_covariance"][isotope_ratio] = {
            **metadata,
            **covariance_result,
        }

    return corrected_plateau_data

def calc_session_nominal_fractionation(known_ratio, primary_standard_ratios, ratio_name, degree=2):
    '''
    function for calculating nominal fractionation factor through a given session by comparing a given known ratio with the measured ratio
        use this function when the dealing with a single known ratio value
        we use this function for radiogenic ratios
    '''
    primary_standard = primary_standard_ratios['sample'].unique()[0]
    spot_mean_times = [] # spot mean time
    spot_mean_fractionation = [] # spot mean fractionation
    spot_mean_fractionation_std = [] # spot mean fractionation std
    # now let's plot the measured sweep-wise fractionation for the primary standard
    for spot_name in primary_standard_ratios['spot_name'].unique():
        this_spot = primary_standard_ratios[primary_standard_ratios['spot_name'] == spot_name]
        # calculate mean time for the spot
        this_spot_mean_time = this_spot['Time'].mean()
        # this_spot_mean_ratio = this_spot[ratio_name+'_DH_corrected'].mean()
        # this_spot_mean_ratio_std = this_spot[ratio_name+'_DH_corrected'].std()
        this_spot_mean_fractionations = known_ratio / this_spot[ratio_name+'_DH_corrected']
        this_spot_mean_fractionation = np.mean(this_spot_mean_fractionations)
        this_spot_mean_fractionation_std = np.std(this_spot_mean_fractionations)
        # this_spot_mean_fractionation_std = known_ratio / this_spot_mean_ratio_std

        # store the calculated mean time and fractionation factors
        spot_mean_times.append(this_spot_mean_time)
        spot_mean_fractionation.append(this_spot_mean_fractionation)
        spot_mean_fractionation_std.append(this_spot_mean_fractionation_std)

    # calculate a polynomial fit for the mean DH corrected ratios against time
    spot_mean_times = np.array(spot_mean_times)
    spot_mean_fractionation = np.array(spot_mean_fractionation)
    spot_mean_fractionation_std = np.array(spot_mean_fractionation_std)
    spot_mean_fractionation_weights = inverse_standard_deviation_weights(
        spot_mean_fractionation_std
    )
    fit_result = fit_weighted_polynomial_with_covariance(
        spot_mean_times,
        spot_mean_fractionation,
        spot_mean_fractionation_std,
        degree,
    )
    # fit a polynomial of user-specified to the mean DH corrected ratios, default is 2, default is weighted by the standard deviation of each spot fractionation
    p = fit_result["coefficients"]
    p_std = fit_result["residual_standard_deviation"]
    
    # calculate a fitted polynomial curve
    fit_x = np.linspace(spot_mean_times[0], spot_mean_times[-1], 100)
    polynomial_values = np.polyval(p, fit_x)
    prediction_covariance = polynomial_prediction_covariance(
        fit_result["covariance"],
        fit_x,
    )
    prediction_1sigma = np.sqrt(np.clip(np.diag(prediction_covariance), 0, None))

    plot = figure(
        width=800, height=400,
        title=ratio_name + ' fractionation correction factor for primary standard ' + primary_standard,
        x_axis_label='Time (s)', y_axis_label=ratio_name + ' fractionation factor',
        tools='pan,wheel_zoom,box_zoom,reset,save', active_scroll='wheel_zoom',
    )
    plot.scatter(spot_mean_times, spot_mean_fractionation, color='black', size=6, legend_label='mean fractionation factor')
    plot.line(fit_x, polynomial_values, color='red', line_width=2, legend_label='polynomial fit of degree {}'.format(degree))
    plot.varea(fit_x, polynomial_values - p_std, polynomial_values + p_std, fill_color='grey', fill_alpha=0.12)
    plot.legend.location = 'top_right'
    
    fit_metadata = {
        "covariance": fit_result["covariance"],
        "covariance_unscaled": fit_result["covariance_unscaled"],
        "covariance_overdispersion_scaled": fit_result["covariance_overdispersion_scaled"],
        "prediction_x": fit_x,
        "prediction_covariance": prediction_covariance,
        "prediction_1sigma": prediction_1sigma,
        "residuals": fit_result["residuals"],
        "reduced_chi_square": fit_result["reduced_chi_square"],
        "degrees_of_freedom": fit_result["degrees_of_freedom"],
        "spot_mean_times": spot_mean_times,
        "spot_mean_fractionation": spot_mean_fractionation,
        "spot_mean_fractionation_std": spot_mean_fractionation_std,
        "weights": spot_mean_fractionation_weights,
    }

    return plot, p, p_std, fit_metadata

def calc_session_fractionation_rad(primary_standard_DH_corrected_ratios, 
                                    standard_archive, 
                                    ratio_names=['206Pb/238U', '207Pb/206Pb', '208Pb/232Th'], 
                                    degree=2):
    '''
    function for plotting and calculating the session-wide fractionation correction, 
        after applying a given downhole correction based on the primary standards
        we use the mean of the DH corrected ratios for the session-wide fractionation correction
        we also assume that we always work with the radiogenic ratios for the fractionation correction
        we still only work with 6/8 and 7/6 ratios for this correction
    
    Parameters
    ----------
    primary_standard_DH_corrected_ratios : pandas.DataFrame
        a pandas dataframe containing the primary standard ratios after downhole variation correction
    standard_archive : pandas.DataFrame
        a pandas dataframe containing the true standard ratios for the primary standard
    ratio_name : str
        the name of the ratio to be corrected
    degree : int
        the degree of the polynomial fit to be used for the session-wide fractionation correction

    Returns
    -------
    plot : bokeh.plotting.figure
        A native Bokeh plot containing primary-standard observations and the polynomial fit.
    p : numpy.ndarray
        the coefficients of the polynomial fit
    '''
    primary_standard = primary_standard_DH_corrected_ratios['sample'].unique()[0]
    session_correction_results = dict()

    for ratio_name in ratio_names:
        known_PR_ratio = standard_archive.loc[primary_standard, ratio_name+'*'] # at this stage we only compare radiogenic vs. radiogenic
        plot, p, p_std, fit_metadata = calc_session_nominal_fractionation(known_PR_ratio, primary_standard_DH_corrected_ratios, ratio_name, degree)
        # append the results to the dictionary
        session_correction_results.update({ratio_name: {
            'plot': plot,
            'fit': p,
            'std': p_std,
            'fit_covariance': fit_metadata['covariance'],
            'fit_covariance_unscaled': fit_metadata['covariance_unscaled'],
            'fit_covariance_overdispersion_scaled': fit_metadata['covariance_overdispersion_scaled'],
            'fit_prediction_x': fit_metadata['prediction_x'],
            'fit_prediction_covariance': fit_metadata['prediction_covariance'],
            'fit_prediction_1sigma': fit_metadata['prediction_1sigma'],
            'fit_residuals': fit_metadata['residuals'],
            'fit_reduced_chi_square': fit_metadata['reduced_chi_square'],
            'fit_degrees_of_freedom': fit_metadata['degrees_of_freedom'],
            'fit_observations': {
                'time': fit_metadata['spot_mean_times'],
                'fractionation': fit_metadata['spot_mean_fractionation'],
                'std': fit_metadata['spot_mean_fractionation_std'],
                'weights': fit_metadata['weights'],
            },
        }})

    return session_correction_results

def calc_session_fractionation_TW(primary_68_DH_ratios, primary_76_DH_ratios, primary_82_DH_ratios, standard_archive, degree=2):
    '''
    function for plotting and calculating the session-wide fractionation correction, 
        after applying a given downhole correction scheme to the primary standard ratios
        we use the mean of the DH corrected ratios for the session-wide fractionation correction
        for the TW variant of the session correction, we still cannot use the true radiogenic 68 ratio of the known standard
        we still need to apply a correction for the 7/6 ratio on the spot mean, and then calculate the corresponding projected TW line 86 ratio
        and then calculate the fractionation factor for the 6/8 ratio
    
    Parameters
    ----------
    primary_standard_DH_corrected_ratios : pandas.DataFrame
        a pandas dataframe containing the primary standard ratios after downhole variation correction
    standard_archive : pandas.DataFrame
        a pandas dataframe containing the true standard ratios for the primary standard
    ratio_name : str
        the name of the ratio to be corrected
    degree : int
        the degree of the polynomial fit to be used for the session-wide fractionation correction

    Returns
    -------
    plot : bokeh.plotting.figure
        A native Bokeh plot containing primary-standard observations and the polynomial fit.
    p : numpy.ndarray
        the coefficients of the polynomial fit
    '''
    
    # first get the known standard ratios from the standard archive
    primary_68_standard = primary_68_DH_ratios['sample'].unique()[0]
    primary_76_standard = primary_76_DH_ratios['sample'].unique()[0]
    primary_82_standard = primary_82_DH_ratios['sample'].unique()[0]
    # store the plots and interpolations in a dictionary
    session_correction_results = dict()

    # let's first get the fractionation factor for the 7/6 ratio
    known_PR_76_ratio = standard_archive.loc[primary_76_standard, '207Pb/206Pb*'] # for the radiogenic 76 ratio (usually use NIST glass standard)
    plot_76, p_76, p_std_76, fit_metadata_76 = calc_session_nominal_fractionation(known_PR_76_ratio, primary_76_DH_ratios, '207Pb/206Pb', degree)

    session_correction_results.update({'207Pb/206Pb': {
        'plot': plot_76,
        'fit': p_76,
        'std': p_std_76,
        'fit_covariance': fit_metadata_76['covariance'],
        'fit_covariance_unscaled': fit_metadata_76['covariance_unscaled'],
        'fit_covariance_overdispersion_scaled': fit_metadata_76['covariance_overdispersion_scaled'],
        'fit_prediction_x': fit_metadata_76['prediction_x'],
        'fit_prediction_covariance': fit_metadata_76['prediction_covariance'],
        'fit_prediction_1sigma': fit_metadata_76['prediction_1sigma'],
        'fit_residuals': fit_metadata_76['residuals'],
        'fit_reduced_chi_square': fit_metadata_76['reduced_chi_square'],
        'fit_degrees_of_freedom': fit_metadata_76['degrees_of_freedom'],
        'fit_observations': {
            'time': fit_metadata_76['spot_mean_times'],
            'fractionation': fit_metadata_76['spot_mean_fractionation'],
            'std': fit_metadata_76['spot_mean_fractionation_std'],
            'weights': fit_metadata_76['weights'],
        },
    }})

    # then use this fractionation factor to correct the 7/6 ratio of the primary 68 standard through the session
    primary_68_DH_ratios['207Pb/206Pb_session_corrected'] = primary_68_DH_ratios['207Pb/206Pb_DH_corrected'] * np.polyval(p_76, primary_68_DH_ratios['Time'])
    
    # horizontally project the corrected 7/6 ratio to get the TW intercept point
    PR_68_rad = standard_archive.loc[primary_68_standard, '206Pb/238U*'] # for the radiogenic 68 ratio
    PR_76_i = standard_archive.loc[primary_68_standard, '207Pb/206Pb_i'] # for the initial 76 ratio
    PR_76_rad = standard_archive.loc[primary_68_standard, '207Pb/206Pb*'] # for the radiogenic 76 ratio

    spot_mean_times = [] # spot mean time
    spot_mean_68_fractionation = [] # spot mean fractionation
    spot_mean_68_fractionation_std = [] # spot mean fractionation std
    for spot_name in primary_68_DH_ratios['spot_name'].unique():
        this_spot = primary_68_DH_ratios[primary_68_DH_ratios['spot_name'] == spot_name]
        # calculate mean time for the spot
        this_spot_mean_time = this_spot['Time'].mean()
        this_spot_68_ratio = this_spot['206Pb/238U_DH_corrected']
        this_spot_mean_68_ratio = this_spot['206Pb/238U_DH_corrected'].mean()
        this_spot_68_TW_projected = project_tw_206pb_238u(
            this_spot['207Pb/206Pb_session_corrected'],
            PR_68_rad,
            PR_76_rad,
            PR_76_i,
        )
        this_spot_68_TW_mean_projected = project_tw_206pb_238u(
            this_spot['207Pb/206Pb_session_corrected'].mean(),
            PR_68_rad,
            PR_76_rad,
            PR_76_i,
        )
        this_spot_68_fractionation_std = np.std(this_spot_68_TW_projected/this_spot_68_ratio)
        this_spot_mean_68_fractionation = this_spot_68_TW_mean_projected/this_spot_mean_68_ratio
        # store the calculated mean time and fractionation factors
        spot_mean_times.append(this_spot_mean_time)
        spot_mean_68_fractionation.append(this_spot_mean_68_fractionation)
        spot_mean_68_fractionation_std.append(this_spot_68_fractionation_std)
    # calculate a polynomial fit for the mean DH corrected ratios against time
    spot_mean_times = np.array(spot_mean_times)
    spot_mean_68_fractionation = np.array(spot_mean_68_fractionation)
    spot_mean_68_fractionation_std = np.array(spot_mean_68_fractionation_std)
    spot_mean_68_fractionation_weights = inverse_standard_deviation_weights(
        spot_mean_68_fractionation_std
    )
    fit_result_68 = fit_weighted_polynomial_with_covariance(
        spot_mean_times,
        spot_mean_68_fractionation,
        spot_mean_68_fractionation_std,
        degree,
    )
    # fit a polynomial of user-specified to the mean DH corrected ratios, default is 2
    p_68 = fit_result_68["coefficients"]
    # calculate the std of the residuals
    p_std_68 = fit_result_68["residual_standard_deviation"]
    # calculate a fitted polynomial curve
    fit_x_68 = np.linspace(spot_mean_times[0], spot_mean_times[-1], 100)
    polynomial_values_68 = np.polyval(p_68, fit_x_68)
    prediction_covariance_68 = polynomial_prediction_covariance(
        fit_result_68["covariance"],
        fit_x_68,
    )
    prediction_1sigma_68 = np.sqrt(np.clip(np.diag(prediction_covariance_68), 0, None))
    plot_68 = figure(
        width=800, height=400,
        title='206Pb/238U fractionation correction factor for primary standard ' + primary_68_standard,
        x_axis_label='Time (s)', y_axis_label='206Pb/238U fractionation factor',
        tools='pan,wheel_zoom,box_zoom,reset,save', active_scroll='wheel_zoom',
    )
    plot_68.scatter(spot_mean_times, spot_mean_68_fractionation, color='black', size=6, legend_label='mean fractionation factor')
    plot_68.line(fit_x_68, polynomial_values_68, color='red', line_width=2, legend_label='polynomial fit of degree {}'.format(degree))
    plot_68.varea(fit_x_68, polynomial_values_68 - p_std_68, polynomial_values_68 + p_std_68, fill_color='grey', fill_alpha=0.12)
    plot_68.legend.location = 'top_right'
    
    session_correction_results.update({'206Pb/238U': {
        'plot': plot_68,
        'fit': p_68,
        'std': p_std_68,
        'fit_covariance': fit_result_68['covariance'],
        'fit_covariance_unscaled': fit_result_68['covariance_unscaled'],
        'fit_covariance_overdispersion_scaled': fit_result_68['covariance_overdispersion_scaled'],
        'fit_prediction_x': fit_x_68,
        'fit_prediction_covariance': prediction_covariance_68,
        'fit_prediction_1sigma': prediction_1sigma_68,
        'fit_residuals': fit_result_68['residuals'],
        'fit_reduced_chi_square': fit_result_68['reduced_chi_square'],
        'fit_degrees_of_freedom': fit_result_68['degrees_of_freedom'],
        'fit_observations': {
            'time': spot_mean_times,
            'fractionation': spot_mean_68_fractionation,
            'std': spot_mean_68_fractionation_std,
            'weights': spot_mean_68_fractionation_weights,
        },
    }})

    # add the 8/2 ratio correction
    known_PR_82_ratio = standard_archive.loc[primary_82_standard, '208Pb/232Th*'] # for the radiogenic 82 ratio
    plot_82, p_82, p_std_82, fit_metadata_82 = calc_session_nominal_fractionation(known_PR_82_ratio, primary_82_DH_ratios, '208Pb/232Th', degree)
    session_correction_results.update({'208Pb/232Th': {
        'plot': plot_82,
        'fit': p_82,
        'std': p_std_82,
        'fit_covariance': fit_metadata_82['covariance'],
        'fit_covariance_unscaled': fit_metadata_82['covariance_unscaled'],
        'fit_covariance_overdispersion_scaled': fit_metadata_82['covariance_overdispersion_scaled'],
        'fit_prediction_x': fit_metadata_82['prediction_x'],
        'fit_prediction_covariance': fit_metadata_82['prediction_covariance'],
        'fit_prediction_1sigma': fit_metadata_82['prediction_1sigma'],
        'fit_residuals': fit_metadata_82['residuals'],
        'fit_reduced_chi_square': fit_metadata_82['reduced_chi_square'],
        'fit_degrees_of_freedom': fit_metadata_82['degrees_of_freedom'],
        'fit_observations': {
            'time': fit_metadata_82['spot_mean_times'],
            'fractionation': fit_metadata_82['spot_mean_fractionation'],
            'std': fit_metadata_82['spot_mean_fractionation_std'],
            'weights': fit_metadata_82['weights'],
        },
    }})

    return session_correction_results

def apply_session_correction(DH_corrected_ratios, session_correction_results):
    '''
    function for applying a session-wide fractionation correction scheme derived from the primary standard 
        to the entire downhole corrected ratios data
        even if the session fractionation correction results are constructed based on TW-intercept ratios or total ratios
        that they constrain isotope ratio fractionation means they can be applied any measured total ratios for unknowns

    Parameters
    ----------
    DH_corrected_ratios : pandas.DataFrame
        a pandas dataframe containing the downhole corrected ratios
    session_correction_results : dict
        a dictionary containing the session-wide fractionation correction plots and fits for each isotope ratio
    '''
    fractionation_corrected_ratios = pd.DataFrame()
    for spot_name in DH_corrected_ratios['spot_name'].unique():
        this_spot_data = DH_corrected_ratios[DH_corrected_ratios['spot_name'] == spot_name].copy()
        for isotope_ratio in session_correction_results.keys():
            session_fit = session_correction_results[isotope_ratio]
            fit_evaluation = evaluate_session_fractionation_fit(
                session_fit,
                this_spot_data['Time'],
            )
            this_spot_data[isotope_ratio + '_session_correction_factor'] = fit_evaluation['prediction']
            this_spot_data[isotope_ratio + '_session_correction_1s'] = fit_evaluation['prediction_1sigma']
            this_spot_data[isotope_ratio + '_session_correction_extrapolated'] = fit_evaluation['is_extrapolated']
            this_spot_data[isotope_ratio + '_fractionation_corrected'] = (
                this_spot_data[isotope_ratio + '_DH_corrected']
                * fit_evaluation['prediction']
            )
        fractionation_corrected_ratios = pd.concat([fractionation_corrected_ratios, this_spot_data])

    if "downhole_corrected_covariance" in DH_corrected_ratios.attrs:
        fractionation_corrected_ratios.attrs["downhole_corrected_covariance"] = (
            DH_corrected_ratios.attrs["downhole_corrected_covariance"]
        )

    return fractionation_corrected_ratios

__all__ = [
    'calc_DH_nominal_fractionation',
    'calc_DH_fractionations_rad',
    'calc_DH_fractionations_TW',
    'calc_DH_dynamic_fractionation',
    'apply_DH_correction',
    'calc_session_nominal_fractionation',
    'calc_session_fractionation_rad',
    'calc_session_fractionation_TW',
    'apply_session_correction',
]
