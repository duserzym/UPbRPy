"""U-Pb age calculations, concordia diagrams, regressions, and age plots."""

from __future__ import annotations

from bokeh.models import (
    ColorBar,
    ColumnDataSource,
    CustomJS,
    HoverTool,
    LabelSet,
    LinearColorMapper,
)
from bokeh.palettes import Viridis256
from bokeh.plotting import figure
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import chi2, t

from .age import (
    calc_68_date,
    calc_68_date_uncertainty,
    calc_75_date,
    calc_75_date_uncertainty,
    calc_76_date,
    calc_76_date_uncertainty,
    calc_82_date,
    calc_82_date_uncertainty,
    propagate_reciprocal_uncertainty,
    radiogenic_76_derivative,
    radiogenic_76_ratio,
)
from .constants import (
    common_U238_U235,
    lambda_235,
    lambda_232,
    lambda_238,
    sigma_235,
    sigma_238,
)

EARTH_AGE_MA = 4567.3
COMMON_PB_RICH_MATERIALS = frozenset(
    {"apatite", "rutile", "titanite", "perovskite"}
)

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

def apply_default_common_pb_corrections(
    frame,
    *,
    material,
    common_pb_correction,
    common_pb_options,
):
    """Apply model ages when explicitly enabled or selected by material."""
    if common_pb_correction not in (True, False, "auto"):
        raise ValueError("common_pb_correction must be True, False, or 'auto'")
    material_key = str(material).strip().casefold() if material is not None else ""
    enabled = common_pb_correction is True or (
        common_pb_correction == "auto"
        and material_key in COMMON_PB_RICH_MATERIALS
    )
    if not enabled:
        return frame

    # Local import avoids making the plotting module and numerical isochron
    # module depend on each other while they are imported into the public API.
    from .isochron import apply_common_pb_corrections

    return apply_common_pb_corrections(frame, **(common_pb_options or {}))


def construct_final_depthpro_df(
    depthpro_concentration_df,
    depthpro_ratios_df,
    *,
    material=None,
    common_pb_correction="auto",
    common_pb_options=None,
):
    """Merge concentration and isotope-ratio depth profiles.

    Setting ``material`` to a common-Pb-rich phase such as ``"apatite"``
    automatically appends conventional 207- and 208-corrected model ages.
    Pass ``common_pb_correction=False`` to opt out, or ``True`` to enable the
    calculations for another material. Method settings are forwarded through
    ``common_pb_options`` to
    :func:`UPbRPy.isochron.apply_common_pb_corrections`.
    """
    # first drop the sample column from the concentration dataframe
    depthpro_ratios_df = depthpro_ratios_df.drop(columns=['sample', 'spot_name'])
    final_depthpro_df = pd.merge(depthpro_concentration_df, depthpro_ratios_df, on='Time')
    final_depthpro_df.sort_values(by='Time', inplace=True, ignore_index=True)
    final_depthpro_df = apply_default_common_pb_corrections(
        final_depthpro_df,
        material=material,
        common_pb_correction=common_pb_correction,
        common_pb_options=common_pb_options,
    )
    final_depthpro_df.attrs["material"] = material
    return final_depthpro_df


def construct_final_df(
    final_concentration_df,
    final_isotope_ratio_df,
    *,
    material=None,
    common_pb_correction="auto",
    common_pb_options=None,
):
    """Merge spot concentrations and isotope ratios into the final table.

    Common-Pb model ages are added by default when ``material`` identifies
    apatite, rutile, titanite, or perovskite. Other workflows remain unchanged.
    See :func:`construct_final_depthpro_df` for override options.
    """
    # first drop the sample column from the concentration dataframe
    final_concentration_df = final_concentration_df.drop(columns=['sample'])
    final_df = pd.merge(final_concentration_df, final_isotope_ratio_df, on='spot_name')
    final_df.sort_values(by='spot_name', inplace=True, ignore_index=True)
    # move the sample column to the front
    cols = ['sample'] + [col for col in final_df.columns if col != 'sample']
    final_df = final_df[cols]
    final_df = apply_default_common_pb_corrections(
        final_df,
        material=material,
        common_pb_correction=common_pb_correction,
        common_pb_options=common_pb_options,
    )
    final_df.attrs["material"] = material
    return final_df

def get_wetherill_point(age):
    '''
    get wetherill coordinates given an age or a list of ages
    age in Ma
    
    Return:
    ---------------------
    Pb207_U235, Pb206_U238 single value or two arrays
    '''
    age = age*1e6
    Pb206_U238 = np.exp(lambda_238*age)-1
    Pb207_U235 = np.exp(lambda_235*age)-1
    
    return Pb207_U235, Pb206_U238

def get_tera_wasserburg_point(age):
    '''
    get concordia coordinates given an age or a list of ages
    age in Ma
    
    Return:
    ---------------------
    Pb207_U235, Pb206_U238 single value or two arrays
    '''
    age = age*1e6
    Pb206_U238 = np.exp(lambda_238*age)-1
    Pb207_U235 = np.exp(lambda_235*age)-1
    
    return 1/Pb206_U238, Pb207_U235/Pb206_U238/common_U238_U235

def finite_array(values, positive=False):
    """Return finite values, optionally restricted to positive numbers."""
    arr = np.asarray(values, dtype=float)
    mask = np.isfinite(arr)
    if positive:
        mask &= arr > 0
    return arr[mask]

def nice_step(value):
    """Round a positive interval upward to a readable 1-2-5 axis step."""
    if not np.isfinite(value) or value <= 0:
        return 1.0
    exponent = np.floor(np.log10(value))
    fraction = value / 10**exponent
    if fraction <= 1:
        nice_fraction = 1
    elif fraction <= 2:
        nice_fraction = 2
    elif fraction <= 5:
        nice_fraction = 5
    else:
        nice_fraction = 10
    return nice_fraction * 10**exponent

def format_age_label(age, step):
    """Format an age label with precision implied by the label step."""
    if step >= 1:
        return str(int(round(age)))
    decimals = int(np.ceil(-np.log10(step)))
    return f"{age:.{decimals}f}".rstrip("0").rstrip(".")


def full_concordia_curve(plot_type, maximum_age=EARTH_AGE_MA, resolution=2500):
    """Return a full Earth-age concordia curve for interactive zooming."""
    ages = np.linspace(0.01, float(maximum_age), int(resolution))
    if plot_type == "Wetherill":
        x, y = get_wetherill_point(ages)
    elif plot_type == "TW":
        x, y = get_tera_wasserburg_point(ages)
    else:
        raise ValueError("plot_type must be either Wetherill or TW")
    return ages, np.asarray(x, dtype=float), np.asarray(y, dtype=float)


