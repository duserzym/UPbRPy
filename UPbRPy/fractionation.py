"""Downhole and session fractionation correction workflows."""

from __future__ import annotations

from bokeh.plotting import figure
import numpy as np
import pandas as pd
from bokeh.palettes import Category20

from .calibration import evaluate_correction_result, fit_calibration_model
from .ratios import project_tw_206pb_238u
from .uncertainty import (
    inverse_standard_deviation_weights,
    relative_time_by_spot,
)


def align_downhole_traces(spot_times, spot_values):
    """Interpolate spot traces across the full observed signal-relative interval.

    Values outside an individual spot's measured interval remain NaN rather
    than being endpoint-extrapolated. Consequently, late signal positions use
    only standards that still contain measured signal sweeps.
    """
    if len(spot_times) != len(spot_values) or not spot_times:
        raise ValueError("spot_times and spot_values must be nonempty lists of equal length.")
    cleaned_times = []
    cleaned_values = []
    for times, values in zip(spot_times, spot_values):
        times = np.asarray(times, dtype=float)
        values = np.asarray(values, dtype=float)
        valid = np.isfinite(times) & np.isfinite(values)
        times = times[valid]
        values = values[valid]
        order = np.argsort(times)
        times = times[order]
        values = values[order]
        if times.size < 2 or times[-1] <= times[0]:
            continue
        cleaned_times.append(times)
        cleaned_values.append(values)
    if not cleaned_times:
        raise ValueError("No downhole trace contains two finite time-value pairs.")
    common_start = min(times[0] for times in cleaned_times)
    common_end = max(times[-1] for times in cleaned_times)
    point_count = max(len(times) for times in cleaned_times)
    if point_count < 2:
        raise ValueError("Downhole traces share fewer than two signal sweeps.")
    unified_time = np.linspace(common_start, common_end, point_count)
    interpolated = []
    for times, values in zip(cleaned_times, cleaned_values):
        trace = np.interp(unified_time, times, values)
        trace[(unified_time < times[0]) | (unified_time > times[-1])] = np.nan
        interpolated.append(trace)
    stacked_values = np.asarray(interpolated, dtype=float)
    return unified_time, stacked_values, cleaned_times, cleaned_values

def calc_DH_nominal_fractionation(
    known_ratio,
    primary_standard_ratios,
    ratio_name,
    return_metadata=False,
    correction_model='mean',
    model_kwargs=None,
):
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
        this_spot = primary_standard_ratios[
            primary_standard_ratios['spot_name'] == spot_name
        ].copy()
        this_spot['relative_time'] = relative_time_by_spot(this_spot)
        valid = (
            np.isfinite(this_spot['relative_time'])
            & np.isfinite(this_spot[ratio_name])
            & (this_spot[ratio_name] > 0)
        )
        this_spot = this_spot.loc[valid]
        if len(this_spot) < 2:
            continue

        # store the measured time and ratios for this spot
        all_times.append(this_spot['relative_time'].values)
        all_ratios.append(this_spot[ratio_name].values)
        plot.line(
            this_spot['relative_time'].values,
            known_ratio / this_spot[ratio_name].values,
            color=Category20[20][i % 20], line_width=1, alpha=0.6,
        )

    # now interpolate the time and fractionation for all spots, unify the time axis, and calculate the mean and std * student's t multiplier
    unified_time, interpolated_ratios, all_times, all_ratios = align_downhole_traces(
        all_times, all_ratios
    )
    
    # calculate the stacked mean sweep-wise ratio vs. relative time
    mean_ratio = np.nanmean(interpolated_ratios, axis=0)
    # calculate the fractionation factor as known ratio / mean_ratio
    mean_fractionation_factor = known_ratio / mean_ratio
    interpolated_fractionations = known_ratio / np.asarray(interpolated_ratios, dtype=float)
    model_kwargs = {} if model_kwargs is None else dict(model_kwargs)
    model_method = (
        'interpolate'
        if isinstance(correction_model, str)
        and correction_model.strip().lower().replace('-', '_').replace(' ', '_')
        in {'mean', 'pointwise_mean'}
        else correction_model
    )
    fitted_model = fit_calibration_model(
        unified_time,
        mean_fractionation_factor,
        method=model_method,
        **model_kwargs,
    )
    downhole_fractionation = fitted_model
    fitted_fractionation_factor = downhole_fractionation(unified_time)

    # calculate the std of the mean_fractionation_factor based on the difference between the mean and the individual ratios
    all_times_array = np.concatenate(all_times)
    all_fractionations_array = known_ratio / np.concatenate(all_ratios)
    # calculate the standard deviation of the mean_fractionation_factor
    std_fractionation_factor = np.std([all_fractionations_array[i] - downhole_fractionation(all_times_array[i]) for i in range(len(all_times_array))])
    residuals = interpolated_fractionations - fitted_fractionation_factor
    downhole_metadata = {
        'time': unified_time,
        'mean_fractionation': mean_fractionation_factor,
        'fitted_fractionation': fitted_fractionation_factor,
        'model': 'mean' if model_method == 'interpolate' else fitted_model.method,
        'model_parameters': fitted_model.parameters,
        'model_settings': fitted_model.settings,
        'residuals': residuals,
        'residual_standard_deviation': std_fractionation_factor,
        'contributor_count': np.count_nonzero(
            np.isfinite(interpolated_fractionations), axis=0
        ),
    }

    plot.varea(
        unified_time, fitted_fractionation_factor - std_fractionation_factor,
        fitted_fractionation_factor + std_fractionation_factor,
        fill_color='grey', fill_alpha=0.12,
    )
    plot.line(unified_time, fitted_fractionation_factor, color='black', line_width=2, line_dash='dashed', legend_label=f'{downhole_metadata["model"]} correction model')
    plot.line(unified_time, fitted_fractionation_factor + std_fractionation_factor, color='grey', line_dash='dashed', line_width=1, legend_label='residual 1 sigma')
    plot.line(unified_time, fitted_fractionation_factor - std_fractionation_factor, color='grey', line_dash='dashed', line_width=1)
    plot.legend.location = 'top_right'

    if return_metadata:
        return plot, downhole_fractionation, downhole_metadata
    return plot, downhole_fractionation

