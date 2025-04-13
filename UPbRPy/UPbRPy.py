import os
from importlib.resources import files
import re
from datetime import datetime, timedelta
import pandas as pd
import hvplot.pandas
import numpy as np
import panel as pn

from scipy.stats import t
from scipy.interpolate import interp1d

from bokeh.palettes import Category20

import holoviews as hv
import holoviews.operation.datashader as hd

hv.extension("bokeh", "matplotlib", "plotly") 
hv.output(backend="bokeh")

# constants
# -----------------------------------------------------------
common_U238_U235 = 137.818 # U238/U235 ratio
old_common_U238_U235 = 137.88 # previous U238/U235 ratio

lambda_235 = 9.8485e-10 # decay constant for U235

lambda_238 = 1.55125e-10 # decay constant for U238

# import the standard archive stored in the package folder
standard_archive = pd.read_csv(files(__package__) / 'standards.csv', index_col=0)

# I/O
# -----------------------------------------------------------
# function for reading LA-ICP-MS data
# in the format of .csv files exported from Thermo iCap with each file representing a spot
def read_experiment_data(directory, primary_channel='238U', baseline_sweep_n =10, peak_sweep_n = 20, threshold=0.1, filter_percentile=70, baseline_offset=2, plateau_offset=2, sep='_'):
    '''
    function for extracting spot names from files exported from Thermo iCap

    You may follow the instruction on Iolite website below for guidance on generating the proper files from iCap
    https://iolite.xyz/docs/import_prep/exporting_data_from_instruments.html

    Parameters
    ----------
    directory : str
        path to the folder containing the files
        we assume that the files in the folders are in .csv format
    
    Returns
    -------
    spot_names : list
        list of spot names extracted from the files
    spot_paths : list
        list of paths to the files
    '''
    spot_names = []
    spot_paths = []
    spot_starting_times = []
    # use the first .csv file to get the channel names
    first_file = open(os.path.join(directory,[f if f.endswith('.csv') else None for f in os.listdir(directory)][0])).readlines()

    # now update the global variable channels
    element_channels = first_file[13].split(',')[1:-1]

    time_format = "%m/%d/%Y %I:%M:%S %p"

    # make a pandas dataframe that stitches all data of all spots together
    total_data = pd.DataFrame()

    # loop through all the .csv files in the directory using io and get their first lines
    for file in os.listdir(directory):
        if file.endswith('.csv'):
            # use os tools to get the time of start, spot name and path of the file
            this_path = os.path.join(directory, file)
            spot_paths.append(this_path)
            file = open(this_path)
            lines = file.readlines()
            this_spot_name = lines[0].split(':')[0]
            spot_names.append(this_spot_name)
            this_starting_time = datetime.strptime(lines[0][-24:-2], time_format)
            spot_starting_times.append(this_starting_time)
            file.close()
        # create a pandas dataframe to store the experiment data
    experiment = pd.DataFrame()
    experiment['spot_names'] = spot_names
    experiment['sample'] = [spot_name.split(sep)[0] for spot_name in spot_names]
    experiment['spot_paths'] = spot_paths
    experiment['spot_starting_times'] = spot_starting_times

    experiment = experiment.sort_values(by='spot_starting_times').reset_index(drop=True)
    for i in range(experiment.shape[0]):
        # then use pandas to read csv files and append them to the total data
        this_data = pd.read_csv(experiment['spot_paths'][i], skiprows=13)
        this_data = this_data.iloc[1:].reset_index(drop=True)
        this_data = this_data.dropna(axis=1, how='all')
        this_data = this_data.apply(pd.to_numeric, errors='coerce')
        # make sure to change the time column to datetime and add start time to all the times
        this_data['Time'] = [experiment['spot_starting_times'][i] + timedelta(seconds=this_data['Time'][j]) for j in range(this_data.shape[0])]
        this_data['spot_name'] = experiment['spot_names'][i]
        this_data['sample'] = experiment['sample'][i]
        total_data = pd.concat([total_data, this_data], axis=0)

    experiment = experiment.sort_values(by='spot_names').reset_index(drop=True)
    #initialize the columns for the experiment dataframe with null values

    # we should also align the time column in both the experiment and total data to be in total seconds with respect to the start of the experiment
    # this will help with the later session-wide fractionation corrections
    experiment_start_time = experiment['spot_starting_times'].min()
    experiment['spot_starting_times'] = (experiment['spot_starting_times'] - experiment_start_time).dt.total_seconds()
    total_data['Time'] = (total_data['Time'] - experiment_start_time).dt.total_seconds()

    experiment['plateau_start_times'] = None
    experiment['plateau_end_times'] = None
    experiment['baseline_start_times'] = None
    experiment['baseline_end_times'] = None

    experiment = auto_select_baseline_peak(experiment, total_data, primary_channel, baseline_sweep_n, peak_sweep_n, threshold, filter_percentile, baseline_offset, plateau_offset)

    return experiment, element_channels, total_data

# read spot data
def read_spot_data(spot_path):
    '''
    function for reading spot data from a .csv file exported from Thermo iCap

    Parameters
    ----------
    spot_path : str
        path to the file containing the spot data
    
    Returns
    -------
    spot_data : pandas.DataFrame
        a pandas dataframe containing the spot data
    '''
    spot_data = pd.read_csv(spot_path, skiprows=13)
    spot_data = spot_data.iloc[1:].reset_index(drop=True)
    spot_data = spot_data.dropna(axis=1, how='all')
    spot_data = spot_data.apply(pd.to_numeric, errors='coerce')
    return spot_data