def visible_concordia_age_labels(plot_type, ages, x, y, xlim, ylim, maximum=12):
    """Choose readable concordia age labels within the current ratio limits."""
    if plot_type not in {"Wetherill", "TW"}:
        raise ValueError("plot_type must be either Wetherill or TW")
    ages = np.asarray(ages, dtype=float)
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    x_low, x_high = min(xlim), max(xlim)
    y_low, y_high = min(ylim), max(ylim)
    label_x_high = x_high - 0.04 * (x_high - x_low)
    label_y_high = y_high - 0.04 * (y_high - y_low)
    visible = (
        np.isfinite(ages)
        & np.isfinite(x)
        & np.isfinite(y)
        & (x >= x_low)
        & (x <= label_x_high)
        & (y >= y_low)
        & (y <= label_y_high)
    )
    visible_ages = ages[visible]
    if visible_ages.size == 0:
        return np.array([], dtype=float)
    age_min = float(np.min(visible_ages))
    age_max = float(np.max(visible_ages))
    span = max(age_max - age_min, 1.0)
    step = nice_step(span / max(int(maximum) - 2, 1))
    start = np.ceil(age_min / step) * step
    labels = np.arange(start, age_max + 0.5 * step, step)
    if labels.size > maximum:
        step = nice_step(step * labels.size / maximum)
        start = np.ceil(age_min / step) * step
        labels = np.arange(start, age_max + 0.5 * step, step)
    labels = labels[(labels >= 0) & (labels <= EARTH_AGE_MA)]
    if plot_type == "Wetherill":
        label_x, label_y = get_wetherill_point(labels)
    else:
        label_x, label_y = get_tera_wasserburg_point(labels)
    fits = (
        (label_x >= x_low)
        & (label_x <= label_x_high)
        & (label_y >= y_low)
        & (label_y <= label_y_high)
    )
    return labels[fits]


def add_dynamic_concordia_age_labels(
    plot,
    plot_type,
    curve_ages,
    curve_x,
    curve_y,
    initial_ages,
    *,
    color="black",
    curve_source=None,
):
    """Attach client-side age labels that update as a Bokeh plot is zoomed."""
    initial_ages = np.asarray(initial_ages, dtype=float)
    if plot_type == "Wetherill":
        initial_x, initial_y = get_wetherill_point(initial_ages)
    else:
        initial_x, initial_y = get_tera_wasserburg_point(initial_ages)
    initial_step = (
        nice_step(np.ptp(initial_ages) / max(len(initial_ages) - 1, 1))
        if initial_ages.size
        else 100.0
    )
    label_source = ColumnDataSource(
        {
            "x": np.asarray(initial_x, dtype=float),
            "y": np.asarray(initial_y, dtype=float),
            "label": [format_age_label(age, initial_step) for age in initial_ages],
        },
        name="concordia_age_labels",
    )
    plot.scatter(
        "x",
        "y",
        source=label_source,
        color=color,
        size=5,
        alpha=0.55,
    )
    plot.add_layout(
        LabelSet(
            x="x",
            y="y",
            text="label",
            source=label_source,
            x_offset=6,
            y_offset=5,
            text_color=color,
            text_font_size="9pt",
            text_baseline="middle",
        )
    )
    if curve_source is None:
        curve_source = ColumnDataSource(
            {
                "age": np.asarray(curve_ages, dtype=float),
                "x": np.asarray(curve_x, dtype=float),
                "y": np.asarray(curve_y, dtype=float),
            }
        )
    callback = CustomJS(
        args={
            "labels": label_source,
            "curve": curve_source,
            "x_range": plot.x_range,
            "y_range": plot.y_range,
            "plot_type": plot_type,
            "lambda235": lambda_235,
            "lambda238": lambda_238,
            "uranium_ratio": common_U238_U235,
            "earth_age": EARTH_AGE_MA,
        },
        code="""
const x0 = Math.min(x_range.start, x_range.end);
const x1 = Math.max(x_range.start, x_range.end);
const y0 = Math.min(y_range.start, y_range.end);
const y1 = Math.max(y_range.start, y_range.end);
const ages = curve.data.age;
const xs = curve.data.x;
const ys = curve.data.y;
let amin = Infinity;
let amax = -Infinity;
for (let i = 0; i < ages.length; i += 1) {
  if (xs[i] >= x0 && xs[i] <= x1 && ys[i] >= y0 && ys[i] <= y1) {
    amin = Math.min(amin, ages[i]);
    amax = Math.max(amax, ages[i]);
  }
}
if (!Number.isFinite(amin) || !Number.isFinite(amax)) {
  labels.data = {x: [], y: [], label: []};
  return;
}
const span = Math.max(amax - amin, 0.1);
const raw = span / 9;
const exponent = Math.floor(Math.log10(raw));
const magnitude = Math.pow(10, exponent);
const fraction = raw / magnitude;
let nice = 10;
if (fraction <= 1) nice = 1;
else if (fraction <= 2) nice = 2;
else if (fraction <= 5) nice = 5;
const step = nice * magnitude;
const first = Math.max(0, Math.ceil(amin / step) * step);
const outX = [];
const outY = [];
const outLabel = [];
for (let age = first; age <= Math.min(amax + step * 0.25, earth_age); age += step) {
  const years = age * 1e6;
  const pb68 = Math.exp(lambda238 * years) - 1;
  const pb75 = Math.exp(lambda235 * years) - 1;
  let x;
  let y;
  if (plot_type === "Wetherill") {
    x = pb75;
    y = pb68;
  } else {
    if (pb68 <= 0) continue;
    x = 1 / pb68;
    y = pb75 / pb68 / uranium_ratio;
  }
  if (x >= x0 && x <= x1 - 0.04 * (x1 - x0) &&
      y >= y0 && y <= y1 - 0.04 * (y1 - y0)) {
    outX.push(x);
    outY.push(y);
    const decimals = step >= 1 ? 0 : Math.max(0, Math.ceil(-Math.log10(step)));
    outLabel.push(age.toFixed(decimals));
  }
}
labels.data = {x: outX, y: outY, label: outLabel};
""",
    )
    for range_model in (plot.x_range, plot.y_range):
        range_model.js_on_change("start", callback)
        range_model.js_on_change("end", callback)
    return label_source

