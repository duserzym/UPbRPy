'''
Python file with utility functions 
'''
import os
import pandas as pd
from datetime import datetime, timedelta
import numpy as np

# function for reading LA-ICP-MS data
# in the format of .csv files exported from Thermo iCap with each file representing a spot
def read_experiment_data(directory):
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
        total_data = pd.concat([total_data, this_data], axis=0)

    experiment = experiment.sort_values(by='spot_names').reset_index(drop=True)
    #initialize the columns for the experiment dataframe with null values

    experiment['plateau_start_times'] = None
    experiment['plateau_end_times'] = None
    experiment['baseline_start_times'] = None
    experiment['baseline_end_times'] = None

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


def select_baseline_peak(data, baseline_z_threshold=1.0, plateau_z_threshold=0.5):
    '''
    general function for selecting the baseline and peak regions in the data

    by default, we skip the first and last point for both the baseline and peak regions

    Parameters
    ----------
    data : a list or array of values 
    
    Returns
    -------
    baseline : list
        a list containing the indices of the baseline region
    peak : list
        a list containing the indices of the peak region
    '''
    z_scores = (data - np.mean(data)) / np.std(data)

    # Identify the start and end indices of the plateau region
    plateau_indices_start = np.where(z_scores >= plateau_z_threshold)[0][1]
    plateau_indices_end = np.where(z_scores >= plateau_z_threshold)[0][-2]

    # Identify the start and end indices of the baseline region
    baseline_indices = np.where(np.abs(z_scores) < baseline_z_threshold)[0]
    baseline_indices = baseline_indices[baseline_indices < plateau_indices_start]
    baseline_indices_start = baseline_indices[1]
    baseline_indices_end = baseline_indices[-2]

    return plateau_indices_start, plateau_indices_end, baseline_indices_start, baseline_indices_end


def auto_select_baseline_peak(experiment, total_data, primary_channel, baseline_z_threshold=1.0, plateau_z_threshold=0.5):
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
        values = spot_data[primary_channel].tolist()
        plateau_indices_start, plateau_indices_end, baseline_indices_start, baseline_indices_end = select_baseline_peak(values, baseline_z_threshold, plateau_z_threshold)
        experiment.loc[experiment['spot_names'] == spot_name, 'plateau_start_times'] = spot_data['Time'][plateau_indices_start]
        experiment.loc[experiment['spot_names'] == spot_name, 'plateau_end_times'] = spot_data['Time'][plateau_indices_end]
        experiment.loc[experiment['spot_names'] == spot_name, 'baseline_start_times'] = spot_data['Time'][baseline_indices_start]
        experiment.loc[experiment['spot_names'] == spot_name, 'baseline_end_times'] = spot_data['Time'][baseline_indices_end]


    return experiment