def select_baseline_peak(data, baseline_sweep_n =10, peak_sweep_n = 20, threshold=0.1, filter_percentile=70, baseline_offset=2, plateau_offset=2):
    '''
    general function for selecting the baseline and peak regions in the data
    detect the start of the plateau by finding the first significant increase in cps

    Parameters
    ----------
    data : list
        a list of values representing the data
    peak_sweep_n : int
        the number of sweeps in the experimental set up for the sample measurement
    threshold : float
        the threshold for selecting the baseline region
        we use the threshold times mean of the top percentile of the data
    filter_percentile : float
        the percentile of the data to use for filtering the data
    
    Returns
    -------
    plateau_indices_start : int
        the index of the start of the plateau region
    plateau_indices_end : int
        the index of the end of the plateau region
    baseline_indices_start : int
        the index of the start of the baseline region
    baseline_indices_end : int
        the index of the end of the baseline region
    '''
    # first make a rough sample signal estiamte by selecting the top 30% of the data
    data = np.array(data)
    rough_plateau_data = data[data > np.percentile(data, filter_percentile)]
    rough_plateau_mean = np.mean(rough_plateau_data)
    threshold_value = rough_plateau_mean * threshold
    # find the first index where the difference is greater than the threshold
    plateau_indices_start = np.where(data > threshold_value)[0][0] + plateau_offset
    plateau_indices_end = plateau_indices_start + peak_sweep_n
    baseline_indices_end = np.where(data > threshold_value)[0][0] - baseline_offset

    baseline_indices_start = baseline_indices_end - baseline_sweep_n

    # deal with situation where the signal is bad (e.g. wrong phase in the unknonwns such that there is no characteristic signal)
    if baseline_indices_start < 0:
        baseline_indices_start = 0
        baseline_indices_end = baseline_sweep_n
        plateau_indices_start = baseline_sweep_n + plateau_offset
        plateau_indices_end = baseline_sweep_n + peak_sweep_n + plateau_offset
    return plateau_indices_start, plateau_indices_end, baseline_indices_start, baseline_indices_end

def auto_select_baseline_peak(experiment, 
                              total_data, 
                              primary_channel, 
                              baseline_sweep_n =10, 
                              peak_sweep_n = 20, 
                              threshold=0.1, 
                              filter_percentile=70, 
                              baseline_offset=2, 
                              plateau_offset=2):
    '''
    specific function for selecting the baseline and peak regions in the data given a parsed experiment dataframe

    Parameters
    ----------
    experiment: pandas.DataFrame
        a pandas dataframe containing the experiment metadata
    total_data : pandas.DataFrame
        a pandas dataframe containing the total data
    baseline_z_threshold : float
        the threshold for selecting the baseline region
    plateau_z_threshold : float
        the threshold for selecting the peak region
    
    Returns
    -------
    experiment : pandas.DataFrame
        a pandas dataframe containing the spot data with the baseline and peak regions selected
    '''
    spot_names = total_data['spot_name'].unique()

    for spot_name in spot_names:
        spot_data = total_data[total_data['spot_name'] == spot_name]
        # always use 29Si for glass
        if 'NIST' in spot_name:
            values = spot_data['29Si'].tolist()
        else:
            values = spot_data[primary_channel].tolist()
        plateau_indices_start, plateau_indices_end, baseline_indices_start, baseline_indices_end = select_baseline_peak(values, 
                                                                                                                        baseline_sweep_n, 
                                                                                                                        peak_sweep_n, 
                                                                                                                        threshold, 
                                                                                                                        filter_percentile,
                                                                                                                        baseline_offset,
                                                                                                                        plateau_offset)
        experiment.loc[experiment['spot_names'] == spot_name, 'plateau_start_times'] = spot_data['Time'][plateau_indices_start]
        experiment.loc[experiment['spot_names'] == spot_name, 'plateau_end_times'] = spot_data['Time'][plateau_indices_end]
        experiment.loc[experiment['spot_names'] == spot_name, 'baseline_start_times'] = spot_data['Time'][baseline_indices_start]
        experiment.loc[experiment['spot_names'] == spot_name, 'baseline_end_times'] = spot_data['Time'][baseline_indices_end]

    return experiment

def subtract_baseline(experiment, channels, total_data):
    '''
    function for subtracting the baseline from the total data

    Parameters
    ----------
    experiment : pandas.DataFrame
        a pandas dataframe containing the experiment metadata
    channels : list
        a list of the channels in the data
    total_data : pandas.DataFrame
        a pandas dataframe containing the total data

    Returns
    -------
    baseline_corrected_total_data : pandas.DataFrame
        a pandas dataframe containing the baseline corrected data
    '''
    baseline_corrected_total_data = pd.DataFrame()
    for spot_name in total_data['spot_name'].unique():
        this_spot_data = total_data[total_data['spot_name'] == spot_name]
        # get the baseline start and end times for this spot
        baseline_start_time = experiment[experiment['spot_names'] == spot_name]['baseline_start_times'].values[0]
        baseline_end_time = experiment[experiment['spot_names'] == spot_name]['baseline_end_times'].values[0]
        this_spot_baseline = this_spot_data[(this_spot_data['Time'] >= baseline_start_time) & (this_spot_data['Time'] < baseline_end_time)]
        for channel in channels:
            this_spot_data.loc[:, channel] = this_spot_data[channel] - this_spot_baseline[channel].mean()
        baseline_corrected_total_data = pd.concat([baseline_corrected_total_data, this_spot_data])

    baseline_corrected_total_data.sort_values(by='Time', inplace=True, ignore_index=True)
    return baseline_corrected_total_data

