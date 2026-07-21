"""Downhole and session fractionation correction workflows."""

from __future__ import annotations

from bokeh.models import ColumnDataSource, HoverTool
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


def robust_downhole_inlier_mask(
    aligned_values,
    sigma,
    *,
    minimum_contributors=4,
    iterations=3,
):
    """Clip anomalous contributors independently at each aligned depth.

    The median absolute deviation is converted to a Gaussian-equivalent sigma.
    Comparing repeated standards at the same signal-relative time preserves a
    real downhole trend, whereas filtering one trace along time could mistake
    that trend for an anomaly.
    """
    values = np.asarray(aligned_values, dtype=float)
    if values.ndim != 2:
        raise ValueError("aligned_values must be a two-dimensional array.")
    finite = np.isfinite(values)
    if sigma is None:
        return finite
    sigma = float(sigma)
    if not np.isfinite(sigma) or sigma <= 0:
        raise ValueError("outlier_sigma must be positive or None.")

    inliers = finite.copy()
    minimum_contributors = max(int(minimum_contributors), 3)
    for column in range(values.shape[1]):
        active = np.flatnonzero(inliers[:, column])
        if active.size < minimum_contributors:
            continue
        for _ in range(max(int(iterations), 1)):
            selected = values[active, column]
            center = float(np.median(selected))
            deviations = np.abs(selected - center)
            robust_scale = 1.4826 * float(np.median(deviations))
            tolerance = np.finfo(float).eps * max(abs(center), 1.0) * 16
            if not np.isfinite(robust_scale) or robust_scale <= tolerance:
                # A rounded majority may have zero MAD. In that special case,
                # retain the coincident majority and reject separated values
                # only when at least three contributors remain.
                keep = deviations <= tolerance
            else:
                keep = deviations <= sigma * robust_scale
            if keep.all() or np.count_nonzero(keep) < 3:
                break
            active = active[keep]
        inliers[:, column] = False
        inliers[active, column] = True
    return inliers


def downhole_outlier_metadata(
    unified_time,
    aligned_values,
    inlier_mask,
    spot_names,
    sigma,
):
    """Describe aligned primary-standard points rejected before model fitting."""
    values = np.asarray(aligned_values, dtype=float)
    excluded = np.isfinite(values) & ~np.asarray(inlier_mask, dtype=bool)
    spot_indices, time_indices = np.where(excluded)
    records = pd.DataFrame(
        {
            "spot_name": [spot_names[index] for index in spot_indices],
            "signal_relative_time": np.asarray(unified_time)[time_indices],
            "fractionation_factor": values[spot_indices, time_indices],
        }
    )
    return {
        "outlier_sigma": None if sigma is None else float(sigma),
        "outlier_mask": excluded,
        "excluded_count": int(np.count_nonzero(excluded)),
        "excluded_aligned_points": records,
        "unfiltered_contributor_count": np.count_nonzero(
            np.isfinite(values), axis=0
        ),
        "contributor_count": np.count_nonzero(inlier_mask, axis=0),
    }


def downhole_plot_y_bounds(
    fitted_fractionation,
    residual_standard_deviation,
    *,
    sigma=3.0,
):
    """Return a useful initial y-range around a downhole correction model."""
    fitted = np.asarray(fitted_fractionation, dtype=float)
    fitted = fitted[np.isfinite(fitted)]
    if fitted.size == 0:
        return 0.0, 1.0
    fitted_low = float(np.min(fitted))
    fitted_high = float(np.max(fitted))
    center_scale = max(float(np.median(np.abs(fitted))), 1.0)
    model_span = fitted_high - fitted_low
    residual_padding = float(sigma) * float(residual_standard_deviation)
    padding = max(
        residual_padding if np.isfinite(residual_padding) else 0.0,
        model_span * 0.05,
        center_scale * 0.02,
        np.finfo(float).eps,
    )
    lower = fitted_low - padding
    upper = fitted_high + padding
    if fitted_low >= 0:
        lower = max(0.0, lower)
    return lower, upper


