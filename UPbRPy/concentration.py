"""Trace-element concentration calibration and calculation."""

from __future__ import annotations

import re

from bokeh.models import ColumnDataSource, HoverTool
from bokeh.plotting import figure
import numpy as np
import pandas as pd
from bokeh.palettes import Category20
from scipy.stats import t


def internal_normalization(
    experiment,
    channels,
    baseline_subtracted_total_data,
    standard_archive,
    material='zircon',
):
    """Select plateau rows and apply sweep-wise internal normalization.

    Each spot is grouped once, normalized as a vector across all requested
    channels, and appended to a list for a single final concatenation.
    """
    if material == 'zircon':
        internal_analyte = '29Si'
        unknown_concentration = 151443
    elif material == 'apatite':
        internal_analyte = '44Ca'
        unknown_concentration = 400443
    elif material == 'rutile':
        internal_analyte = '49Ti'
        unknown_concentration = 599499
    else:
        raise ValueError("material must be 'zircon', 'apatite', or 'rutile'")

    this_standard_archive = standard_archive[
        standard_archive['material'].isin([material, 'glass'])
    ]
    selection_by_spot = (
        experiment.drop_duplicates('spot_names', keep='first')
        .set_index('spot_names')[['plateau_start_times', 'plateau_end_times']]
    )
    normalized_frames = []
    channel_names = list(channels)
    for spot_name, spot_frame in baseline_subtracted_total_data.groupby(
        'spot_name', sort=False
    ):
        sample_name = spot_frame['sample'].iloc[0]
        if sample_name in this_standard_archive.index:
            internal_analyte_concentration = this_standard_archive.loc[
                sample_name, internal_analyte
            ]
        else:
            internal_analyte_concentration = unknown_concentration

        plateau_start, plateau_end = selection_by_spot.loc[spot_name]
        plateau_mask = (
            (spot_frame['Time'] >= plateau_start)
            & (spot_frame['Time'] < plateau_end)
        )
        plateau = spot_frame.loc[plateau_mask].copy()
        plateau['internal_normalization_factor'] = (
            internal_analyte_concentration / plateau[internal_analyte]
        )
        plateau.loc[:, channel_names] = plateau[channel_names].mul(
            plateau['internal_normalization_factor'], axis='index'
        )
        normalized_frames.append(plateau)

    if not normalized_frames:
        return baseline_subtracted_total_data.iloc[0:0].copy()
    normalized = pd.concat(normalized_frames, ignore_index=True)
    return normalized.sort_values(by='Time', ignore_index=True)


def filter_concentration_outliers(NIST_data, primary_channel, n=5):
    """Iteratively remove within-spot outliers from glass-standard data.

    On each pass, rows outside the sample mean plus or minus the Student-t
    multiplier times the sample standard deviation are removed. Spots are
    grouped once and the retained frames are concatenated only once.

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
    """
    filtered_frames = []
    for _, this_spot_data in NIST_data.groupby('spot_name', sort=False):
        for _ in range(n):
            this_spot_channel = this_spot_data[primary_channel]
            t_multiplier = t.ppf(0.975, len(this_spot_channel) - 1)
            interval = this_spot_channel.std() * t_multiplier
            upper_bound = this_spot_channel.mean() + interval
            lower_bound = this_spot_channel.mean() - interval
            this_spot_data = this_spot_data[
                (this_spot_data[primary_channel] < upper_bound)
                & (this_spot_data[primary_channel] > lower_bound)
            ]
        filtered_frames.append(this_spot_data)
    if not filtered_frames:
        return NIST_data.iloc[0:0].copy()
    return pd.concat(filtered_frames, ignore_index=True)


def calc_concentration_calibration_factor(NIST_data, channels, standard_archive):
    '''
    function for calculating the calibration factors for the elemental concentrations
        based on the NIST glass standards (610, 612)
    the standard archive stores all the true ppm values independently measured for the glass and standards
    in the NIST_data dataframe we can extract the mean values for the cps for each NIST measurement and calculate a best-fit origin-anchored line
        whose slope is the calibration factor for cps-ppm conversion
    '''
    calibration_factors = pd.Series(dtype=float)
    effective_channels = [
        channel
        for channel in channels
        if channel in standard_archive.columns and channel != '29Si'
    ]
    print(
        'Analytes that can be calibrated for concentration measurements are: ',
        effective_channels,
    )

    # Calculate every per-spot channel mean in one grouped operation instead of
    # rescanning the full NIST table for each channel and spot combination.
    grouped = NIST_data.groupby('spot_name', sort=False)
    spot_means = grouped[effective_channels].mean()
    spot_samples = grouped['sample'].first().reindex(spot_means.index)

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
        this_channel_measured_cps = spot_means[channel].to_numpy(dtype=float)
        sample_list = spot_samples.astype(str).tolist()
        spot_list = spot_means.index.astype(str).tolist()
        this_channel_known_ppm = np.asarray(
            [standard_archive.loc[sample, channel] for sample in sample_list],
            dtype=float,
        )
        this_channel_color = Category20[20][i % 20]

        # now calculate the calibration factor using the best-fit line anchored at the origin
        slope = np.dot(
            this_channel_measured_cps, this_channel_known_ppm
        ) / np.dot(this_channel_measured_cps, this_channel_measured_cps)
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
            tooltips=[
                ('channel', channel),
                ('sample', '@sample'),
                ('spot', '@spot_name'),
                ('ppm', '@ppm'),
                ('cps', '@cps'),
            ],
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
    """Calculate filtered spot-mean concentrations and uncertainty intervals.

    The existing Student-t filtering calculation is preserved, but spot groups
    and output records are constructed only once. This avoids thousands of
    repeated full-table filters and scalar DataFrame insertions in sessions with
    many analytes.
    """
    element_channels = concentration_calibration.index

    # Strip isotope mass numbers because each selected isotope signal has
    # already been scaled to represent the total elemental concentration.
    elements = [re.sub(r'\d+', '', channel) for channel in element_channels]
    records = []
    for spot_name, this_spot_data in unknown_standard_data.groupby(
        'spot_name', sort=False
    ):
        record = {
            'spot_name': spot_name,
            'sample': str(spot_name).split(sep)[0],
        }
        spot_t_multiplier = t.ppf(0.975, this_spot_data.shape[0] - 1)
        for channel, element in zip(element_channels, elements):
            unknown_standard_cps_mean = this_spot_data[channel].mean()
            unknown_standard_cps_std = this_spot_data[channel].std()
            lower_bound = (
                unknown_standard_cps_mean
                - spot_t_multiplier * unknown_standard_cps_std
            )
            upper_bound = (
                unknown_standard_cps_mean
                + spot_t_multiplier * unknown_standard_cps_std
            )

            # Retain values within the spot's 95% Student-t interval.
            this_spot_data_filtered = this_spot_data[
                (this_spot_data[channel] >= lower_bound)
                & (this_spot_data[channel] <= upper_bound)
            ]
            filtered_cps_mean = this_spot_data_filtered[channel].mean()
            filtered_cps_95CI = this_spot_data_filtered[channel].std() * t.ppf(
                0.975, this_spot_data_filtered.shape[0] - 1
            )
            calibration_factor = concentration_calibration[channel]
            record[element] = filtered_cps_mean * calibration_factor
            record[element + '_95CI'] = filtered_cps_95CI * calibration_factor
        records.append(record)

    if not records:
        return pd.DataFrame(columns=['spot_name', 'sample'])

    result = pd.DataFrame.from_records(records)
    result = result.set_index('spot_name', drop=False)
    result.index.name = None
    return result

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