# plotting
# -----------------------------------------------------------
def plot_selected_data(experiment, total_data, channel, color='black', line_width=0.5, width=1200, height=400):
    '''
    function for plotting original total data with baseline and plateau selections, allowing plotting only one channel at once
    '''
    overlay = plot_time_series(total_data, y=channel, color=color, line_width=line_width)
    baseline_data = pd.DataFrame()
    plateau_data = pd.DataFrame()
    # now add scatter plot of the baseline on top of the overlay
    for i in range(experiment.shape[0]):
        # get the baseline start and end times for this spot
        baseline_start_time = experiment['baseline_start_times'][i]
        baseline_end_time = experiment['baseline_end_times'][i]
        plateau_start_time = experiment['plateau_start_times'][i]
        plateau_end_time = experiment['plateau_end_times'][i]

        baseline_data = pd.concat([baseline_data, total_data[(total_data['Time'] >= baseline_start_time) & (total_data['Time'] < baseline_end_time)]])
        plateau_data = pd.concat([plateau_data, total_data[(total_data['Time'] >= plateau_start_time) & (total_data['Time'] < plateau_end_time)]])

    # add baseline plot
    overlay *= baseline_data.hvplot(x='Time', y=channel, kind='scatter', scale=0.5, color='red', label='baseline', hover_cols=['Time', channel, 'spot_name'])
    # ad plateau plot
    overlay *= plateau_data.hvplot(x='Time', y=channel, kind='scatter', scale=0.5, color='blue', label='plateau', hover_cols=['Time', channel, 'spot_name'])
    # Customize the plot
    overlay.opts(
        width=width,
        height=height,
        legend_position='top_right',
        title='Original data with selected baseline and plateau',
        xlabel='Time',
        ylabel='cps'
    )
    return overlay

def plot_time_series(dataframe, y=None, x='Time', color='black', kind='line', line_width=0.5, width=1200, height=400):
    """
    Plots time series data for multiple channels with individual curve coloring.

    Parameters:
        dataframe (pd.DataFrame): The DataFrame containing the data.
        y (str or list): The column name(s) of the channel(s) to plot.
        x (str): The column name for the x-axis (default is 'Time').
        color (str or list): The color(s) for the curves. If a list, it must match the length of `y`.
        line_width (float): The width of the lines (default is 0.5).
        width (int): The width of the plot (default is 1200).
        height (int): The height of the plot (default is 400).

    Returns:
        hv.Overlay: A Holoviews Overlay of the curves.
    """
    if y is None:
        raise ValueError('Channel name(s) must be specified')
    
    # If y is a single channel, convert it to a list
    if isinstance(y, str):
        y = [y]
    elif not isinstance(y, list):
        raise ValueError('Channel name(s) must be a string or a list of strings')
    
    # If color is a single value, convert it to a list of the same length as y
    if isinstance(color, str):
        color = [color] * len(y)
    elif isinstance(color, list) and len(color) != len(y):
        raise ValueError('Length of `color` list must match the number of channels in `y`')
    
    # Create an empty overlay
    overlay = hv.Overlay()
    
    # Plot each channel with its respective color
    for channel, col in zip(y, color):
        curve = dataframe.hvplot(x=x, y=channel, kind=kind, line_width=line_width, color=col, label=channel)
        overlay *= curve
    
    # Customize the plot
    overlay.opts(
        width=width,
        height=height,
        legend_position='top_right',
        title='Time Series Plot',
        xlabel='Time',
        ylabel='cps'
    )
    
    return overlay


# concentration calibration
# -----------------------------------------------------------

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

    plot = hv.Overlay()
    for i, channel in enumerate(effective_channels):
        this_channel_measured_cps = []
        this_channel_known_ppm = []
        this_channel_color = Category20[20][i % 20]
        for spot in NIST_data['spot_name'].unique():
            this_spot_data = NIST_data[NIST_data['spot_name'] == spot]
            this_spot_mean = this_spot_data[channel].mean()
            this_channel_measured_cps.append(this_spot_mean)
            this_spot_true_concentration = standard_archive.loc[this_spot_data['sample'].values[0], channel]
            this_channel_known_ppm.append(this_spot_true_concentration)
        # now calculate the calibration factor using the best-fit line anchored at the origin
        this_channel_measured_cps = np.array(this_channel_measured_cps)
        this_channel_known_ppm = np.array(this_channel_known_ppm)
        slope = np.dot(this_channel_measured_cps, this_channel_known_ppm) / np.dot(this_channel_measured_cps, this_channel_measured_cps)
        calibration_factors[channel] = slope
        # plot the data and the best-fit line
        plot *= hv.Scatter((this_channel_known_ppm, this_channel_measured_cps), label=channel).opts(color=this_channel_color, size=5, alpha=0.6)
        plot *= hv.Curve(((0, this_channel_measured_cps.max()*slope), (0, this_channel_measured_cps.max())), label=channel + ' calibration factor').opts(color=this_channel_color, line_width=2, show_legend=True)
        plot.opts(title='Calibration factor for concentration measurements', xlabel='True concentration (ppm)', ylabel='Measured cps', width=800, height=600)
        # set xlim and ylim
    # turn off legend
    plot.opts(show_legend=False)
    return calibration_factors, plot

def calc_concentration(unknown_standard_data, concentration_calibration, sep='_'):
    '''
    function for calculating the elemental concentrations in the unknown and standard samples
        after the construction of the calibration factors stored in the concentration calibration pandas series

    when calculating the unknonwns, aftern constructing the mean and standard deviation multiplied by a student's t multiplier with n-1 degrees of freedom
        we exclude the data that fall outside of the 95% confidence interval
    '''
    element_channels = concentration_calibration.index

    # strip the isotope mass number from the channel name as we already aligned the isotope signals to the total elemental concentration 
    elements = [re.sub(r'\d+', '', channel) for channel in element_channels]
    # unknown_standard_ppm_df = pd.DataFrame(columns=elements)
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

# ratio calibration
# -----------------------------------------------------------

