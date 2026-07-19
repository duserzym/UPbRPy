"""Trace-element concentration calibration and calculation."""

from __future__ import annotations

import re

from bokeh.models import ColumnDataSource, HoverTool
from bokeh.plotting import figure
import numpy as np
import pandas as pd
from bokeh.palettes import Category20
from scipy.stats import t

def internal_normalization(experiment, channels, baseline_subtracted_total_data, standard_archive, material='zircon'):
    '''
    function for internal normalization of the data, sweep-wise correction
    '''
    if material == 'zircon':
        internal_analyte = '29Si'
        unknown_concentration = 151443
    elif material == 'apatite':
        internal_analyte = '44Ca'
        unknown_concentration = 400443
    elif material == 'rutile':
        internal_analyte = '49Ti'
        unknown_concentration = 599499

    this_standard_archive = standard_archive[standard_archive['material'].isin([material, 'glass'])]

    normalized_plateau_data = pd.DataFrame()
    for spot_name in baseline_subtracted_total_data['spot_name'].unique():
        this_spot_data = baseline_subtracted_total_data[baseline_subtracted_total_data['spot_name'] == spot_name]
        if this_spot_data['sample'].values[0] in this_standard_archive.index:
            internal_analyte_concentration = this_standard_archive.loc[this_spot_data['sample'].values[0], internal_analyte]
        else:
            internal_analyte_concentration = unknown_concentration
            
        # get the baseline start and end times for this spot
        plateau_start_time = experiment[experiment['spot_names'] == spot_name]['plateau_start_times'].values[0]
        plateau_end_time = experiment[experiment['spot_names'] == spot_name]['plateau_end_times'].values[0]
        this_spot_plateau = this_spot_data[(this_spot_data['Time'] >= plateau_start_time) & (this_spot_data['Time'] < plateau_end_time)]
        this_spot_plateau['internal_normalization_factor'] = internal_analyte_concentration / this_spot_plateau[internal_analyte]
        for channel in channels:
            this_spot_plateau.loc[:, channel] = this_spot_plateau[channel] * this_spot_plateau['internal_normalization_factor']
        normalized_plateau_data = pd.concat([normalized_plateau_data, this_spot_plateau])

    normalized_plateau_data.sort_values(by='Time', inplace=True, ignore_index=True)
    return normalized_plateau_data

def filter_concentration_outliers(NIST_data, primary_channel, n = 5):
    '''
    function for filtering out concentration outliers in the NIST glass standards before calibrating the unknowns
        we filter out the points that lie beyond the 95CI which is calculated as std * student's t multiplier with n-1 degree of freedom

    Parameters
    ----------
    NIST_data : pd.DataFrame
        NIST glass standards data
    primary_channel : str
        primary channel for concentration calibration
    n : int
        number of iterations for filtering outliers

    Returns
    -------
    filtered_NIST_data : pd.DataFrame
        filtered NIST glass standards data
    '''
    filtered_NIST_data = pd.DataFrame()
    for spot_name in NIST_data['spot_name'].unique():
        this_spot_data = NIST_data[NIST_data['spot_name'] == spot_name]
        for i in range(n):
            this_spot_channel = this_spot_data[primary_channel]
            this_post_channel_upper = this_spot_channel.mean() + this_spot_channel.std() * t.ppf(0.975, len(this_spot_channel)-1)
            this_post_channel_lower = this_spot_channel.mean() - this_spot_channel.std() * t.ppf(0.975, len(this_spot_channel)-1)
            this_spot_data = this_spot_data[(this_spot_data[primary_channel] < this_post_channel_upper) & (this_spot_data[primary_channel] > this_post_channel_lower)]
        filtered_NIST_data = pd.concat([filtered_NIST_data, this_spot_data], ignore_index=True)
    return filtered_NIST_data

def calc_concentration_calibration_factor(NIST_data, channels, standard_archive):
    '''
    function for calculating the calibration factors for the elemental concentrations
        based on the NIST glass standards (610, 612)
    the standard archive stores all the true ppm values independently measured for the glass and standards
    in the NIST_data dataframe we can extract the mean values for the cps for each NIST measurement and calculate a best-fit origin-anchored line
        whose slope is the calibration factor for cps-ppm conversion
    '''
    calibration_factors = pd.Series()
    effective_channels = [i for i in channels if (i in standard_archive.columns) and (i !='29Si')]
    print('Analytes that can be calibrated for concentration measurements are: ', effective_channels)

    plot = figure(
        width=800,
        height=600,
        title='Calibration factor for concentration measurements',
        x_axis_label='True concentration (ppm)',
        y_axis_label='Measured cps',
        tools='pan,wheel_zoom,box_zoom,reset,save',
        active_scroll='wheel_zoom',
    )
    for i, channel in enumerate(effective_channels):
        this_channel_measured_cps = []
        this_channel_known_ppm = []
        sample_list = []
        spot_list = []
        this_channel_color = Category20[20][i % 20]
        for spot in NIST_data['spot_name'].unique():
            this_spot_data = NIST_data[NIST_data['spot_name'] == spot]
            this_spot_mean = this_spot_data[channel].mean()
            this_channel_measured_cps.append(this_spot_mean)
            this_spot_true_concentration = standard_archive.loc[this_spot_data['sample'].values[0], channel]
            this_channel_known_ppm.append(this_spot_true_concentration)
            sample_list.append(this_spot_data['sample'].values[0])
            spot_list.append(spot)

        # now calculate the calibration factor using the best-fit line anchored at the origin
        this_channel_measured_cps = np.array(this_channel_measured_cps)
        this_channel_known_ppm = np.array(this_channel_known_ppm)
        slope = np.dot(this_channel_measured_cps, this_channel_known_ppm) / np.dot(this_channel_measured_cps, this_channel_measured_cps)
        calibration_factors[channel] = slope
        # plot the data and the best-fit line
        source = ColumnDataSource({
            'sample': sample_list,
            'spot_name': spot_list,
            'cps': this_channel_measured_cps,
            'ppm': this_channel_known_ppm,
        })
        scatter = plot.scatter(
            x='ppm', y='cps', source=source, color=this_channel_color,
            size=5, alpha=0.6, legend_label=channel,
        )
        plot.add_tools(HoverTool(
            renderers=[scatter],
            tooltips=[('channel', channel), ('sample', '@sample'), ('spot', '@spot_name'), ('ppm', '@ppm'), ('cps', '@cps')],
        ))
        max_ppm = float(np.nanmax(this_channel_known_ppm)) if this_channel_known_ppm.size else 0.0
        plot.line(
            [0, max_ppm], [0, max_ppm / slope if slope else 0],
            color=this_channel_color, line_width=2,
        )
    if plot.legend:
        legend = plot.legend[0]
        legend.location = 'top_left'
        legend.orientation = 'horizontal'
        legend.ncols = 2
        legend.click_policy = 'hide'
        legend.label_text_font_size = '8pt'
        legend.spacing = 4
        legend.padding = 8
        # ``legend_label`` initially places the generated legend over the
        # plotting canvas. Move that same model into a dedicated side panel.
        plot.center.remove(legend)
        plot.add_layout(legend, 'right')
    return calibration_factors, plot