def add_downhole_outlier_markers(plot, metadata, y_bounds):
    """Add auditable hollow markers without letting anomalies set the viewport.

    Actual rejected values remain plotted and become visible when users zoom
    or pan outward. Values outside the focused initial y-range also receive a
    marker just inside the corresponding plot boundary. Hovering either marker
    reports the true factor and whether it was initially off scale.
    """
    excluded = metadata["excluded_aligned_points"].copy()
    if excluded.empty:
        return []
    lower, upper = (float(value) for value in y_bounds)
    span = max(upper - lower, np.finfo(float).eps)
    factors = excluded["fractionation_factor"].to_numpy(dtype=float)
    excluded["display_factor"] = np.clip(
        factors,
        lower + 0.025 * span,
        upper - 0.025 * span,
    )
    excluded["display_status"] = np.where(
        factors > upper,
        "above initial y-range",
        np.where(factors < lower, "below initial y-range", "within initial y-range"),
    )
    source = ColumnDataSource(excluded)
    actual = plot.scatter(
        x="signal_relative_time",
        y="fractionation_factor",
        source=source,
        marker="circle",
        size=8,
        fill_alpha=0,
        line_color="#d62728",
        line_width=1.8,
        legend_label="Excluded primary-standard point",
    )
    off_scale = excluded["display_status"] != "within initial y-range"
    boundary_renderers = []
    if off_scale.any():
        boundary_source = ColumnDataSource(excluded.loc[off_scale])
        boundary = plot.scatter(
            x="signal_relative_time",
            y="display_factor",
            source=boundary_source,
            marker="diamond",
            size=9,
            fill_alpha=0,
            line_color="#d62728",
            line_width=1.8,
            legend_label="Off-scale excluded point",
        )
        boundary_renderers.append(boundary)
    hover = HoverTool(
        renderers=[actual, *boundary_renderers],
        tooltips=[
            ("spot", "@spot_name"),
            ("signal-relative time", "@signal_relative_time{0.000} s"),
            ("actual factor", "@fractionation_factor{0.000000}"),
            ("display", "@display_status"),
        ],
    )
    plot.add_tools(hover)
    return [actual, *boundary_renderers]