def propagate_raw_ratio_uncertaintys(nom, denom):
    '''
    function for propagating the uncertainty of the raw ratios
        using Possion distribution relative uncertainty where
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
        this_spot_R82_2e = propagate_raw_ratio_uncertaintys(this_spot_plateau_data['208Pb'], this_spot_plateau_data['232Th'])

        # 206/238 mean of ratios
        this_spot_R68 = this_spot_plateau_data['206Pb'] / this_spot_plateau_data['238U']
        this_spot_R68_2e = propagate_raw_ratio_uncertaintys(this_spot_plateau_data['206Pb'], this_spot_plateau_data['238U'])

        # 207/206 mean of ratios
        this_spot_R76 = this_spot_plateau_data['207Pb'] / this_spot_plateau_data['206Pb']
        this_spot_R76_2e = propagate_raw_ratio_uncertaintys(this_spot_plateau_data['207Pb'], this_spot_plateau_data['206Pb'])

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

def calc_DH_nominal_fractionation(known_ratio, primary_standard_ratios, ratio_name):
    '''
    function for calculating nominal fractionation factor by comparing a given known ratio with the measured ratio
        use this function when the dealing with a single known ratio value

    '''
    primary_standard = primary_standard_ratios['sample'].unique()[0]
    # initialize a time variable and a ratio fractionation variable to store the interpolated time and fractionation for all primary standard spots
    all_times = [] # relative time within each spot, starts with zero
    all_ratios = [] # the fractionation of the measured ratio from the known standard ratio

    # now let's plot the measured sweep-wise fractionation for the primary standard
    plot = hv.Overlay()
    for i, spot_name in enumerate(primary_standard_ratios['spot_name'].unique()):
        this_spot = primary_standard_ratios[primary_standard_ratios['spot_name'] == spot_name]
        # these are already plateau data so we just need to adjust the time so that every spot starts at 0
        this_spot['relative_time'] = this_spot['Time'] - this_spot['Time'].iloc[0]

        # store the measured time and ratios for this spot
        all_times.append(this_spot['relative_time'].values)
        all_ratios.append(this_spot[ratio_name].values)
        # plot the fractionation
        plot *= hv.Curve((this_spot['relative_time'].values, known_ratio/this_spot[ratio_name].values),label=spot_name).opts(color=Category20[20][i % 20], line_width=1, alpha=0.6, show_legend=True)

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
    # construct the downhole fractionation function
    downhole_fractionation = interp1d(unified_time, mean_fractionation_factor, kind='linear', fill_value='extrapolate')

    # calculate the std of the mean_fractionation_factor based on the difference between the mean and the individual ratios
    all_times_array = np.array(all_times).flatten()
    all_fractionations_array = known_ratio/np.array(all_ratios).flatten()
    # calculate the standard deviation of the mean_fractionation_factor
    std_fractionation_factor = np.std([all_fractionations_array[i] - downhole_fractionation(all_times_array[i]) for i in range(len(all_times_array))])

    # plot the mean and std * student's t multiplier of the interpolated fractionation
    plot *= hv.Curve((unified_time, mean_fractionation_factor), label='mean fractionation factor').opts(color='black', line_width=2, line_dash='dashed', show_legend=False)
    plot *= hv.Curve((unified_time, mean_fractionation_factor + std_fractionation_factor), label='1 std').opts(color='grey', line_dash='dashed', line_width=1, show_legend=False)
    plot *= hv.Curve((unified_time, mean_fractionation_factor - std_fractionation_factor)).opts(color='grey', line_dash='dashed', line_width=1)
    # fill between the 95CI interval
    plot *= hv.Area((unified_time, mean_fractionation_factor - std_fractionation_factor, mean_fractionation_factor + std_fractionation_factor), vdims=['y', 'y2']).opts(color='grey', alpha=0.1)

    plot.opts(title=ratio_name + ' fractionation factor for primary standard '+ primary_standard, 
        xlabel='Time (s)', ylabel=ratio_name + '\nfractionation factor',
        fontsize={'xlabel':12, 'ylabel':12},
        legend_position='right', show_legend = False,
        width=800, height=400)
            
    return plot, downhole_fractionation

def calc_DH_fractionations_rad(primary_standard_ratios, standard_archive, fit='linear'):
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

    for ratio_name in ['206Pb/238U', '207Pb/206Pb', '208Pb/232Th']:
        known_PR_ratio = standard_archive.loc[primary_standard, ratio_name+'*'] # for highly radiogenic primary standards
        plot, downhole_fractionation = calc_DH_nominal_fractionation(known_PR_ratio, primary_standard_ratios, ratio_name)
        # append the results to the dictionary
        DH_correction_results.update({ratio_name: {'plot': plot, 'interpolation': downhole_fractionation}})
    return DH_correction_results

def calc_DH_fractionations_TW(primary_68_standard_ratios, primary_76_standard_ratios, standard_archive, fit='linear'):
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

    # store the plots and interpolations in a dictionary
    DH_correction_results = dict()

    # let's first get the fractionation factor for the 7/6 ratio
    known_PR_76_ratio = standard_archive.loc[primary_76_standard, '207Pb/206Pb*'] # for the radiogenic 76 ratio (usually use NIST glass standard)
    plot_76, downhole_fractionation_76 = calc_DH_nominal_fractionation(known_PR_76_ratio, primary_76_standard_ratios, '207Pb/206Pb')
    DH_correction_results.update({'207Pb/206Pb': {'plot': plot_76, 'interpolation': downhole_fractionation_76}})

    # then use this fractionation factor to correct the 7/6 ratio of the primary 68 standard
    primary_68_standard_ratios['207Pb/206Pb_DH_corrected'] = primary_68_standard_ratios['207Pb/206Pb'] * downhole_fractionation_76(primary_68_standard_ratios['Time'] - primary_68_standard_ratios['Time'].iloc[0])
    # horizontally project the corrected 7/6 ratio to get the TW intercept point
    PR_68_rad = standard_archive.loc[primary_68_standard, '238U/206Pb*'] # for the radiogenic 68 ratio
    PR_86_rad = 1/PR_68_rad # for the radiogenic 86 ratio
    PR_76_i = standard_archive.loc[primary_68_standard, '207Pb/206Pb_i'] # for the initial 76 ratio
    PR_76_rad = standard_archive.loc[primary_68_standard, '207Pb/206Pb*'] # for the radiogenic 76 ratio

    # construct the known TW line
    TW_slope = (PR_76_i - PR_76_rad) / (0 - PR_86_rad)
    TW_upper_intercept = PR_76_i
    # calculate the TW projections
    primary_68_standard_ratios['206Pb/238UTW_projected'] = 1 / ((primary_68_standard_ratios['207Pb/206Pb_DH_corrected'] - TW_upper_intercept) / TW_slope)
    # now calculate the fractionation factor for the 6/8 ratio
    plot_68, downhole_fractionation_68 = calc_dynamic_fractionation(primary_68_standard_ratios, '206Pb/238U', '206Pb/238UTW_projected')
    DH_correction_results.update({'206Pb/238U': {'plot': plot_68, 'interpolation': downhole_fractionation_68}})

    # add the 8/2 ratio correction
    known_PR_82_ratio = standard_archive.loc[primary_68_standard, '208Pb/232Th*'] # for the radiogenic 82 ratio
    plot_82, downhole_fractionation_82 = calc_DH_nominal_fractionation(known_PR_82_ratio, primary_68_standard_ratios, '208Pb/232Th')
    DH_correction_results.update({'208Pb/232Th': {'plot': plot_82, 'interpolation': downhole_fractionation_82}})
    return DH_correction_results


def calc_DH_dynamic_fractionation(primary_standard_ratios, measured_ratio_name, projected_ratio_name):
    '''
    function for calculating dynamic fractionation, this is used for cases where we need to do TW projection to get
        the TW 86 ratios for each sweep and then calculate the projected ratios against the measured ratios
    '''
    primary_standard = primary_standard_ratios['sample'].unique()[0]
    # initialize a time variable and a ratio fractionation variable to store the interpolated time and fractionation for all primary standard spots
    all_times = [] # relative time within each spot, starts with zero
    all_fractionations = [] # the fractionation of the measured ratio from the known standard ratio

    # now let's plot the measured sweep-wise fractionation for the primary standard
    plot = hv.Overlay()
    for i, spot_name in enumerate(primary_standard_ratios['spot_name'].unique()):
        this_spot = primary_standard_ratios[primary_standard_ratios['spot_name'] == spot_name]
        # these are already plateau data so we just need to adjust the time so that every spot starts at 0
        this_spot['relative_time'] = this_spot['Time'] - this_spot['Time'].iloc[0]
        this_spot[measured_ratio_name +'_fractionation_factor'] = this_spot[projected_ratio_name] / this_spot[measured_ratio_name]
        # store the measured time and fractionation factors for this spot
        all_times.append(this_spot['relative_time'].values)
        all_fractionations.append(this_spot[measured_ratio_name +'_fractionation_factor'].values)
        # plot the fractionation
        plot *= this_spot.hvplot(x='relative_time', y=measured_ratio_name +'_fractionation_factor', color=Category20[20][i % 20], size=10, alpha=0.6, kind='line', line_width=1)

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
    # calculate the std of the mean_fractionation_factor based on the difference between the mean and the individual ratios
    all_times_array = np.array(all_times).flatten()
    all_fractionations_array = np.array(all_fractionations).flatten()
    # calculate the standard deviation of the mean_fractionation_factor
    std_fractionation_factor = np.std([all_fractionations_array[i] - downhole_fractionation(all_times_array[i]) for i in range(len(all_times_array))])

    # plot the mean and std * student's t multiplier of the interpolated fractionation
    plot *= hv.Curve((unified_time, mean_fractionation_factor), label='mean fractionation factor').opts(color='black', line_width=2, line_dash='dashed', show_legend=True)
    plot *= hv.Curve((unified_time, mean_fractionation_factor + std_fractionation_factor), label='1 std').opts(color='grey', line_dash='dashed', line_width=1, show_legend=True)
    plot *= hv.Curve((unified_time, mean_fractionation_factor - std_fractionation_factor)).opts(color='grey', line_dash='dashed', line_width=1)
    # fill between the 95CI interval
    plot *= hv.Area((unified_time, mean_fractionation_factor - std_fractionation_factor, mean_fractionation_factor + std_fractionation_factor), vdims=['y', 'y2']).opts(color='grey', alpha=0.1)

    plot.opts(title=measured_ratio_name + ' fractionation factor for primary standard '+ primary_standard, 
        xlabel='Time (s)', ylabel=measured_ratio_name + '\nfractionation factor',
        fontsize={'xlabel':12, 'ylabel':12},
        legend_position='right', show_legend = True,
        width=800, height=400)
    
    downhole_fractionation = interp1d(unified_time, mean_fractionation_factor, kind='linear', fill_value='extrapolate')
        
    return plot, downhole_fractionation

def apply_DH_correction(plateau_data_with_raw_ratios, DH_correction_results):
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
        this_spot_data = plateau_data_with_raw_ratios[plateau_data_with_raw_ratios['spot_name'] == spot_name]
        for isotope_ratio in DH_correction_results.keys():
            this_spot_DH_correction = DH_correction_results[isotope_ratio]['interpolation']
            this_spot_data[isotope_ratio + '_DH_corrected'] = this_spot_data[isotope_ratio] / this_spot_DH_correction(this_spot_data['Time'] - this_spot_data['Time'].iloc[0])
        corrected_plateau_data = pd.concat([corrected_plateau_data, this_spot_data])

    return corrected_plateau_data

# def calc_TW_radiogenic_ratios(primary_standard_DH_corrected_ratios, standard_archive):
#     '''
#     function for calculating the TW projected 86 ratios
#         the assumption there is that the fractionation between 207Pb/206Pb is negligible (fractionation factor close to 1)
#         therefore on the TW diagram we can horizontally project the measured 76 ratio to get the intercept with the TW curvet the true 86 ratio