def ratio_bounds(values, one_sigma_errors, extra_values=None, sigma=2):
    """Return padded plot bounds enclosing values and uncertainty envelopes."""
    values = np.asarray(values, dtype=float)
    one_sigma_errors = np.asarray(one_sigma_errors, dtype=float)
    finite = np.isfinite(values)
    lower_values = values[finite] - sigma * np.nan_to_num(one_sigma_errors[finite], nan=0)
    upper_values = values[finite] + sigma * np.nan_to_num(one_sigma_errors[finite], nan=0)
    all_values = np.concatenate([lower_values, upper_values])
    if extra_values is not None:
        all_values = np.concatenate([all_values, finite_array(extra_values)])
    all_values = all_values[np.isfinite(all_values)]
    if len(all_values) == 0:
        return 0, 1

    lower = np.min(all_values)
    upper = np.max(all_values)
    center = np.nanmean(all_values)
    span = upper - lower
    error_scale = np.nanmedian(np.abs(one_sigma_errors[np.isfinite(one_sigma_errors)])) if np.any(np.isfinite(one_sigma_errors)) else 0
    pad = max(0.08 * span, 4 * error_scale, 0.01 * abs(center), 1e-12)
    if span == 0:
        lower -= pad
        upper += pad
    else:
        lower -= pad
        upper += pad
    return min(lower, upper), max(lower, upper)

def concordia_data_ages(plot_type, x, y):
    """Estimate data-covered ages from Wetherill or TW coordinates."""
    if plot_type == "Wetherill":
        ages = np.concatenate([
            calc_75_date(finite_array(x, positive=True)),
            calc_68_date(finite_array(y, positive=True)),
        ])
    else:
        x_positive = finite_array(x, positive=True)
        ages = calc_68_date(1 / x_positive) if len(x_positive) else np.array([])
    ages = ages[np.isfinite(ages)]
    if len(ages) == 0:
        return np.array([1000.0])
    return ages