def calc_DH_nominal_fractionation(
    known_ratio,
    primary_standard_ratios,
    ratio_name,
    return_metadata=False,
    correction_model='mean',
    model_kwargs=None,
    outlier_sigma=5.0,
):
    """Calculate nominal downhole fractionation against one known ratio.

    ``outlier_sigma`` robustly filters repeated-standard contributors at each
    aligned signal-relative time before constructing the mean correction.
    The default 5-sigma cutoff is deliberately conservative; pass another
    positive value to tune it or ``None`` to disable filtering.
    """
    primary_standard = primary_standard_ratios['sample'].unique()[0]
    # initialize a time variable and a ratio fractionation variable to store the interpolated time and fractionation for all primary standard spots
    all_times = [] # relative time within each spot, starts with zero
    all_ratios = [] # the fractionation of the measured ratio from the known standard ratio
    spot_names = []

    plot = figure(
        width=800, height=400,
        title=ratio_name + ' fractionation factor for primary standard ' + primary_standard,
        x_axis_label='Time (s)',
        y_axis_label=ratio_name + ' fractionation factor',
        tools='pan,wheel_zoom,box_zoom,reset,save', active_scroll='wheel_zoom',
    )
    for i, (_, this_spot) in enumerate(
        primary_standard_ratios.groupby('spot_name', sort=False)
    ):
        this_spot = this_spot.copy()
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
        spot_names.append(str(this_spot['spot_name'].iloc[0]))
        all_times.append(this_spot['relative_time'].values)
        all_ratios.append(this_spot[ratio_name].values)

    # now interpolate the time and fractionation for all spots, unify the time axis, and calculate the mean and std * student's t multiplier
    unified_time, interpolated_ratios, all_times, all_ratios = align_downhole_traces(
        all_times, all_ratios
    )
    
    interpolated_fractionations = known_ratio / np.asarray(interpolated_ratios, dtype=float)
    inlier_mask = robust_downhole_inlier_mask(
        interpolated_fractionations,
        outlier_sigma,
    )
    filtered_ratios = np.where(inlier_mask, interpolated_ratios, np.nan)
    filtered_fractionations = np.where(
        inlier_mask,
        interpolated_fractionations,
        np.nan,
    )
    # Preserve the nominal-ratio convention: average measured ratios first,
    # then calculate the target-over-mean fractionation factor.
    mean_ratio = np.nanmean(filtered_ratios, axis=0)
    mean_fractionation_factor = known_ratio / mean_ratio
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

    residuals = filtered_fractionations - fitted_fractionation_factor
    std_fractionation_factor = float(np.nanstd(residuals))
    outlier_metadata = downhole_outlier_metadata(
        unified_time,
        interpolated_fractionations,
        inlier_mask,
        spot_names,
        outlier_sigma,
    )
    downhole_metadata = {
        'time': unified_time,
        'mean_fractionation': mean_fractionation_factor,
        'fitted_fractionation': fitted_fractionation_factor,
        'model': 'mean' if model_method == 'interpolate' else fitted_model.method,
        'model_parameters': fitted_model.parameters,
        'model_settings': fitted_model.settings,
        'residuals': residuals,
        'residual_standard_deviation': std_fractionation_factor,
        **outlier_metadata,
    }

    for i, trace in enumerate(filtered_fractionations):
        plot.line(
            unified_time,
            trace,
            color=Category20[20][i % 20],
            line_width=1,
            alpha=0.6,
        )

    plot.varea(
        unified_time, fitted_fractionation_factor - std_fractionation_factor,
        fitted_fractionation_factor + std_fractionation_factor,
        fill_color='grey', fill_alpha=0.12,
    )
    plot.line(unified_time, fitted_fractionation_factor, color='black', line_width=2, line_dash='dashed', legend_label=f'{downhole_metadata["model"]} correction model')
    plot.line(unified_time, fitted_fractionation_factor + std_fractionation_factor, color='grey', line_dash='dashed', line_width=1, legend_label='residual 1 sigma')
    plot.line(unified_time, fitted_fractionation_factor - std_fractionation_factor, color='grey', line_dash='dashed', line_width=1)
    initial_y_bounds = downhole_plot_y_bounds(
        fitted_fractionation_factor,
        std_fractionation_factor,
    )
    plot.y_range.start, plot.y_range.end = initial_y_bounds
    add_downhole_outlier_markers(plot, downhole_metadata, initial_y_bounds)
    plot.legend.location = 'top_right'
    plot.legend.click_policy = 'hide'
    if downhole_metadata['excluded_count']:
        plot.title.text += (
            f" | {downhole_metadata['excluded_count']} excluded "
            f"(>{float(outlier_sigma):g} robust sigma)"
        )

    if return_metadata:
        return plot, downhole_fractionation, downhole_metadata
    return plot, downhole_fractionation

def calc_DH_fractionations_rad(primary_standard_ratios, 
                                standard_archive, 
                                ratio_names = ['206Pb/238U', '207Pb/206Pb', '208Pb/232Th'], 
                                fit=None,
                                *,
                                model='mean',
                                model_kwargs=None,
                                outlier_sigma=5.0):
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
    outlier_sigma : float or None, optional
        Robust cross-spot sigma cutoff applied independently at each aligned
        signal-relative time. The conservative default is 5. Set ``None`` to
        retain every finite primary-standard contributor.
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
            outlier_sigma=outlier_sigma,
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
            'outlier_sigma': downhole_metadata['outlier_sigma'],
            'excluded_count': downhole_metadata['excluded_count'],
            'excluded_aligned_points': downhole_metadata['excluded_aligned_points'],
        }})
    return DH_correction_results