#     Parameters
#     ----------
#     primary_standard_DH_corrected_ratios : pandas.DataFrame
#         a pandas dataframe containing the primary standard DH corrected ratios
#     standard_archive : pandas.DataFrame
#         a pandas dataframe containing the standard archive data
#     '''
#     # get the initial 207Pb/206Pb ratio and radiogenic 76 and radiogenic (TW lower intercept) 86 ratio from the standard archive
#     PR_initial_76 = standard_archive.loc[primary_standard_DH_corrected_ratios['sample'].unique()[0], '207Pb/206Pb_i']
#     PR_radiogenic_76 = standard_archive.loc[primary_standard_DH_corrected_ratios['sample'].unique()[0], '207Pb/206Pb*']
#     PR_radiogenic_86= standard_archive.loc[primary_standard_DH_corrected_ratios['sample'].unique()[0], '238U/206Pb*']
#     # construct the known TW line
#     TW_slope = (PR_initial_76 - PR_radiogenic_76) / (0 - PR_radiogenic_86)
#     TW_upper_intercept = PR_initial_76

#     # calculate TW lower intercept 86 ratio
#     primary_standard_DH_corrected_ratios['238U/206Pb_TW'] = (primary_standard_DH_corrected_ratios['207Pb/206Pb_DH_corrected'] - TW_upper_intercept) / TW_slope
#     primary_standard_DH_corrected_ratios['206Pb/238U_TW'] = 1/primary_standard_DH_corrected_ratios['238U/206Pb_TW']