def calc_DH_fractionations_rad(primary_standard_ratios, 
                                standard_archive, 
                                ratio_names = ['206Pb/238U', '207Pb/206Pb', '208Pb/232Th'], 
                                fit=None,
                                *,
                                model='mean',
                                model_kwargs=None):
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
    model : str or callable, optional
        Downhole calibration model. ``'mean'`` (default) interpolates the
        pointwise mean of aligned reference-material traces. Other built-ins
        are ``'interpolate'``, ``'linear'``, ``'polynomial'``,
        ``'orthogonal_polynomial'``, ``'exponential'``, and ``'spline'``.
        A callable may be parametric or an already calibrated predictor.
        ``fit`` is retained as a backward-compatible alias for ``model``.
    model_kwargs : dict, optional
        Options passed to :func:`fit_calibration_model`, such as polynomial
        degree, spline smoothing, or custom-function initial values.
    '''

    if fit is not None:
        model = fit
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
            correction_model=model,
            model_kwargs=model_kwargs,
        )
        # append the results to the dictionary
        DH_correction_results.update({ratio_name: {
            'plot': plot,
            'interpolation': downhole_fractionation,
            'metadata': downhole_metadata,
            'model': downhole_metadata['model'],
            'model_parameters': downhole_metadata['model_parameters'],
            'fit_prediction_x': downhole_metadata['time'],
            'fit_residual_standard_deviation': downhole_metadata['residual_standard_deviation'],
            'fit_residuals': downhole_metadata['residuals'],
        }})
    return DH_correction_results

def calc_DH_fractionations_TW(primary_68_standard_ratios, 
                                primary_76_standard_ratios, 
                                primary_82_standard_ratios, 
                                standard_archive, fit=None, *, model='mean', model_kwargs=None):
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
    model : str or callable, optional
        Downhole calibration model; see :func:`calc_DH_fractionations_rad`.
    '''

    if fit is not None:
        model = fit
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
        correction_model=model,
        model_kwargs=model_kwargs,
    )
    DH_correction_results.update({'207Pb/206Pb': {
        'plot': plot_76,
        'interpolation': downhole_fractionation_76,
        'metadata': downhole_metadata_76,
        'model': downhole_metadata_76['model'],
        'model_parameters': downhole_metadata_76['model_parameters'],
        'fit_prediction_x': downhole_metadata_76['time'],
        'fit_residual_standard_deviation': downhole_metadata_76['residual_standard_deviation'],
        'fit_residuals': downhole_metadata_76['residuals'],
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
        correction_model=model,
        model_kwargs=model_kwargs,
    )
    DH_correction_results.update({'206Pb/238U': {
        'plot': plot_68,
        'interpolation': downhole_fractionation_68,
        'metadata': downhole_metadata_68,
        'model': downhole_metadata_68['model'],
        'model_parameters': downhole_metadata_68['model_parameters'],
        'fit_prediction_x': downhole_metadata_68['time'],
        'fit_residual_standard_deviation': downhole_metadata_68['residual_standard_deviation'],
        'fit_residuals': downhole_metadata_68['residuals'],
    }})

    # add the 8/2 ratio correction
    known_PR_82_ratio = standard_archive.loc[primary_82_standard, '208Pb/232Th*'] # for the radiogenic 82 ratio
    plot_82, downhole_fractionation_82, downhole_metadata_82 = calc_DH_nominal_fractionation(
        known_PR_82_ratio,
        primary_82_standard_ratios,
        '208Pb/232Th',
        return_metadata=True,
        correction_model=model,
        model_kwargs=model_kwargs,
    )
    DH_correction_results.update({'208Pb/232Th': {
        'plot': plot_82,
        'interpolation': downhole_fractionation_82,
        'metadata': downhole_metadata_82,
        'model': downhole_metadata_82['model'],
        'model_parameters': downhole_metadata_82['model_parameters'],
        'fit_prediction_x': downhole_metadata_82['time'],
        'fit_residual_standard_deviation': downhole_metadata_82['residual_standard_deviation'],
        'fit_residuals': downhole_metadata_82['residuals'],
    }})
    return DH_correction_results

def calc_DH_dynamic_fractionation(
    primary_standard_ratios,
    measured_ratio_name,
    projected_ratio_name,
    return_metadata=False,
    correction_model='mean',
    model_kwargs=None,
):
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
        this_spot = primary_standard_ratios[
            primary_standard_ratios['spot_name'] == spot_name
        ].copy()
        this_spot['relative_time'] = relative_time_by_spot(this_spot)
        valid = (
            np.isfinite(this_spot['relative_time'])
            & np.isfinite(this_spot[measured_ratio_name])
            & np.isfinite(this_spot[projected_ratio_name])
            & (this_spot[measured_ratio_name] > 0)
        )
        this_spot = this_spot.loc[valid]
        if len(this_spot) < 2:
            continue
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
    unified_time, interpolated_fractionations, all_times, all_fractionations = (
        align_downhole_traces(all_times, all_fractionations)
    )
    
    # calculate the stacked mean sweep-wise fractionation vs. relative time
    mean_fractionation_factor = np.nanmean(interpolated_fractionations, axis=0)
    model_kwargs = {} if model_kwargs is None else dict(model_kwargs)
    model_method = (
        'interpolate'
        if isinstance(correction_model, str)
        and correction_model.strip().lower().replace('-', '_').replace(' ', '_')
        in {'mean', 'pointwise_mean'}
        else correction_model
    )
    fitted_model = fit_calibration_model(
        unified_time,
        mean_fractionation_factor,
        method=model_method,
        **model_kwargs,
    )
    # calculate the std of the mean_fractionation_factor based on the difference between the mean and the individual ratios
    all_times_array = np.concatenate(all_times)
    all_fractionations_array = np.concatenate(all_fractionations)
    downhole_fractionation = fitted_model
    fitted_fractionation_factor = downhole_fractionation(unified_time)
    # calculate the standard deviation of the mean_fractionation_factor
    std_fractionation_factor = np.std([all_fractionations_array[i] - downhole_fractionation(all_times_array[i]) for i in range(len(all_times_array))])
    residuals = interpolated_fractionations - fitted_fractionation_factor
    downhole_metadata = {
        'time': unified_time,
        'mean_fractionation': mean_fractionation_factor,
        'fitted_fractionation': fitted_fractionation_factor,
        'model': 'mean' if model_method == 'interpolate' else fitted_model.method,
        'model_parameters': fitted_model.parameters,
        'model_settings': fitted_model.settings,
        'residuals': residuals,
        'residual_standard_deviation': std_fractionation_factor,
        'contributor_count': np.count_nonzero(
            np.isfinite(interpolated_fractionations), axis=0
        ),
    }

    plot.varea(
        unified_time, fitted_fractionation_factor - std_fractionation_factor,
        fitted_fractionation_factor + std_fractionation_factor,
        fill_color='grey', fill_alpha=0.12,
    )
    plot.line(unified_time, fitted_fractionation_factor, color='black', line_width=2, line_dash='dashed', legend_label=f'{downhole_metadata["model"]} correction model')
    plot.line(unified_time, fitted_fractionation_factor + std_fractionation_factor, color='grey', line_dash='dashed', line_width=1, legend_label='residual 1 sigma')
    plot.line(unified_time, fitted_fractionation_factor - std_fractionation_factor, color='grey', line_dash='dashed', line_width=1)
    plot.legend.location = 'top_right'

    if return_metadata:
        return plot, downhole_fractionation, downhole_metadata
    return plot, downhole_fractionation

def apply_DH_correction(
    plateau_data_with_raw_ratios,
    DH_correction_results,
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
            relative_time = relative_time_by_spot(this_spot_data)
            correction_factor = this_spot_DH_correction(relative_time)
            model_time = DH_correction_results[isotope_ratio].get('fit_prediction_x')
            if model_time is not None:
                model_time = np.asarray(model_time, dtype=float)
                this_spot_data[isotope_ratio + '_DH_correction_extrapolated'] = (
                    (relative_time < np.nanmin(model_time))
                    | (relative_time > np.nanmax(model_time))
                )
            # All UPbRPy fractionation factors use the convention
            # C = R_target / R_measured, so R_corrected = C * R_measured.
            this_spot_data[isotope_ratio + '_DH_correction_factor'] = correction_factor
            this_spot_data[isotope_ratio + '_DH_corrected'] = (
                this_spot_data[isotope_ratio] * correction_factor
            )

        corrected_plateau_data = pd.concat([corrected_plateau_data, this_spot_data])

    corrected_plateau_data.attrs = {}
    return corrected_plateau_data

def calc_session_nominal_fractionation(
    known_ratio,
    primary_standard_ratios,
    ratio_name,
    degree=2,
    model='polynomial',
    model_kwargs=None,
):
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
        this_spot_mean_ratio = this_spot[ratio_name+'_DH_corrected'].mean()
        this_spot_mean_fractionation = known_ratio / this_spot_mean_ratio
        this_spot_mean_fractionation_std = np.std(this_spot_mean_fractionations)
        # this_spot_mean_fractionation_std = known_ratio / this_spot_mean_ratio_std

        # store the calculated mean time and fractionation factors
        spot_mean_times.append(this_spot_mean_time)
        spot_mean_fractionation.append(this_spot_mean_fractionation)
        spot_mean_fractionation_std.append(this_spot_mean_fractionation_std)

    # Fit the configured model to the mean DH-corrected ratios against time.
    spot_mean_times = np.array(spot_mean_times)
    spot_mean_fractionation = np.array(spot_mean_fractionation)
    spot_mean_fractionation_std = np.array(spot_mean_fractionation_std)
    spot_mean_fractionation_weights = inverse_standard_deviation_weights(
        spot_mean_fractionation_std
    )
    model_kwargs = {} if model_kwargs is None else dict(model_kwargs)
    model_kwargs.setdefault('degree', degree)
    fitted_model = fit_calibration_model(
        spot_mean_times,
        spot_mean_fractionation,
        method=model,
        weights=spot_mean_fractionation_weights,
        **model_kwargs,
    )
    p = fitted_model.parameters
    p_std = fitted_model.residual_standard_deviation
    
    # Calculate a fitted curve across the observed session interval.
    fit_x = np.linspace(np.nanmin(spot_mean_times), np.nanmax(spot_mean_times), 100)
    fitted_values = fitted_model(fit_x)

    plot = figure(
        width=800, height=400,
        title=ratio_name + ' fractionation correction factor for primary standard ' + primary_standard,
        x_axis_label='Time (s)', y_axis_label=ratio_name + ' fractionation factor',
        tools='pan,wheel_zoom,box_zoom,reset,save', active_scroll='wheel_zoom',
    )
    plot.scatter(spot_mean_times, spot_mean_fractionation, color='black', size=6, legend_label='mean fractionation factor')
    plot.line(fit_x, fitted_values, color='red', line_width=2, legend_label=f'{fitted_model.method} session model')
    plot.varea(fit_x, fitted_values - p_std, fitted_values + p_std, fill_color='grey', fill_alpha=0.12)
    plot.legend.location = 'top_right'
    
    fit_metadata = {
        "model": fitted_model.method,
        "predictor": fitted_model,
        "parameters": fitted_model.parameters,
        "model_settings": fitted_model.settings,
        "prediction_x": fit_x,
        "residual_standard_deviation": p_std,
        "residuals": fitted_model.residuals,
        "degrees_of_freedom": fitted_model.degrees_of_freedom,
        "spot_mean_times": spot_mean_times,
        "spot_mean_fractionation": spot_mean_fractionation,
        "spot_mean_fractionation_std": spot_mean_fractionation_std,
        "weights": spot_mean_fractionation_weights,
    }

    return plot, p, p_std, fit_metadata

def calc_session_fractionation_rad(primary_standard_DH_corrected_ratios, 
                                    standard_archive, 
                                    ratio_names=['206Pb/238U', '207Pb/206Pb', '208Pb/232Th'], 
                                    degree=2,
                                    model='polynomial',
                                    model_kwargs=None):
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
        Degree used by polynomial and orthogonal-polynomial models.
    model : str or callable, optional
        Session calibration model. Built-ins are ``'mean'``, ``'interpolate'``,
        ``'linear'``, ``'polynomial'``, ``'orthogonal_polynomial'``,
        ``'exponential'``, and ``'spline'``. Custom callables follow
        :func:`fit_calibration_model`.

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
        plot, p, p_std, fit_metadata = calc_session_nominal_fractionation(
            known_PR_ratio,
            primary_standard_DH_corrected_ratios,
            ratio_name,
            degree,
            model=model,
            model_kwargs=model_kwargs,
        )
        # append the results to the dictionary
        session_correction_results.update({ratio_name: {
            'plot': plot,
            'fit': p,
            'predictor': fit_metadata['predictor'],
            'model': fit_metadata['model'],
            'model_parameters': fit_metadata['parameters'],
            'std': p_std,
            'fit_prediction_x': fit_metadata['prediction_x'],
            'fit_residual_standard_deviation': fit_metadata['residual_standard_deviation'],
            'fit_residuals': fit_metadata['residuals'],
            'fit_degrees_of_freedom': fit_metadata['degrees_of_freedom'],
            'fit_observations': {
                'time': fit_metadata['spot_mean_times'],
                'fractionation': fit_metadata['spot_mean_fractionation'],
                'std': fit_metadata['spot_mean_fractionation_std'],
                'weights': fit_metadata['weights'],
            },
        }})

    return session_correction_results

def calc_session_fractionation_TW(
    primary_68_DH_ratios,
    primary_76_DH_ratios,
    primary_82_DH_ratios,
    standard_archive,
    degree=2,
    model='polynomial',
    model_kwargs=None,
):
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
    plot_76, p_76, p_std_76, fit_metadata_76 = calc_session_nominal_fractionation(
        known_PR_76_ratio,
        primary_76_DH_ratios,
        '207Pb/206Pb',
        degree,
        model=model,
        model_kwargs=model_kwargs,
    )

    session_correction_results.update({'207Pb/206Pb': {
        'plot': plot_76,
        'fit': p_76,
        'predictor': fit_metadata_76['predictor'],
        'model': fit_metadata_76['model'],
        'model_parameters': fit_metadata_76['parameters'],
        'std': p_std_76,
        'fit_prediction_x': fit_metadata_76['prediction_x'],
        'fit_residual_standard_deviation': fit_metadata_76['residual_standard_deviation'],
        'fit_residuals': fit_metadata_76['residuals'],
        'fit_degrees_of_freedom': fit_metadata_76['degrees_of_freedom'],
        'fit_observations': {
            'time': fit_metadata_76['spot_mean_times'],
            'fractionation': fit_metadata_76['spot_mean_fractionation'],
            'std': fit_metadata_76['spot_mean_fractionation_std'],
            'weights': fit_metadata_76['weights'],
        },
    }})

    # then use this fractionation factor to correct the 7/6 ratio of the primary 68 standard through the session
    primary_68_DH_ratios['207Pb/206Pb_session_corrected'] = (
        primary_68_DH_ratios['207Pb/206Pb_DH_corrected']
        * fit_metadata_76['predictor'](primary_68_DH_ratios['Time'])
    )
    
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
    # Fit the configured model to mean DH-corrected ratios against time.
    spot_mean_times = np.array(spot_mean_times)
    spot_mean_68_fractionation = np.array(spot_mean_68_fractionation)
    spot_mean_68_fractionation_std = np.array(spot_mean_68_fractionation_std)
    spot_mean_68_fractionation_weights = inverse_standard_deviation_weights(
        spot_mean_68_fractionation_std
    )
    model_kwargs_68 = {} if model_kwargs is None else dict(model_kwargs)
    model_kwargs_68.setdefault('degree', degree)
    fitted_model_68 = fit_calibration_model(
        spot_mean_times,
        spot_mean_68_fractionation,
        method=model,
        weights=spot_mean_68_fractionation_weights,
        **model_kwargs_68,
    )
    p_68 = fitted_model_68.parameters
    p_std_68 = fitted_model_68.residual_standard_deviation
    # Calculate a fitted curve across the observed session interval.
    fit_x_68 = np.linspace(np.nanmin(spot_mean_times), np.nanmax(spot_mean_times), 100)
    fitted_values_68 = fitted_model_68(fit_x_68)
    plot_68 = figure(
        width=800, height=400,
        title='206Pb/238U fractionation correction factor for primary standard ' + primary_68_standard,
        x_axis_label='Time (s)', y_axis_label='206Pb/238U fractionation factor',
        tools='pan,wheel_zoom,box_zoom,reset,save', active_scroll='wheel_zoom',
    )
    plot_68.scatter(spot_mean_times, spot_mean_68_fractionation, color='black', size=6, legend_label='mean fractionation factor')
    plot_68.line(fit_x_68, fitted_values_68, color='red', line_width=2, legend_label=f'{fitted_model_68.method} session model')
    plot_68.varea(fit_x_68, fitted_values_68 - p_std_68, fitted_values_68 + p_std_68, fill_color='grey', fill_alpha=0.12)
    plot_68.legend.location = 'top_right'
    
    session_correction_results.update({'206Pb/238U': {
        'plot': plot_68,
        'fit': p_68,
        'predictor': fitted_model_68,
        'model': fitted_model_68.method,
        'model_parameters': fitted_model_68.parameters,
        'std': p_std_68,
        'fit_prediction_x': fit_x_68,
        'fit_residual_standard_deviation': p_std_68,
        'fit_residuals': fitted_model_68.residuals,
        'fit_degrees_of_freedom': fitted_model_68.degrees_of_freedom,
        'fit_observations': {
            'time': spot_mean_times,
            'fractionation': spot_mean_68_fractionation,
            'std': spot_mean_68_fractionation_std,
            'weights': spot_mean_68_fractionation_weights,
        },
    }})

    # add the 8/2 ratio correction
    known_PR_82_ratio = standard_archive.loc[primary_82_standard, '208Pb/232Th*'] # for the radiogenic 82 ratio
    plot_82, p_82, p_std_82, fit_metadata_82 = calc_session_nominal_fractionation(
        known_PR_82_ratio,
        primary_82_DH_ratios,
        '208Pb/232Th',
        degree,
        model=model,
        model_kwargs=model_kwargs,
    )
    session_correction_results.update({'208Pb/232Th': {
        'plot': plot_82,
        'fit': p_82,
        'predictor': fit_metadata_82['predictor'],
        'model': fit_metadata_82['model'],
        'model_parameters': fit_metadata_82['parameters'],
        'std': p_std_82,
        'fit_prediction_x': fit_metadata_82['prediction_x'],
        'fit_residual_standard_deviation': fit_metadata_82['residual_standard_deviation'],
        'fit_residuals': fit_metadata_82['residuals'],
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
            correction_factor = evaluate_correction_result(
                session_fit, this_spot_data['Time']
            )
            this_spot_data[isotope_ratio + '_session_correction_factor'] = correction_factor
            this_spot_data[isotope_ratio + '_session_correction_1s'] = session_fit['std']
            fit_times = np.asarray(
                session_fit.get('fit_observations', {}).get('time', []),
                dtype=float,
            )
            if fit_times.size:
                this_spot_data[isotope_ratio + '_session_correction_extrapolated'] = (
                    (this_spot_data['Time'] < np.nanmin(fit_times))
                    | (this_spot_data['Time'] > np.nanmax(fit_times))
                )
            else:
                this_spot_data[isotope_ratio + '_session_correction_extrapolated'] = False
            this_spot_data[isotope_ratio + '_fractionation_corrected'] = (
                this_spot_data[isotope_ratio + '_DH_corrected']
                * correction_factor
            )
        fractionation_corrected_ratios = pd.concat([fractionation_corrected_ratios, this_spot_data])

    fractionation_corrected_ratios.attrs = {}
    return fractionation_corrected_ratios

__all__ = [
    'align_downhole_traces',
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
