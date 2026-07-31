"""Trace-element concentration calibration and calculation."""

from __future__ import annotations

import re

import numpy as np
import pandas as pd
from scipy.stats import t


_ISOTOPE_CHANNEL_PATTERN = re.compile(r"^(?:\d+)?([A-Z][a-z]?)$")


def element_from_channel(channel):
    """Return the element symbol represented by an isotope channel label.

    Both ``29Si`` and ``Si`` resolve to ``Si``. Ratio names and other derived
    quantities return ``None`` so they cannot accidentally enter elemental
    concentration calibration.
    """
    match = _ISOTOPE_CHANNEL_PATTERN.fullmatch(str(channel).strip())
    return match.group(1) if match else None


def resolve_element_concentration(standard_archive, standard, channel):
    """Resolve a total elemental concentration independently of isotope mass.

    An exact channel is preferred. Otherwise, any finite archive column for
    the same element may supply the value because reference concentrations are
    total elemental mass fractions rather than isotope-specific abundances.
    Multiple non-identical candidates are rejected as an ambiguous archive.

    Returns
    -------
    value : float
        Total elemental concentration in micrograms per gram.
    archive_channel : str
        Archive quantity from which the value was obtained.
    """
    if standard not in standard_archive.index:
        raise KeyError(f"Unknown concentration standard {standard!r}.")
    element = element_from_channel(channel)
    if element is None:
        raise KeyError(f"{channel!r} is not an elemental isotope channel.")

    row = standard_archive.loc[standard]
    exact = row.get(channel, np.nan)
    if pd.notna(exact):
        return float(exact), str(channel)

    candidates = []
    for quantity in standard_archive.columns:
        if element_from_channel(quantity) != element:
            continue
        value = row.get(quantity, np.nan)
        if pd.notna(value):
            candidates.append((str(quantity), float(value)))
    if not candidates:
        raise KeyError(
            f"Standard {standard!r} has no concentration for element {element}."
        )

    values = np.asarray([value for _, value in candidates], dtype=float)
    if not np.allclose(values, values[0], rtol=1e-12, atol=0):
        labels = ", ".join(f"{name}={value:g}" for name, value in candidates)
        raise ValueError(
            f"Standard {standard!r} has conflicting {element} concentrations: "
            f"{labels}."
        )
    return float(values[0]), candidates[0][0]


def channel_for_element(channels, preferred_channel):
    """Choose the measured channel for an internal-normalization element."""
    channels = list(channels)
    if preferred_channel in channels:
        return preferred_channel
    element = element_from_channel(preferred_channel)
    matches = [
        channel for channel in channels if element_from_channel(channel) == element
    ]
    if not matches:
        raise KeyError(
            f"No measured {element} channel is available for internal normalization."
        )
    return matches[0]


def internal_normalization(
    experiment,
    channels,
    baseline_subtracted_total_data,
    standard_archive,
    material='zircon',
    *,
    internal_channel=None,
):
    """Select plateau rows and apply sweep-wise internal normalization.

    Each spot is grouped once, normalized as a vector across all requested
    channels, and appended to a list for a single final concatenation.
    """
    if material == 'zircon':
        preferred_internal_channel = '29Si'
        unknown_concentration = 151443
    elif material == 'apatite':
        preferred_internal_channel = '44Ca'
        unknown_concentration = 400443
    elif material == 'rutile':
        preferred_internal_channel = '49Ti'
        unknown_concentration = 599499
    elif material == 'titanite':
        preferred_internal_channel = '29Si'
        unknown_concentration = 151443
    else:
        raise ValueError(
            "material must be 'zircon', 'apatite', 'rutile', or 'titanite'"
        )
    if internal_channel is not None:
        preferred_internal_channel = internal_channel
    internal_analyte = channel_for_element(
        channels,
        preferred_internal_channel,
    )

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
            internal_analyte_concentration, _ = resolve_element_concentration(
                this_standard_archive,
                sample_name,
                internal_analyte,
            )
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