#     return primary_standard_DH_corrected_ratios 

def calc_session_nominal_fractionation(known_ratio, primary_standard_ratios, ratio_name, degree=2):
    '''
    function for calculating nominal fractionation factor through a given session by comparing a given known ratio with the measured ratio
        use this function when the dealing with a single known ratio value
        we use this function for radiogenic ratios
    '''
    primary_standard = primary_standard_ratios['sample'].unique()[0]
    spot_mean_times = [] # spot mean time
    spot_mean_fractionation = [] # spot mean fractionation
    # now let's plot the measured sweep-wise fractionation for the primary standard
    for spot_name in primary_standard_ratios['spot_name'].unique():
        this_spot = primary_standard_ratios[primary_standard_ratios['spot_name'] == spot_name]
        # calculate mean time for the spot
        this_spot_mean_time = this_spot['Time'].mean()
        this_spot_mean_ratio = this_spot[ratio_name+'_DH_corrected'].mean()
        this_spot_mean_fractionation = known_ratio / this_spot_mean_ratio

        # store the calculated mean time and fractionation factors
        spot_mean_times.append(this_spot_mean_time)
        spot_mean_fractionation.append(this_spot_mean_fractionation)

    # calculate a polynomial fit for the mean DH corrected ratios against time
    spot_mean_times = np.array(spot_mean_times)
    spot_mean_fractionation = np.array(spot_mean_fractionation)

    # fit a polynomial of user-specified to the mean DH corrected ratios, default is 2
    p = np.polyfit(spot_mean_times, spot_mean_fractionation, degree)

    # calculate the standard deviation of the polynomial fit
    p_std = np.std(spot_mean_fractionation - np.polyval(p, spot_mean_times))
    
    # calculate a fitted polynomial curve
    polynomial_values = np.polyval(p, np.linspace(spot_mean_times[0], spot_mean_times[-1], 100))

    # now plot up the mean values and associated 95CI as errorbars 
    plot = hv.Scatter((spot_mean_times, spot_mean_fractionation), label='mean fractionation factor').opts(color='black', line_width=2)

    # plot the polynomial fit
    plot *= hv.Curve((np.linspace(spot_mean_times[0], spot_mean_times[-1], 100), polynomial_values), label='polynomial fit of degree {}\nto the fractionation factor'.format(degree)).opts(color='red', line_width=2)
    # plot the 1 std deviation envelope of the polynomial fit
    plot *= hv.Curve((np.linspace(spot_mean_times[0], spot_mean_times[-1], 100), polynomial_values + p_std), label='1 std deviation envelope').opts(color='grey', line_dash='dashed', line_width=1)
    plot *= hv.Curve((np.linspace(spot_mean_times[0], spot_mean_times[-1], 100), polynomial_values - p_std)).opts(color='grey', line_dash='dashed', line_width=1)
    
    plot.opts(title=ratio_name + ' fractionation correction factor for primary standard '+ primary_standard, 
        xlabel='Time (s)', ylabel=ratio_name + '\nfractionation factor', 
        fontsize={'xlabel':12, 'ylabel':12},
        legend_position='top_right', 
        width=800, height=400)
    
    return plot, p, p_std