def calc_DH_fractionations_TW(primary_68_standard_ratios, 
                                primary_76_standard_ratios, 
                                primary_82_standard_ratios, 
                                standard_archive, fit=None, *, model='mean',
                                model_kwargs=None, outlier_sigma=5.0):
    """Build downhole corrections for a common-Pb-bearing primary standard.

    The Pb/Pb standard first defines the sweep-wise ``207Pb/206Pb``
    correction. After applying it to the Pb-rich U/Pb standard, each corrected
    ordinate is horizontally projected onto that material's true
    Tera--Wasserburg mixing line. The projected ``206Pb/238U`` value is the
    sweep-specific target used to calculate U/Pb fractionation. Different
    standard spots or sweeps may therefore contain different proportions of
    common Pb without requiring a single total-Pb composition.

    Traces should cover the full selected signal and carry signal-relative time
    with zero at the selected signal start. The resulting correction is later
    evaluated at each plateau sweep's actual signal-relative time.

    Parameters
    ----------
    primary_68_standard_ratios : pandas.DataFrame
        Full-signal ratios for the common-Pb-bearing U/Pb standard.
    primary_76_standard_ratios : pandas.DataFrame
        Full-signal ratios for a standard with known ``207Pb/206Pb``.
    primary_82_standard_ratios : pandas.DataFrame
        Full-signal ratios for a standard with known ``208Pb/232Th``.
    standard_archive : pandas.DataFrame
        Reference archive containing the true ratios and initial Pb value.
    model : str or callable, optional
        Downhole calibration model; see :func:`calc_DH_fractionations_rad`.
    outlier_sigma : float or None, optional
        Robust cross-spot sigma cutoff applied independently at each aligned
        depth. The default is 5; ``None`` disables filtering.
    """

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
        outlier_sigma=outlier_sigma,
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
        'outlier_sigma': downhole_metadata_76['outlier_sigma'],
        'excluded_count': downhole_metadata_76['excluded_count'],
        'excluded_aligned_points': downhole_metadata_76['excluded_aligned_points'],
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
        outlier_sigma=outlier_sigma,
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
        'outlier_sigma': downhole_metadata_68['outlier_sigma'],
        'excluded_count': downhole_metadata_68['excluded_count'],
        'excluded_aligned_points': downhole_metadata_68['excluded_aligned_points'],
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
        outlier_sigma=outlier_sigma,
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
        'outlier_sigma': downhole_metadata_82['outlier_sigma'],
        'excluded_count': downhole_metadata_82['excluded_count'],
        'excluded_aligned_points': downhole_metadata_82['excluded_aligned_points'],
    }})
    return DH_correction_results