def concordia_age_uncertainties(plot_type, x, y, sx, sy):
    """Convert coordinate one-sigma errors to approximate two-sigma ages."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    sx = np.asarray(sx, dtype=float)
    sy = np.asarray(sy, dtype=float)
    if plot_type == "Wetherill":
        with np.errstate(divide="ignore", invalid="ignore"):
            age_sigma_75 = sx / (lambda_235 * (1 + x)) / 1e6
            age_sigma_68 = sy / (lambda_238 * (1 + y)) / 1e6
        age_sigmas = np.concatenate([age_sigma_75, age_sigma_68])
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            age_sigmas = sx / (lambda_238 * x * (x + 1)) / 1e6
    age_2sigmas = 2 * finite_array(age_sigmas)
    return age_2sigmas[age_2sigmas > 0]

def concordia_plot_ranges(plot_type, x, y, sx, sy, age_min, age_max, xlim, ylim):
    """Choose initial concordia curve and axis ranges around measured data.

    Data-implied ages and propagated age uncertainties control padding. User
    limits override the corresponding automatically selected range.
    """
    data_ages = concordia_data_ages(plot_type, x, y)
    age_uncertainties = concordia_age_uncertainties(plot_type, x, y, sx, sy)
    age_uncertainty = np.nanmedian(age_uncertainties) if len(age_uncertainties) else 0

    data_age_min = float(np.nanmin(data_ages))
    data_age_max = float(np.nanmax(data_ages))
    data_age_span = data_age_max - data_age_min
    mean_data_age = float(np.nanmean(data_ages))
    if data_age_span > 0:
        preliminary_span = data_age_span
        age_pad = max(
            0.08 * data_age_span,
            0.01 * abs(mean_data_age),
            5.0,
        )
    else:
        preliminary_span = max(1.0, abs(mean_data_age) * 0.02)
        bounded_uncertainty = min(
            2 * age_uncertainty,
            0.25 * max(abs(mean_data_age), 1.0),
        )
        age_pad = max(bounded_uncertainty, preliminary_span, 5.0)
    bounded_age_uncertainty = min(age_uncertainty, preliminary_span / 4)
    age_step = nice_step(max(preliminary_span / 8, bounded_age_uncertainty))

    curve_min_age = data_age_min - age_pad if age_min is None else float(age_min)
    curve_max_age = data_age_max + age_pad if age_max is None else float(age_max)
    curve_min_age = max(0.01, curve_min_age)
    curve_max_age = min(4500.0, curve_max_age)
    if curve_min_age >= curve_max_age:
        curve_min_age = max(0.01, data_age_min - age_pad)
        curve_max_age = min(4500.0, data_age_max + age_pad)

    visible_age_span = max(curve_max_age - curve_min_age, age_step)
    age_step = nice_step(
        max(visible_age_span / 8, min(age_uncertainty, visible_age_span / 4))
    )
    tick_start = np.floor(curve_min_age / age_step) * age_step
    tick_end = np.ceil(curve_max_age / age_step) * age_step
    tick_ages = np.arange(tick_start, tick_end + 0.5 * age_step, age_step)
    tick_ages = tick_ages[(tick_ages >= 0) & (tick_ages <= 4500)]
    if len(tick_ages) > 14:
        age_step = nice_step(age_step * len(tick_ages) / 10)
        tick_start = np.floor(curve_min_age / age_step) * age_step
        tick_end = np.ceil(curve_max_age / age_step) * age_step
        tick_ages = np.arange(tick_start, tick_end + 0.5 * age_step, age_step)
        tick_ages = tick_ages[(tick_ages >= 0) & (tick_ages <= 4500)]

    # Always include the relevant concordia segment in the initial viewport.
    # This matters for discordant sweep data, whose x/y cloud may otherwise
    # fall wholly to one side of the curve.
    edge_ages = np.array([curve_min_age, curve_max_age])
    if plot_type == "Wetherill":
        extra_x, extra_y = get_wetherill_point(edge_ages)
    else:
        extra_x, extra_y = get_tera_wasserburg_point(edge_ages)

    if xlim is None:
        xlim = ratio_bounds(x, sx, extra_x)
    else:
        xlim = tuple(sorted(xlim))
    if ylim is None:
        ylim = ratio_bounds(y, sy, extra_y)
    else:
        ylim = tuple(sorted(ylim))

    return xlim, ylim, curve_min_age, curve_max_age, tick_ages, age_step

def concordia(data, type='Wetherill', 
              r75='207Pb/235U', r68='206Pb/238U', 
              r86='238U/206Pb', r76='207Pb/206Pb',
              s75='207Pb/235U_2e', s68='206Pb/238U_2e', 
              s86='238U/206Pb_2e', s76='207Pb/206Pb_2e',
              rho='err_corr',
              color_by=None, color_map='viridis_r', color_key='bar',
              discordia_fit=False, discordia_anchor=None, discordia_anchor_age=None, 
              age_min=None, age_max=None, xlim=None, ylim=None,
              concordia_color='grey', concordia_lw=1, concordia_alpha=0.5, concordia_uncertainty=False,
              ellipse_center_color='black', ellipse_center_alpha=0.5, ellipse_center_size=5, ellipse_center_marker='o',
              ellipse_fill_color='green', ellipse_fill_alpha=0.2, 
              ellipse_line_color='black', ellipse_line_width=1, ellipse_line_alpha=1,
              width=800, height=600):
    '''
    function for plotting the U-Pb ellipses on a concordia diagram

    Parameters
    ----------
    data : pandas.DataFrame
        a pandas dataframe containing the data to be plotted
    type : str, optional
        the type of concordia diagram to be plotted, either 'Wetherill' or 'TW', by default 'Wetherill'
    r75, r68, r86, r76 : str, optional
        the names of the columns in the data to be used for the 75, 68, 86, and 76 ratios, respectively, by default '207Pb/235U', '206Pb/238U', '238U/206Pb', '207Pb/206Pb'
    s75, s68, s86, s76 : str, optional
        the names of the columns in the data to be used for the 75, 68, 86, and 76 ratio uncertainties, respectively, by default '207Pb/235U_2e', '206Pb/238U_2e', '238U/206Pb_2e', '207Pb/206Pb_2e'
    rho : str, optional
        the name of the error correlation column in the data, by default 'err_corr'
    color_by : str, optional
        the name of the column in the data to color the points by, by default None
    color_map : str, optional
        the name of the color map to use, by default 'viridis_r'
    color_key : str, optional
        show a continuous ``'bar'`` or a vector-friendly discrete ``'legend'``
        when ``color_by`` is supplied, by default ``'bar'``
    discordia_fit : bool, optional
        whether to fit a discordia line to the data, by default False
    discordia_anchor : str, optional
        the name of the column in the data to use as the anchor point for the discordia line, by default None
    discordia_anchor_age : float, optional
        the age of the anchor point for the discordia line, by default None
    age_min : float, optional
        the minimum age to plot, by default None
    age_max : float, optional
        the maximum age to plot, by default None
    xlim : tuple, optional
        the x-axis limits, by default None
    ylim : tuple, optional
        the y-axis limits, by default None
    concordia_color : str, optional
        the color of the concordia line, by default 'grey'
    concordia_lw : float, optional
        the line width of the concordia line, by default 1
    concordia_alpha : float, optional
        the alpha value of the concordia line, by default 0.5
    concordia_uncertainty : bool, optional
        whether to plot the concordia uncertainty, by default False
    ellipse_center_color : str, optional
        the color of the ellipse center, by default 'black'
    ellipse_center_alpha : float, optional
        the alpha value of the ellipse center, by default 0.5
    ellipse_center_size : float, optional
        the size of the ellipse center, by default 5
    ellipse_center_marker : str, optional
        the marker style of the ellipse center, by default 'o'
    ellipse_fill_color : str, optional
        the color of the ellipse fill, by default 'green'
    ellipse_fill_alpha : float, optional
        the alpha value of the ellipse fill, by default 0.2
    ellipse_line_color : str, optional
        the color of the ellipse line, by default 'black'
    ellipse_line_width : float, optional
        the line width of the ellipse line, by default 1
    ellipse_line_alpha : float, optional
        the alpha value of the ellipse line, by default 1
    width, height : int, optional
        Fixed Bokeh canvas dimensions in pixels, by default 800 by 600.
        Supplying the final dimensions when the figure is constructed avoids
        a stale initial browser layout when the plot is embedded in a notebook.

    Returns
    -------
    bokeh.plotting.figure
        A native Bokeh concordia diagram.
    '''
    if color_key not in {'bar', 'legend'}:
        raise ValueError("color_key must be either 'bar' or 'legend'")
    if type == 'Wetherill':
        x = pd.to_numeric(data[r75], errors='coerce').to_numpy(dtype=float)
        y = pd.to_numeric(data[r68], errors='coerce').to_numpy(dtype=float)
        sx = pd.to_numeric(data[s75], errors='coerce').to_numpy(dtype=float) / 2
        sy = pd.to_numeric(data[s68], errors='coerce').to_numpy(dtype=float) / 2
        err_corr = pd.to_numeric(data[rho], errors='coerce').fillna(0).to_numpy(dtype=float)
    elif type == 'TW':
        x = pd.to_numeric(data[r86], errors='coerce').to_numpy(dtype=float)
        y = pd.to_numeric(data[r76], errors='coerce').to_numpy(dtype=float)
        sx = pd.to_numeric(data[s86], errors='coerce').to_numpy(dtype=float) / 2
        sy = pd.to_numeric(data[s76], errors='coerce').to_numpy(dtype=float) / 2
        err_corr = np.zeros(x.shape[0])
    else:
        raise ValueError('type must be either Wetherill or TW')

    xlim, ylim, _curve_min_age, _curve_max_age, _tick_ages, _age_step = concordia_plot_ranges(
        type, x, y, sx, sy, age_min, age_max, xlim, ylim
    )

    plot = figure(
        width=int(width),
        height=int(height),
        width_policy='fixed',
        height_policy='fixed',
        title='Wetherill Concordia Diagram' if type == 'Wetherill' else 'Tera-Wasserburg Concordia Diagram',
        x_axis_label='207Pb/235U' if type == 'Wetherill' else '238U/206Pb',
        y_axis_label='206Pb/238U' if type == 'Wetherill' else '207Pb/206Pb',
        x_range=xlim,
        y_range=ylim,
        tools='pan,wheel_zoom,box_zoom,reset,save',
        active_scroll='wheel_zoom',
    )
    curve_ages, curve_x, curve_y = full_concordia_curve(type)
    curve_source = ColumnDataSource(
        {"age": curve_ages, "x": curve_x, "y": curve_y},
        name="full_earth_concordia_source",
    )
    if type == 'Wetherill':
        plot.line(
            'x',
            'y',
            source=curve_source,
            color=concordia_color,
            line_width=concordia_lw,
            alpha=concordia_alpha,
            legend_label='Concordia',
            name='full_earth_concordia',
        )
        if concordia_uncertainty:
            Pb207_U235s, lower_68, upper_68 = get_Wetherill_concordia_uncertainty()
            plot.varea(Pb207_U235s, lower_68, upper_68, fill_color='grey', fill_alpha=0.25)
    elif type == 'TW':
        plot.line(
            'x',
            'y',
            source=curve_source,
            color=concordia_color,
            line_width=concordia_lw,
            alpha=concordia_alpha,
            legend_label='Concordia',
            name='full_earth_concordia',
        )
        if concordia_uncertainty:
            U238_Pb206s, lower_76, upper_76 = get_TW_concordia_uncertainty()
            plot.varea(U238_Pb206s, lower_76, upper_76, fill_color='grey', fill_alpha=0.25)

    initial_label_ages = visible_concordia_age_labels(
        type,
        curve_ages,
        curve_x,
        curve_y,
        xlim,
        ylim,
    )
    add_dynamic_concordia_age_labels(
        plot,
        type,
        curve_ages,
        curve_x,
        curve_y,
        initial_label_ages,
        curve_source=curve_source,
    )

    if color_by is None:
        # plot data ellipses
        for i in range(len(x)):
            if not np.all(np.isfinite([x[i], y[i], sx[i], sy[i], err_corr[i]])):
                continue
            ell_x, ell_y = calc_concordia_ellipse(x[i], y[i], sx[i], sy[i], err_corr[i])
            plot.patch(
                ell_x, ell_y,
                fill_color=ellipse_fill_color,
                fill_alpha=ellipse_fill_alpha,
                line_color=ellipse_line_color,
                line_width=ellipse_line_width,
                line_alpha=ellipse_line_alpha,
            )
    else:
        polygon_xs = []
        polygon_ys = []
        color_values = []
        for i in range(len(x)):
            if not np.all(np.isfinite([x[i], y[i], sx[i], sy[i], err_corr[i]])):
                continue
            ell_x, ell_y = calc_concordia_ellipse(x[i], y[i], sx[i], sy[i], err_corr[i])
            color_value = pd.to_numeric(pd.Series([data[color_by].iloc[i]]), errors='coerce').iloc[0]
            if not np.isfinite(color_value):
                continue
            polygon_xs.append(ell_x)
            polygon_ys.append(ell_y)
            color_values.append(float(color_value))

        if color_values:
            low, high = min(color_values), max(color_values)
            if low == high:
                high = low + 1
            palette = list(reversed(Viridis256))
            mapper = LinearColorMapper(
                palette=palette,
                low=low,
                high=high,
                name='concordia_color_mapper',
            )
            polygon_source = ColumnDataSource(
                {'xs': polygon_xs, 'ys': polygon_ys, 'value': color_values},
                name='concordia_ellipse_source',
            )
            plot.patches(
                xs='xs', ys='ys', source=polygon_source,
                fill_color={'field': 'value', 'transform': mapper},
                fill_alpha=ellipse_fill_alpha,
                line_color=ellipse_line_color,
                line_width=ellipse_line_width,
                line_alpha=ellipse_line_alpha,
            )
            if color_key == 'bar':
                plot.add_layout(ColorBar(color_mapper=mapper, title=color_by), 'right')
            else:
                key_values = np.linspace(low, high, min(5, len(color_values)))
                for key_value in key_values:
                    fraction = (key_value - low) / (high - low)
                    color_index = int(round(fraction * (len(palette) - 1)))
                    plot.scatter(
                        [np.nan],
                        [np.nan],
                        color=palette[color_index],
                        size=8,
                        legend_label=f'{key_value:g}',
                    )

    center_data = pd.DataFrame({
        'x': x,
        'y': y,
        'sample': data['sample'].astype(str).to_numpy() if 'sample' in data else np.full(len(data), ''),
        'age_75': pd.to_numeric(data.get('207Pb/235U_age', np.nan), errors='coerce'),
        'age_68': pd.to_numeric(data.get('206Pb/238U_age', np.nan), errors='coerce'),
    })
    center_source = ColumnDataSource(center_data, name='concordia_center_source')
    marker = {'o': 'circle', 's': 'square', '^': 'triangle', 'd': 'diamond'}.get(ellipse_center_marker, ellipse_center_marker)
    centers = plot.scatter(
        x='x', y='y', source=center_source,
        color=ellipse_center_color,
        alpha=ellipse_center_alpha,
        size=ellipse_center_size,
        marker=marker,
        legend_label='Data',
    )
    plot.add_tools(HoverTool(
        renderers=[centers],
        tooltips=[('sample', '@sample'), ('207Pb/235U age (Ma)', '@age_75{0.00}'), ('206Pb/238U age (Ma)', '@age_68{0.00}')],
    ))
    if plot.legend:
        plot.legend.location = 'top_right'
        plot.legend.click_policy = 'hide'
        if color_by is not None and color_key == 'legend':
            plot.legend.title = color_by
            legend = plot.legend[0]
            if legend in plot.center:
                plot.center.remove(legend)
            plot.add_layout(legend, 'right')
    return plot

def get_Wetherill_concordia_uncertainty():
    """Monte Carlo-sample decay constants to form a Wetherill uncertainty band."""
    Pb207_U235s = np.linspace(0.001,80, 1000)

    # resample Lambdas
    lambda_235s = np.random.normal(lambda_235, sigma_235, 1000)
    lambda_238s = np.random.normal(lambda_238, sigma_238, 1000)

    lower_68 = []
    upper_68 = []

    for i in range(len(Pb207_U235s)):
        Pb207_U235 = Pb207_U235s[i]
    
        all_Pb206_U238 = []
        for l_235, l_238 in zip(lambda_235s, lambda_238s):
            age = np.log(Pb207_U235+1)/l_235
            this_Pb206_U238 = np.exp(l_238*age)-1
            
            all_Pb206_U238.append(this_Pb206_U238)
        all_Pb206_U238 = np.array(all_Pb206_U238)
        lower_68.append(np.percentile(all_Pb206_U238, 2.5))
        upper_68.append(np.percentile(all_Pb206_U238, 97.5))

    return Pb207_U235s, lower_68, upper_68

def get_TW_concordia_uncertainty():
    """Monte Carlo-sample decay constants to form a TW uncertainty band."""
    U238_Pb206s = np.linspace(1, 100, 1000)

    # resample Lambdas
    lambda_235s = np.random.normal(lambda_235, sigma_235, 1000)
    lambda_238s = np.random.normal(lambda_238, sigma_238, 1000)

    lower_76 = []
    upper_76 = []

    for i in range(len(U238_Pb206s)):
        U238_Pb206 = U238_Pb206s[i]
    
        all_Pb207_Pb206 = []
        for l_235, l_238 in zip(lambda_235s, lambda_238s):
            age = np.log(1/U238_Pb206+1)/l_238
            this_Pb207_U235 = np.exp(l_235*age)-1
            this_Pb207_Pb206 = this_Pb207_U235 * U238_Pb206 / common_U238_U235
            all_Pb207_Pb206.append(this_Pb207_Pb206)
        all_Pb207_Pb206 = np.array(all_Pb207_Pb206)
        lower_76.append(np.percentile(all_Pb207_Pb206, 2.5))
        upper_76.append(np.percentile(all_Pb207_Pb206, 97.5))
    return U238_Pb206s, lower_76, upper_76

def cor2cov2(sx,sy,rxy):
    """Build a 2×2 covariance matrix from standard errors and correlation."""

    covmat = np.empty(shape=(2,2))
    covmat[0][0] = sx**2
    covmat[1][1] = sy**2
    covmat[0][1] = covmat[1][0] = rxy*sx*sy
    return covmat

def calc_concordia_ellipse(x,y,sx,sy,rxy, alpha=0.05, n=100):
    '''
    function for calculating error ellipses for Wetherill concordia diagram
    
    Required Parameters:
    ---------------------
    x: a list or array of x axis
    sx: the one sigma standard error of the x axis
    y: a list or array of y axis
    sy: the one sigma standard error of the y axis
    rxy: the correlation coefficient between the x and y axis

    Required Parameters:
    ---------------------
    alpha: (1-alpha)*100% confidence range
    n: number of points to calculate on the ellipse to smooth the shape
    '''
    cov_mat = cor2cov2(sx, sy, rxy)
    cutoff = chi2.ppf(1-alpha,2)
    eigenvalues, eigenvectors = np.linalg.eigh(cov_mat)
    eigenvalues = np.maximum(eigenvalues, 0)
    major_index = int(np.argmax(eigenvalues))
    minor_index = 1 - major_index

    a = np.sqrt(cutoff * eigenvalues[major_index])
    b = np.sqrt(cutoff * eigenvalues[minor_index])
    major_vector = eigenvectors[:, major_index]
    beta = np.arctan2(major_vector[1], major_vector[0])
    theta = np.linspace(0, 2 * np.pi, n)
    
    ell_x = x + a * np.cos(theta) * np.cos(beta) - b * np.sin(theta) * np.sin(beta)
    ell_y = y + a * np.cos(theta) * np.sin(beta) + b * np.sin(theta) * np.cos(beta)
    
    return np.asarray(ell_x, dtype=float), np.asarray(ell_y, dtype=float)

def york_fit(X, Y, dX, dY, ri=0.0, maxIter=1e6, tol=1e-8):
    '''
    Make a linear bivariate fit to X, Y data using York et al. (2004).
    This is an implementation of the line fitting algorithm presented in:
    York, D et al., Unified equations for the slope, intercept, and standard
    errors of the best straight line, American Journal of Physics, 2004, 72,
    3, 367-375, doi = 10.1119/1.1632486

    Required Parameters:
    -----------------------
    X, Y      x and y data points
    dX, dY    errors for the data points X, Y
      
    Optional Parameters:
    -----------------------
    ri          correlation coefficient for the weights
    maxIter     float, maXmum allowed number of iterations
    tol: tolerance for the precision of the slope
      
    Returns:
    -----------------------
    a           y-intercept, y = a + bx
    b           slope
    sigma_a     standard error of a
    sigma_b     standard error of b
    x_bar       x coordinate of the centroid of the best fit line
    y_bar       y coordinate of the centroid of the best fit line
    S           goodness-of-fit estimate
    cov_matrix  covariant matrix of the fit
    
    Usage:
    [a, b] = bivariate_fit( X, Y, dX, dY, ri, b0, maxIter)
    '''
    # (1) Choose an approXmate initial value of b based on ols
    b = np.polyfit(X, Y, 1)[1]
    
    # (2) Determine the weights wX, wY, for each point.
    wX = 1.0 / dX**2.0
    wY = 1.0 / dY**2.0

    alphai = (wX * wY)**0.5
    b_diff = 999.0

    # tolerance for the fit, when b changes by less than tol for two
    # consecutive iterations, fit is considered found
    tol = tol

    # iterate until b changes less than tol
    iIter = 1
    while (abs(b_diff) >= tol) & (iIter <= maxIter):

        b_prev = b

        # (3) Use these weights wX, wY to evaluate Wi for each point.
        Wi = (wX * wY) / (wX + b**2.0 * wY - 2.0*b*ri*alphai)

        # (4) Use the observed points (X ,Y) and Wi to calculate x_bar and
        # y_bar, from which Ui and Vi , and hence betai can be evaluated for
        # each point
        x_bar = np.sum(Wi * X) / np.sum(Wi)
        y_bar = np.sum(Wi * Y) / np.sum(Wi)

        Ui = X - x_bar
        Vi = Y - y_bar

        betai = Wi * (Ui / wY + b*Vi / wX - (b*Ui + Vi) * ri / alphai)

        # (5) Use Wi, Ui, Vi, and betai to calculate an improved estimate of b
        b = np.sum(Wi * betai * Vi) / np.sum(Wi * betai * Ui)

        # (6) Use the new b and repeat steps (3), (4), and (5) until successive
        # estimates of b agree within some desired tolerance tol
        b_diff = b - b_prev

        iIter += 1

    # (7) From this final value of b, together with the final x_bar and y_bar,
    # calculate a from
    a = y_bar - b * x_bar
    
    # Goodness of fit
    S = np.sum(Wi * (Y - b*X - a)**2.0)

    # (8) For each point (X, Y), calculate the adjusted values X_adj
    X_adj = x_bar + betai

    # (9) Use X_adj, together with Wi, to calculate X_adj_bar and thence ui
    X_adj_bar = np.sum(Wi * X_adj) / np.sum(Wi)
    ui = X_adj - X_adj_bar

    # (10) From Wi , X_adj_bar and ui, calculate sigma_b, and then sigma_a
    # (the standard uncertainties of the fitted parameters)
    sigma_b = np.sqrt(1.0 / np.sum(Wi * ui**2))
    sigma_a = np.sqrt(1.0 / np.sum(Wi) + X_adj_bar**2 * sigma_b**2)

    # calculate covariance matrix of b and a (York et al., Section II)
    cov = -X_adj_bar * sigma_b**2
    # [[var(b), cov], [cov, var(a)]]
    cov_matrix = np.array(
        [[sigma_b**2, cov], [cov, sigma_a**2]])
    
    X2 = np.sum(Wi*(Y-b*X-a)**2)
    mswd = X2/(len(X)-2)
    p_value = 1-chi2.cdf(X2, len(X)-2)
    if iIter <= maxIter:
        return {'a':a, 'b':b, 
                'sigma_a':sigma_a, 'sigma_b':sigma_b, 
                'x_bar':X_adj_bar, 'y_bar':y_bar, 
                'S':S, 'cov_matrix':cov_matrix, 
                'mswd':mswd, 'p_value':p_value}
    else:
        print("bivariate_fit.py exceeded maXmum number of iterations, " +
              "maxIter = {:}".format(maxIter))
        return np.nan, np.nan, np.nan, np.nan

def york_fit_zero_intercept(X, Y, sX, sY):
    '''
    function for getting York fit for a fit anchored at origin
    
    Required Parameters:
    ---------------------
    X: a list or array of x axis
    sX: the one sigma standard error of the x axis
    Y: a list or array of y axis
    sY: the one sigma standard error of the y axis
    
    Return:
    ---------------------
    b          slope of the fit
    sigma_b     standard error of b
    x_bar       x coordinate of the centroid of the best fit line
    y_bar       y coordinate of the centroid of the best fit line
    S           goodness-of-fit estimate    
    '''
    X, Y, sX, sY = np.array(X), np.array(Y), np.array(sX), np.array(sY)
    b, _, _, _ = np.linalg.lstsq(X[:,np.newaxis], Y, rcond=None) # initial guess
    wX = 1/sX**2
    wY = 1/sY**2
    for i in range(10):
        W = wX*wY/(wX+wY*b**2)
        Xbar = sum(W*X)/sum(W)
        Ybar = sum(W*Y)/sum(W)
        b = Ybar/Xbar
        
    U = X-Xbar
    V = Y-Ybar
    sigma_b = np.sum(W*(b*U-V)**2)/np.sum(W*U**2)/(len(X)-1)
    # Goodness of fit
    S = np.sum(W * (Y - b*X)**2.0)
    
    return {'b':b, 'sigma_b':sigma_b, 'x_bar':Xbar, 'y_bar':Ybar, 'S':S}

def plot_dates(zircon_means, zircon_2sigmas, include, 
               sample_means, sample_2sigmas, colors, labels,
               legend=True, figsize=None, ylim=None, lineweight=None, 
               bbox_to_anchor=None, plot_zircons=True):
    """
    Make a date distribution plot.
    
    inputs:
    - zircon_means = list of numpy arrays containing mean dates (each array being a sample)
    - zircon_2sigmas = list of numpy arrays containing 2sigmas (each array being a sample)
    - include = list of 'yes' or 'no' array for inclusion in mean (each array being a sample)
    - sample_means = list of sample mean dates (one per sample)
    - sample_2sigmas = list of sample 2sigmas (one per sample)
    - colors = list of colors (one per sample)
    - labels = list of labels (one per sample)
    - figsize = figure size (optional)
    - ylim = ylim (optional)
    - linewidth = zircon linewidth (optional)
    - bbox_to_anchor = for the legend (optional)
    """
    # formatting constants
    zircon_spacing = 0.9
    sample_spacing = 5.75 - zircon_spacing
    start_x = 3
    yes_alpha = 0.8
    no_alpha = 0.2
    x_positions = []
    if lineweight is None:
        lineweight=3
    
    # initialize current x
    current_x = start_x
    
    # number of samples
    n_samples = len(zircon_means)
    
    # plot
    if figsize is None:
        fig, ax = plt.subplots()
    else:
        fig, ax = plt.subplots(figsize=figsize)
    
    if not plot_zircons:
        for i in range(n_samples):
            sample_width = 20

            rectangle = matplotlib.patches.Rectangle((current_x-1, sample_means[i]-sample_2sigmas[i]),\
                                                     sample_width+1, sample_2sigmas[i]*2, color='lightgrey')
            ax.add_patch(rectangle)

            # the mean age

            ax.hlines(sample_means[i], current_x-1, current_x+sample_width)
            
            current_x = current_x + 25
        
    if plot_zircons:
        # iterate over each sample
        for i in range(n_samples):

            # number of zircons in sample
            n_zircons = len(zircon_means[i])

            # sample width
            sample_width = zircon_spacing*n_zircons

            rectangle = matplotlib.patches.Rectangle((current_x-1, sample_means[i]-sample_2sigmas[i]),\
                                                     sample_width+1, sample_2sigmas[i]*2, color='lightgrey')
            ax.add_patch(rectangle)

            # the mean age

            ax.hlines(sample_means[i], current_x-1, current_x+sample_width)

            # legend control
            marked = False

            # iterate over each zircon
            for j in range(n_zircons):

                if marked:
                    if include[i][j] == 'yes':
                        ax.vlines(current_x, zircon_means[i][j]-zircon_2sigmas[i][j], zircon_means[i][j]+zircon_2sigmas[i][j],\
                                  colors=colors[i], lw=lineweight, alpha=yes_alpha)
                    else:
                        ax.vlines(current_x, zircon_means[i][j]-zircon_2sigmas[i][j], zircon_means[i][j]+zircon_2sigmas[i][j],\
                                  colors=colors[i], lw=lineweight, alpha=no_alpha)
                else:
                    if include[i][j] == 'yes':
                        ax.vlines(current_x, zircon_means[i][j]-zircon_2sigmas[i][j], zircon_means[i][j]+zircon_2sigmas[i][j],\
                                  colors=colors[i], lw=lineweight, alpha=yes_alpha, label=labels[i])
                        marked = True
                    else:
                        ax.vlines(current_x, zircon_means[i][j]-zircon_2sigmas[i][j], zircon_means[i][j]+zircon_2sigmas[i][j],\
                                  colors=colors[i], lw=lineweight, alpha=no_alpha)

                # tick the x to the next zircon
                current_x = current_x + zircon_spacing

            # tick the x to the next sample
            current_x = current_x + sample_spacing
            x_positions.append(current_x-sample_spacing)
    
    # prettify
    ax.invert_yaxis()
    ax.set_ylabel('$^{206}$Pb/$^{238}$U date (Ma)',fontsize=13)
    if legend:
        if bbox_to_anchor is None:
            ax.legend(bbox_to_anchor=(1.8,1), fontsize=12)
        else:
            ax.legend(bbox_to_anchor=bbox_to_anchor, fontsize=12)
    if ylim is not None:
        ax.set_ylim(ylim)
        
    return fig, ax, x_positions

def weighted_mean(dates, sigma, student_T=True):
    
    """
    calculated a weighted mean from dates and uncertainities
    
    input parameters
    ----------------
    dates: date of individual grains
    sigma: 2σ uncertainty associated with dates
    student_T: if True, report the 95% confidence interval by multiplying the
        standard error of the mean by the student T value for n-1 degrees of freedom
    
    returns
    ----------------
    weighted_mean, 2σ uncertainty
    
    """
    sigma = np.array(sigma)/2
    inverse_variance=[]
    alpha=[]
    weighted_mean_calc=[]
    
    for n in range(0,len(dates)):
        value = 1/sigma[n]**2
        inverse_variance.append(value)
    inverse_variance_sum = np.sum(inverse_variance)
    
    for n in range(0,len(dates)):
        value = (1/sigma[n]**2)/inverse_variance_sum
        alpha.append(value)
        
    #calculate the weights for each date    
    for n in range(0,len(dates)):
        value = alpha[n]*dates[n]
        weighted_mean_calc.append(value)
    
    #take the sum of the weights multiplied by the dates to get the weighted
    #mean (equation 64 of McLean et al., 2011)
    weighted_mean = np.sum(weighted_mean_calc)
    
    #the variance of the weighted mean is simply the inverse of the sum of
    #the inverse variances of each date (equation 66 of McLean et al., 2011)
    variance = 1/inverse_variance_sum
    
    weighted_mean_sigma = np.sqrt(variance)
    
    n = len(dates)
    
    if student_T:
        # calculate the student T value for n-1 degrees of freedom
        this_t = t.ppf(0.975, n-1)
        weighted_mean_sigma = weighted_mean_sigma * this_t
        return weighted_mean, weighted_mean_sigma
    else:
        return weighted_mean, 2*weighted_mean_sigma

def plot_sample_weighted_mean(means, uncertainties, student_T = True, 
                              line_color='k', line_width=1, mean_alpha=0.5,
                              color='green', alpha=0.3, edgecolor='k',
                              figsize=(6,6)):
    '''
    function for plotting weighted mean results for grains of a sample

    Parameters
    ----------
    means: list of means for each grain
    uncertainties: list of 2σ uncertainties for each grain
    student_T: if True, report the 95% confidence interval by multiplying the
        standard error of the mean by the student T value for n-1 degrees of freedom
    line_color: color of the mean line
    line_width: width of the mean line
    mean_alpha: alpha of the mean line
    color: color of the grain rectangles
    alpha: alpha of the grain rectangles
    edgecolor: edge color of the grain rectangles
    figsize: figure size

    Returns
    -------
    fig: figure object
    '''
    # sort the means and uncertainties
    means, uncertainties = zip(*sorted(zip(means, uncertainties)))
    # first calculate the weighted mean and 95% confidence interval
    sample_weighted_mean, sample_mean_2sigma = weighted_mean(means, uncertainties, student_T)

    grain_spacing = 1
    grain_width=0.3
    start_x = 1
    # initialize current x
    current_x = start_x

    fig, ax = plt.subplots(figsize=figsize)

    # number of zircons in sample
    n_grains = len(means)

    # sample width
    sample_width = grain_spacing*n_grains

    rectangle = matplotlib.patches.Rectangle((current_x-1, sample_weighted_mean-sample_mean_2sigma),\
                                                sample_width+1, sample_mean_2sigma*2, color='lightgrey', alpha=mean_alpha)
    ax.add_patch(rectangle)

    # the mean age

    ax.hlines(sample_weighted_mean, current_x-1, current_x+sample_width, color=line_color, lw=line_width)

    # iterate over each zircon
    for j in range(n_grains):
        this_rectangle = matplotlib.patches.Rectangle((current_x-grain_width/2, 
                                                       means[j]-uncertainties[j]), 
                                                       grain_width, uncertainties[j]*2, 
                                                       facecolor=color, alpha=alpha, edgecolor=edgecolor)
        ax.add_patch(this_rectangle)

        current_x = current_x + grain_spacing

    ax.set_title(str(np.round(sample_weighted_mean, 3))+ r'$\pm$'+str(np.round(float(sample_mean_2sigma), 3)) +str(' Ma'), fontdict={'size': 12})

    # tick the x to the next sample
    current_x = current_x + grain_spacing
    ax.set_xlabel('Grain Number', fontdict={'size': 12})
    ax.set_ylabel('Age (Ma)', fontdict={'size': 12})
    ax.set_xticks(range(1, n_grains+1), range(1, n_grains+1))
    return fig, ax

__all__ = [
    'calc_TW_lower_intercept',
    'apply_default_common_pb_corrections',
    'construct_final_depthpro_df',
    'construct_final_df',
    'calc_75_date',
    'calc_75_date_uncertainty',
    'calc_68_date',
    'calc_68_date_uncertainty',
    'calc_82_date',
    'calc_82_date_uncertainty',
    'radiogenic_76_ratio',
    'radiogenic_76_derivative',
    'calc_76_date',
    'calc_76_date_uncertainty',
    'propagate_reciprocal_uncertainty',
    'get_wetherill_point',
    'get_tera_wasserburg_point',
    'finite_array',
    'nice_step',
    'format_age_label',
    'full_concordia_curve',
    'visible_concordia_age_labels',
    'add_dynamic_concordia_age_labels',
    'ratio_bounds',
    'concordia_data_ages',
    'concordia_age_uncertainties',
    'concordia_plot_ranges',
    'concordia',
    'get_Wetherill_concordia_uncertainty',
    'get_TW_concordia_uncertainty',
    'cor2cov2',
    'calc_concordia_ellipse',
    'york_fit',
    'york_fit_zero_intercept',
    'plot_dates',
    'weighted_mean',
    'plot_sample_weighted_mean',
]