def calc_session_fractionation_rad(primary_standard_DH_corrected_ratios, standard_archive, degree=2):
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
    plot : hv.Overlay
        a holoviews overlay plot containing the mean DH corrected ratios for the primary standards, the polynomial fit
    p : numpy.ndarray
        the coefficients of the polynomial fit
    '''
    primary_standard = primary_standard_DH_corrected_ratios['sample'].unique()[0]
    session_correction_results = dict()

    for ratio_name in ['206Pb/238U', '207Pb/206Pb', '208Pb/232Th']:
        known_PR_ratio = standard_archive.loc[primary_standard, ratio_name+'*'] # at this stage we only compare radiogenic vs. radiogenic
        plot, p, p_std = calc_session_nominal_fractionation(known_PR_ratio, primary_standard_DH_corrected_ratios, ratio_name, degree)
        # append the results to the dictionary
        session_correction_results.update({ratio_name: {'plot': plot, 'fit': p, 'std': p_std}})

    return session_correction_results

def calc_session_fractionation_TW(primary_68_DH_ratios, primary_76_DH_ratios, standard_archive, degree=2):
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
    plot : hv.Overlay
        a holoviews overlay plot containing the mean DH corrected ratios for the primary standards, the polynomial fit
    p : numpy.ndarray
        the coefficients of the polynomial fit
    '''
    
    # first get the known standard ratios from the standard archive
    primary_68_standard = primary_68_DH_ratios['sample'].unique()[0]
    primary_76_standard = primary_76_DH_ratios['sample'].unique()[0]

    # store the plots and interpolations in a dictionary
    session_correction_results = dict()

    # let's first get the fractionation factor for the 7/6 ratio
    known_PR_76_ratio = standard_archive.loc[primary_76_standard, '207Pb/206Pb*'] # for the radiogenic 76 ratio (usually use NIST glass standard)
    plot_76, p_76, p_std_76 = calc_session_nominal_fractionation(known_PR_76_ratio, primary_76_DH_ratios, '207Pb/206Pb', degree)

    session_correction_results.update({'207Pb/206Pb': {'plot': plot_76, 'fit': p_76, 'std': p_std_76}})

    # then use this fractionation factor to correct the 7/6 ratio of the primary 68 standard through the session
    primary_68_DH_ratios['207Pb/206Pb_session_corrected'] = primary_68_DH_ratios['207Pb/206Pb_DH_corrected'] * np.polyval(p_76, primary_68_DH_ratios['Time'])
    
    # horizontally project the corrected 7/6 ratio to get the TW intercept point
    PR_68_rad = standard_archive.loc[primary_68_standard, '238U/206Pb*'] # for the radiogenic 68 ratio
    PR_86_rad = 1/PR_68_rad # for the radiogenic 86 ratio
    PR_76_i = standard_archive.loc[primary_68_standard, '207Pb/206Pb_i'] # for the initial 76 ratio
    PR_76_rad = standard_archive.loc[primary_68_standard, '207Pb/206Pb*'] # for the radiogenic 76 ratio

    # construct the known TW line
    TW_slope = (PR_76_i - PR_76_rad) / (0 - PR_86_rad)
    TW_upper_intercept = PR_76_i

    spot_mean_times = [] # spot mean time
    spot_mean_68_fractionation = [] # spot mean fractionation
    for spot_name in primary_68_DH_ratios['spot_name'].unique():
        this_spot = primary_68_DH_ratios[primary_68_DH_ratios['spot_name'] == spot_name]
        # calculate mean time for the spot
        this_spot_mean_time = this_spot['Time'].mean()
        this_spot_mean_68_ratio = this_spot['206Pb/238U_DH_corrected'].mean()
        this_spot_68_TW_projected = 1 / (this_spot_mean_68_ratio * (this_spot['207Pb/206Pb_session_corrected'].mean() - TW_upper_intercept) / TW_slope)
        this_spot_mean_68_fractionation = this_spot_68_TW_projected/this_spot_mean_68_ratio
        # store the calculated mean time and fractionation factors
        spot_mean_times.append(this_spot_mean_time)
        spot_mean_68_fractionation.append(this_spot_mean_68_fractionation)
    # calculate a polynomial fit for the mean DH corrected ratios against time
    spot_mean_times = np.array(spot_mean_times)
    spot_mean_68_fractionation = np.array(spot_mean_68_fractionation)
    # fit a polynomial of user-specified to the mean DH corrected ratios, default is 2
    p_68 = np.polyfit(spot_mean_times, spot_mean_68_fractionation, degree)
    # calculate the standard deviation of the polynomial fit
    p_std_68 = np.std(spot_mean_68_fractionation - np.polyval(p_68, spot_mean_times))
    # calculate a fitted polynomial curve
    polynomial_values_68 = np.polyval(p_68, np.linspace(spot_mean_times[0], spot_mean_times[-1], 100))
    # now plot up the mean values and associated 95CI as errorbars
    plot_68 = hv.Scatter((spot_mean_times, spot_mean_68_fractionation), label='mean fractionation factor').opts(color='black', line_width=2)
    # plot the polynomial fit
    plot_68 *= hv.Curve((np.linspace(spot_mean_times[0], spot_mean_times[-1], 100), polynomial_values_68), label='polynomial fit of degree {}\nto the fractionation factor'.format(degree)).opts(color='red', line_width=2)
    # plot the 1 std deviation envelope of the polynomial fit
    plot_68 *= hv.Curve((np.linspace(spot_mean_times[0], spot_mean_times[-1], 100), polynomial_values_68 + p_std_68), label='1 std deviation envelope').opts(color='grey', line_dash='dashed', line_width=1)
    plot_68 *= hv.Curve((np.linspace(spot_mean_times[0], spot_mean_times[-1], 100), polynomial_values_68 - p_std_68)).opts(color='grey', line_dash='dashed', line_width=1)
        
    plot_68.opts(title='206Pb/238U fractionation correction factor for primary standard '+ primary_68_standard,
        xlabel='Time (s)', ylabel='206Pb/238U\nfractionation factor', 
        fontsize={'xlabel':12, 'ylabel':12},
        legend_position='top_right', 
        width=800, height=400)
    
    session_correction_results.update({'206Pb/238U': {'plot': plot_68, 'fit': p_68, 'std': p_std_68}})

    # add the 8/2 ratio correction
    known_PR_82_ratio = standard_archive.loc[primary_68_standard, '208Pb/232Th*'] # for the radiogenic 82 ratio
    plot_82, p_82, p_std_82 = calc_session_nominal_fractionation(known_PR_82_ratio, primary_68_DH_ratios, '208Pb/232Th', degree)
    session_correction_results.update({'208Pb/232Th': {'plot': plot_82, 'fit': p_82, 'std': p_std_82}})

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
        this_spot_data = DH_corrected_ratios[DH_corrected_ratios['spot_name'] == spot_name]
        for isotope_ratio in session_correction_results.keys():
            this_spot_fractionation_correction = session_correction_results[isotope_ratio]['fit']
            this_spot_data[isotope_ratio + '_fractionation_corrected'] = this_spot_data[isotope_ratio + '_DH_corrected'] * np.polyval(this_spot_fractionation_correction, this_spot_data['Time'])
        fractionation_corrected_ratios = pd.concat([fractionation_corrected_ratios, this_spot_data])

    return fractionation_corrected_ratios

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
            this_spot_measured_ratio_mean = this_spot_data[isotope_ratio + '_DH_corrected'].mean()
            this_spot_measured_ratio_std = this_spot_data[isotope_ratio + '_DH_corrected'].std()

            # get the session-wide fractionation correction mean using the mean spot time and the polynomial fit
            this_spot_this_ratio_fractionation_fit = session_correction_results[isotope_ratio]['fit']
            this_spot_this_ratio_fractionation_mean = np.polyval(this_spot_this_ratio_fractionation_fit, this_spot_data['Time'].mean())
            # get the session-wide fractionation correction std
            this_ratio_fractionation_std = session_correction_results[isotope_ratio]['std']
            # later will add mean and std of the blank correction

            # get the fractionation corrected mean
            this_spot_fractionation_correction_mean = this_spot_data[isotope_ratio + '_fractionation_corrected'].mean()
            # calculate the propagated uncertainty
            this_spot_this_ratio_propagated_uncertainty = this_spot_fractionation_correction_mean * np.sqrt((this_spot_measured_ratio_std/this_spot_measured_ratio_mean)**2 + (this_ratio_fractionation_std/this_spot_this_ratio_fractionation_mean)**2)
            # print('contributions of error from fractionation correction:', this_ratio_fractionation_std/this_spot_this_ratio_fractionation_mean*100)
            # save results
            final_ratios.loc[spot_name, isotope_ratio] = this_spot_fractionation_correction_mean
            final_ratios.loc[spot_name, isotope_ratio + '_2e%'] = this_spot_this_ratio_propagated_uncertainty * 2 / np.sqrt(this_spot_data.shape[0]) / this_spot_fractionation_correction_mean * 100
            final_ratios.loc[spot_name, isotope_ratio + '_2e'] = this_spot_this_ratio_propagated_uncertainty * 2 / np.sqrt(this_spot_data.shape[0])
        # calculate the 207Pb/235U ratio and error
        final_ratios.loc[spot_name, '207Pb/235U'] = final_ratios.loc[spot_name,'207Pb/206Pb'] * final_ratios.loc[spot_name,'206Pb/238U'] * common_U238_U235
        final_ratios.loc[spot_name, '207Pb/235U_2e%'] = np.sqrt((final_ratios.loc[spot_name,'207Pb/206Pb_2e%']**2 + final_ratios.loc[spot_name,'206Pb/238U_2e%']**2))
        final_ratios.loc[spot_name, '207Pb/235U_2e'] = final_ratios.loc[spot_name, '207Pb/235U'] * final_ratios.loc[spot_name, '207Pb/235U_2e%'] / 100
        
        # calculate the 238U/206Pb ratio and error where the 238U/206Pb ratio is the inverse of the 206Pb/238U ratio
        final_ratios.loc[spot_name, '238U/206Pb'] = 1 / final_ratios.loc[spot_name, '206Pb/238U']
        final_ratios.loc[spot_name, '238U/206Pb_2e%'] = final_ratios.loc[spot_name,'206Pb/238U_2e%'] * np.sqrt(1/final_ratios.loc[spot_name,'206Pb/238U'])
        final_ratios.loc[spot_name, '238U/206Pb_2e'] = final_ratios.loc[spot_name, '238U/206Pb'] * final_ratios.loc[spot_name, '238U/206Pb_2e%'] / 100

        # calculate error correlations for the 207Pb/235U-206Pb/238U ratios
        x = final_ratios.loc[spot_name,'207Pb/206Pb'] # 76 ratio
        u = final_ratios.loc[spot_name,'207Pb/235U']  # 75 ratio
        v = final_ratios.loc[spot_name,'206Pb/238U']  # 68 ratio

        sx = final_ratios.loc[spot_name,'207Pb/206Pb_2e']/2 # 76 std
        su = final_ratios.loc[spot_name,'207Pb/235U_2e']/2 # 75 std
        sv = final_ratios.loc[spot_name,'206Pb/238U_2e']/2 # 68 std

        this_75_68_err_corr = calc_err_corr_quotient(x, sx, u, su, v, sv)

        final_ratios.loc[spot_name, 'err_corr'] = this_75_68_err_corr

    final_ratios['sample'] = final_ratios.index.str.split(sep).str[0]
    final_ratios['spot_name'] = final_ratios.index
    # now let's reorganize the columns
    final_ratios_columns = final_ratios.columns.tolist()
    first_columns = ['spot_name', 'sample', 
                     '207Pb/235U', '207Pb/235U_2e%', 
                     '206Pb/238U', '206Pb/238U_2e%', 'err_corr', 
                     '238U/206Pb', '238U/206Pb_2e%',
                     '207Pb/206Pb', '207Pb/206Pb_2e%',
                     '208Pb/232Th', '208Pb/232Th_2e%',
                     '207Pb/235U_2e', 
                     '206Pb/238U_2e',
                     '238U/206Pb_2e',  
                     '207Pb/206Pb_2e',
                     '208Pb/232Th_2e']
    other_columns = [i for i in final_ratios_columns if i not in first_columns]
    final_ratios = final_ratios[first_columns + other_columns]
    final_ratios = final_ratios.sort_values(by='spot_name').reset_index(drop=True)
    return final_ratios
    
def calc_TW_lower_intercept(initial_76, total_76, total_86, x0 = 5, tol=1e-6):
    '''
    function for calculating the Tera-Wasserburg lower intercept value 
        based on a given initial 207Pb/206Pb ratio, measured total 207Pb/206Pb ratio
        and measured total 238U/206Pb ratio
        the resultant lower concordia intercept is the radiogenic ratio result

    here we assume we are solving the intercept between a line of y = ax + b and the TW concordia curve
    '''
    max_n = 100
    n = 0

    initial_86 = 0

    a = (initial_76 - total_76)/ (initial_86 - total_86) # a, slope
    b = initial_76 # b, intercept 
    # print(a, b)
    initial_x = x0
    final_x = 0
    while abs(final_x - initial_x) > tol and n <= max_n:
        n += 1
        final_x = initial_x
        # print(final_x)
        initial_x =  ((((1/initial_x + 1)**(lambda_235/lambda_238) - 1) / common_U238_U235) - b) /a
    if n > max_n:
        print('maximum iterations reached', 'total_86: ', total_86)
        return None
    else:
        return final_x # final 86 ratio
    