def calc_concentration(unknown_standard_data, concentration_calibration, sep='_'):
    '''
    function for calculating the elemental concentrations in the unknown and standard samples
        after the construction of the calibration factors stored in the concentration calibration pandas series

    when calculating the unknonwns, after constructing the mean and standard deviation multiplied by a student's t multiplier with n-1 degrees of freedom
        we exclude the data that fall outside of the 95% confidence interval
    '''
    element_channels = concentration_calibration.index

    # strip the isotope mass number from the channel name as we already aligned the isotope signals to the total elemental concentration 
    elements = [re.sub(r'\d+', '', channel) for channel in element_channels]
    unknown_standard_ppm_df = pd.DataFrame()

    for spot_name in unknown_standard_data['spot_name'].unique():
        this_spot_data = unknown_standard_data[unknown_standard_data['spot_name'] == spot_name]
        for channel, element in zip(element_channels, elements):
            unknown_standard_cps_mean = this_spot_data[channel].mean()
            unknown_standard_cps_std = this_spot_data[channel].std()
            t_multiplier = t.ppf(0.975, this_spot_data.shape[0]-1)
            lower_bound = unknown_standard_cps_mean - t_multiplier * unknown_standard_cps_std
            upper_bound = unknown_standard_cps_mean + t_multiplier * unknown_standard_cps_std

            # now filter for the data that falls within the 95% confidence interval
            this_spot_data_filtered = this_spot_data[(this_spot_data[channel] >= lower_bound) & (this_spot_data[channel] <= upper_bound)]
            filtered_cps_mean = this_spot_data_filtered[channel].mean()
            filtered_cps_95CI = this_spot_data_filtered[channel].std() * t.ppf(0.975, this_spot_data_filtered.shape[0]-1)
            unknown_standard_ppm_df.loc[spot_name, element] = filtered_cps_mean * concentration_calibration[channel]
            unknown_standard_ppm_df.loc[spot_name, element + '_95CI'] = filtered_cps_95CI * concentration_calibration[channel]
    unknown_standard_ppm_df['spot_name'] = unknown_standard_ppm_df.index
    unknown_standard_ppm_df['sample'] = unknown_standard_ppm_df['spot_name'].str.split(sep).str[0]
    # now move the spot and sample columns up front
    unknown_standard_ppm_df = unknown_standard_ppm_df[['spot_name', 'sample'] + [i for i in unknown_standard_ppm_df.columns if i not in ['spot_name', 'sample']]]
    return unknown_standard_ppm_df

def calc_concentration_depth_profile(unknown_standard_data, concentration_calibration, sep='_'):
    '''
    function for calculating the elemental concentrations in the unknown and standard samples
        after the construction of the calibration factors stored in the concentration calibration pandas series
        while we do 95% confidence interval filtering when calculating the spot mean concentrations, we do not do this when exporting the sweep-wise depth profile data
        for each sweep the concentration uncertainties are propagated using Poisson statistics on the raw counting data and the calibration factors uncertainties are ignored
    '''
    depthpro_concentration_df = pd.DataFrame()
    # get all the calibrated element channels
    element_channels = concentration_calibration.index
    
    # strip the isotope mass number from the channel name as we already aligned the isotope signals to the total elemental concentration 
    elements = [re.sub(r'\d+', '', channel) for channel in element_channels]

    depthpro_concentration_df['Time'] = unknown_standard_data['Time']
    depthpro_concentration_df['sample'] = unknown_standard_data['sample']
    depthpro_concentration_df['spot_name'] = unknown_standard_data['spot_name']

    for channel, element in zip(element_channels, elements):

        depthpro_concentration_df[element] = unknown_standard_data[channel] * concentration_calibration[channel]
        # replace negative values with NaN
        depthpro_concentration_df[element]  = depthpro_concentration_df[element].mask(depthpro_concentration_df[element]  < 0, np.nan)
        depthpro_concentration_df[element + '_95CI'] = np.sqrt(depthpro_concentration_df[element]) # propagate uncertainty using Poisson statistics, ignore calibration uncertainty

    return depthpro_concentration_df

__all__ = [
    'internal_normalization',
    'filter_concentration_outliers',
    'calc_concentration_calibration_factor',
    'calc_concentration',
    'calc_concentration_depth_profile',
]