def calc_DH_dynamic_fractionation(
    primary_standard_ratios,
    measured_ratio_name,
    projected_ratio_name,
    return_metadata=False,
    correction_model='mean',
    model_kwargs=None,
    outlier_sigma=5.0,
):
    """Calculate a downhole model from sweep-specific projected targets.

    This is the U/Pb branch of the Tera--Wasserburg workflow. Repeated-standard
    contributors are robustly filtered across spots at each aligned depth.
    ``outlier_sigma=None`` disables that filtering.
    """
    primary_standard = primary_standard_ratios['sample'].unique()[0]
    # initialize a time variable and a ratio fractionation variable to store the interpolated time and fractionation for all primary standard spots
    all_times = [] # relative time within each spot, starts with zero
    all_fractionations = [] # the fractionation of the measured ratio from the known standard ratio
    spot_names = []

    plot = figure(
        width=800, height=400,
        title=measured_ratio_name + ' fractionation factor for primary standard ' + primary_standard,
        x_axis_label='Time (s)',
        y_axis_label=measured_ratio_name + ' fractionation factor',
        tools='pan,wheel_zoom,box_zoom,reset,save', active_scroll='wheel_zoom',
    )
    for i, (_, this_spot) in enumerate(
        primary_standard_ratios.groupby('spot_name', sort=False)
    ):
        this_spot = this_spot.copy()
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
        spot_names.append(str(this_spot['spot_name'].iloc[0]))
        all_times.append(this_spot['relative_time'].values)
        all_fractionations.append(this_spot[measured_ratio_name +'_fractionation_factor'].values)

    # now interpolate the time and fractionation for all spots, unify the time axis, and calculate the mean and std * student's t multiplier
    unified_time, interpolated_fractionations, all_times, all_fractionations = (
        align_downhole_traces(all_times, all_fractionations)
    )
    
    inlier_mask = robust_downhole_inlier_mask(
        interpolated_fractionations,
        outlier_sigma,
    )
    filtered_fractionations = np.where(
        inlier_mask,
        interpolated_fractionations,
        np.nan,
    )
    # calculate the stacked mean sweep-wise fractionation vs. relative time
    mean_fractionation_factor = np.nanmean(filtered_fractionations, axis=0)
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
    residuals = filtered_fractionations - fitted_fractionation_factor
    std_fractionation_factor = float(np.nanstd(residuals))
    outlier_metadata = downhole_outlier_metadata(
        unified_time,
        interpolated_fractionations,
        inlier_mask,
        spot_names,
        outlier_sigma,
    )
    downhole_metadata = {
        'time': unified_time,
        'mean_fractionation': mean_fractionation_factor,
        'fitted_fractionation': fitted_fractionation_factor,
        'model': 'mean' if model_method == 'interpolate' else fitted_model.method,
        'model_parameters': fitted_model.parameters,
        'model_settings': fitted_model.settings,
        'residuals': residuals,
        'residual_standard_deviation': std_fractionation_factor,
        **outlier_metadata,
    }

    for i, trace in enumerate(filtered_fractionations):
        plot.line(
            unified_time,
            trace,
            color=Category20[20][i % 20],
            line_width=1,
            alpha=0.6,
        )

    plot.varea(
        unified_time, fitted_fractionation_factor - std_fractionation_factor,
        fitted_fractionation_factor + std_fractionation_factor,
        fill_color='grey', fill_alpha=0.12,
    )
    plot.line(unified_time, fitted_fractionation_factor, color='black', line_width=2, line_dash='dashed', legend_label=f'{downhole_metadata["model"]} correction model')
    plot.line(unified_time, fitted_fractionation_factor + std_fractionation_factor, color='grey', line_dash='dashed', line_width=1, legend_label='residual 1 sigma')
    plot.line(unified_time, fitted_fractionation_factor - std_fractionation_factor, color='grey', line_dash='dashed', line_width=1)
    initial_y_bounds = downhole_plot_y_bounds(
        fitted_fractionation_factor,
        std_fractionation_factor,
    )
    plot.y_range.start, plot.y_range.end = initial_y_bounds
    add_downhole_outlier_markers(plot, downhole_metadata, initial_y_bounds)
    plot.legend.location = 'top_right'
    plot.legend.click_policy = 'hide'
    if downhole_metadata['excluded_count']:
        plot.title.text += (
            f" | {downhole_metadata['excluded_count']} excluded "
            f"(>{float(outlier_sigma):g} robust sigma)"
        )

    if return_metadata:
        return plot, downhole_fractionation, downhole_metadata
    return plot, downhole_fractionation