def calc_concentration_calibration_factor(
    NIST_data,
    channels,
    standard_archive,
    *,
    internal_channel='29Si',
    build_plot=True,
):
    """Calculate total-element calibration factors from glass standards.

    Measured isotope channels are matched to reference concentrations by
    element symbol, so a ``151Eu`` sweep can use a concentration documented as
    ``153Eu`` without duplicating the value in the reference library. If two
    masses of the same element were acquired, a measured mass explicitly
    documented by every calibration standard is preferred; otherwise the
    first resolvable measured mass is used. The element is reported only once.

    ``internal_channel`` identifies the element fixed by internal
    normalization and excludes it from external concentration calibration.
    Set ``build_plot=False`` in a frontend that draws its own diagnostics to
    avoid importing and constructing the optional Bokeh figure.
    """
    calibration_factors = pd.Series(dtype=float)
    sample_names = NIST_data['sample'].dropna().astype(str).unique().tolist()
    internal_element = element_from_channel(internal_channel)
    effective_channels = []
    reference_channels = {}
    channels_by_element = {}
    for channel in channels:
        element = element_from_channel(channel)
        if element is None or element == internal_element:
            continue
        channels_by_element.setdefault(element, []).append(channel)

    for element_channels in channels_by_element.values():
        # Prefer a measured mass that is also explicitly documented for every
        # calibration standard. This preserves established channels such as
        # 208Pb when a method also acquires 206Pb and 207Pb. If no exact mass
        # exists, fall back to the first measured mass that can be resolved by
        # element (for example measured 151Eu using archived 153Eu).
        exact_channels = [
            channel
            for channel in element_channels
            if channel in standard_archive.columns
            and all(
                pd.notna(standard_archive.loc[sample, channel])
                for sample in sample_names
            )
        ]
        candidates = [*exact_channels, *element_channels]
        for channel in dict.fromkeys(candidates):
            resolved = {}
            try:
                for sample in sample_names:
                    _, archive_channel = resolve_element_concentration(
                        standard_archive,
                        sample,
                        channel,
                    )
                    resolved[sample] = archive_channel
            except KeyError:
                continue
            effective_channels.append(channel)
            reference_channels[channel] = resolved
            break
    print(
        'Analytes that can be calibrated for concentration measurements are: ',
        effective_channels,
    )

    # Calculate every per-spot channel mean in one grouped operation instead of
    # rescanning the full NIST table for each channel and spot combination.
    grouped = NIST_data.groupby('spot_name', sort=False)
    spot_means = grouped[effective_channels].mean()
    spot_samples = grouped['sample'].first().reindex(spot_means.index)

    plot = None
    if build_plot:
        from bokeh.models import ColumnDataSource, HoverTool
        from bokeh.palettes import Category20
        from bokeh.plotting import figure

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
            [
                resolve_element_concentration(
                    standard_archive,
                    sample,
                    channel,
                )[0]
                for sample in sample_list
            ],
            dtype=float,
        )
        # now calculate the calibration factor using the best-fit line anchored at the origin
        slope = np.dot(
            this_channel_measured_cps, this_channel_known_ppm
        ) / np.dot(this_channel_measured_cps, this_channel_measured_cps)
        calibration_factors[channel] = slope
        if plot is not None:
            this_channel_color = Category20[20][i % 20]
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
            max_ppm = (
                float(np.nanmax(this_channel_known_ppm))
                if this_channel_known_ppm.size
                else 0.0
            )
            plot.line(
                [0, max_ppm],
                [0, max_ppm / slope if slope else 0],
                color=this_channel_color,
                line_width=2,
            )
    if plot is not None and plot.legend:
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
    calibration_factors.attrs['reference_channels'] = reference_channels
    calibration_factors.attrs['internal_channel'] = internal_channel
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
    'channel_for_element',
    'element_from_channel',
    'resolve_element_concentration',
    'internal_normalization',
    'filter_concentration_outliers',
    'calc_concentration_calibration_factor',
    'calc_concentration',
    'calc_concentration_depth_profile',
]