def apply_DH_correction(
    plateau_data_with_raw_ratios,
    DH_correction_results,
):
    """Apply downhole correction models to a sweep or plateau ratio table.

    Correction factors are evaluated for all rows in one vectorized pass per
    isotope ratio. Signal-relative time is calculated once, avoiding repeated
    per-spot filtering and DataFrame concatenation. This function is the first
    stage of the two-stage fractionation correction; session drift is applied
    separately by :func:`apply_session_correction`.
        
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
    """
    corrected = plateau_data_with_raw_ratios.copy()
    relative_time = relative_time_by_spot(corrected)
    for isotope_ratio, correction_result in DH_correction_results.items():
        correction_model = correction_result['interpolation']
        correction_factor = correction_model(relative_time)
        model_time = correction_result.get('fit_prediction_x')
        if model_time is not None:
            model_time = np.asarray(model_time, dtype=float)
            corrected[isotope_ratio + '_DH_correction_extrapolated'] = (
                (relative_time < np.nanmin(model_time))
                | (relative_time > np.nanmax(model_time))
            )
        # All UPbRPy fractionation factors use the convention
        # C = R_target / R_measured, so R_corrected = C * R_measured.
        corrected[isotope_ratio + '_DH_correction_factor'] = correction_factor
        corrected[isotope_ratio + '_DH_corrected'] = (
            corrected[isotope_ratio] * correction_factor
        )

    corrected.attrs = {}
    return corrected

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
    for _, this_spot in primary_standard_ratios.groupby('spot_name', sort=False):
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
    """Build session corrections for a common-Pb-bearing primary standard.

    One downhole-corrected plateau mean is calculated per standard spot. The
    Pb/Pb standard defines the session ``207Pb/206Pb`` correction. Corrected
    U/Pb-standard spot means are projected horizontally onto the true
    Tera--Wasserburg mixing line, producing a target ``206Pb/238U`` for each
    spot even when common-Pb proportions differ. A session model is fitted to
    those target-over-measured fractionation factors.
    
    Parameters
    ----------
    primary_68_DH_ratios : pandas.DataFrame
        Downhole-corrected plateau ratios for the Pb-rich U/Pb standard.
    primary_76_DH_ratios : pandas.DataFrame
        Downhole-corrected plateau ratios for the Pb/Pb standard.
    primary_82_DH_ratios : pandas.DataFrame
        Downhole-corrected plateau ratios for the Th/Pb standard.
    standard_archive : pandas.DataFrame
        Reference archive containing the true ratios and initial Pb value.
    degree : int
        the degree of the polynomial fit to be used for the session-wide fractionation correction

    Returns
    -------
    dict
        Correction models and diagnostic plots keyed by isotope ratio.
    """
    
    # The session Pb/Pb-corrected intermediate is an implementation detail,
    # not a mutation callers expect on their input table.
    primary_68_DH_ratios = primary_68_DH_ratios.copy()

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
    for _, this_spot in primary_68_DH_ratios.groupby('spot_name', sort=False):
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
    """Apply session correction models to all downhole-corrected rows.

    Models are evaluated column-wise over the complete time array. This retains
    the established numerical result while avoiding one DataFrame copy and
    concatenation per spot. Correction models derived from radiogenic,
    Tera--Wasserburg-projected, or total reference-material ratios all use the
    same target-over-measured multiplicative convention.

    Parameters
    ----------
    DH_corrected_ratios : pandas.DataFrame
        a pandas dataframe containing the downhole corrected ratios
    session_correction_results : dict
        a dictionary containing the session-wide fractionation correction plots and fits for each isotope ratio
    """
    corrected = DH_corrected_ratios.copy()
    session_time = corrected['Time'].to_numpy(dtype=float)
    for isotope_ratio, session_fit in session_correction_results.items():
        correction_factor = evaluate_correction_result(session_fit, session_time)
        corrected[isotope_ratio + '_session_correction_factor'] = correction_factor
        corrected[isotope_ratio + '_session_correction_1s'] = session_fit['std']
        fit_times = np.asarray(
            session_fit.get('fit_observations', {}).get('time', []),
            dtype=float,
        )
        if fit_times.size:
            corrected[isotope_ratio + '_session_correction_extrapolated'] = (
                (session_time < np.nanmin(fit_times))
                | (session_time > np.nanmax(fit_times))
            )
        else:
            corrected[isotope_ratio + '_session_correction_extrapolated'] = False
        corrected[isotope_ratio + '_fractionation_corrected'] = (
            corrected[isotope_ratio + '_DH_corrected'] * correction_factor
        )

    corrected.attrs = {}
    return corrected

__all__ = [
    'align_downhole_traces',
    'robust_downhole_inlier_mask',
    'downhole_outlier_metadata',
    'downhole_plot_y_bounds',
    'add_downhole_outlier_markers',
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
