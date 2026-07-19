"""General-purpose time-series visualization helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import inspect
from functools import wraps

from bokeh.models import (
    BoxAnnotation,
    CDSView,
    ColumnDataSource,
    CustomJS,
    HoverTool,
    IndexFilter,
    Range1d,
)
from bokeh.plotting import figure
import numpy as np
import pandas as pd
from scipy.stats import t as student_t

from .io import SelectionError, select_signal_regions


def excel_plateau_inlier_mask(
    time,
    signal,
    *,
    confidence: float = 0.95,
    iterations: int = 3,
) -> np.ndarray:
    """Return an Excel-style iterative inlier mask around a linear time trend.

    The reference VBA describes this as a 2-sigma filter. Its worksheet uses a
    two-sided Student-t confidence multiplier times ``STEYX`` and repeats the
    removal three times for count data. This implementation retains original
    row positions and never treats non-finite measurements as inliers.
    """
    x = np.asarray(time, dtype=float)
    y = np.asarray(signal, dtype=float)
    if x.ndim != 1 or y.ndim != 1 or x.size != y.size:
        raise ValueError("time and signal must be one-dimensional arrays of equal length.")
    confidence = float(confidence)
    iterations = int(iterations)
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1.")
    if iterations < 1:
        raise ValueError("iterations must be positive.")

    finite = np.isfinite(x) & np.isfinite(y)
    inliers = finite.copy()
    for _ in range(iterations):
        selected = np.flatnonzero(inliers)
        if selected.size < 3:
            break
        selected_x = x[selected]
        selected_y = y[selected]
        if np.ptp(selected_x) == 0:
            predicted = np.full_like(selected_y, np.mean(selected_y))
        else:
            slope, intercept = np.polyfit(selected_x, selected_y, 1)
            predicted = intercept + slope * selected_x
        residuals = selected_y - predicted
        residual_se = float(np.sqrt(np.sum(residuals**2) / max(1, selected.size - 2)))
        if not np.isfinite(residual_se) or residual_se == 0:
            break
        # The workbook passes COUNT as TINV's degrees of freedom.
        multiplier = float(student_t.ppf((1 + confidence) / 2, selected.size))
        keep_selected = np.abs(residuals) <= multiplier * residual_se
        updated = np.zeros_like(inliers)
        updated[selected[keep_selected]] = True
        if updated.sum() < 3 or np.array_equal(updated, inliers):
            break
        inliers = updated
    return inliers


def initialize_bokeh():
    """Initialize Bokeh output in an active notebook, when available."""
    try:
        from bokeh.io import output_notebook

        output_notebook(hide_banner=True)
    except Exception:
        # Plot objects still render through their rich representation in
        # notebook frontends that manage Bokeh resources themselves.
        pass


def initialize_notebook(*, namespace=None, comms: str = "vscode"):
    """Enable inline UPbRPy plots in a VS Code notebook with one setup call.

    Call this after importing UPbRPy in a notebook. It replaces the notebook's
    ``plot_selected_data``, ``plot_series``, ``plot_time_series``, and
    ``concordia`` names with Panel-backed display wrappers while retaining the
    same call signatures.
    """
    try:
        import panel as pn
        __import__("jupyter_bokeh.widgets")
    except ImportError as error:
        raise ImportError(
            "Notebook display support requires Panel and jupyter_bokeh. "
            "Install with `pip install 'UPbRPy[notebook]'`."
        ) from error

    if namespace is None:
        caller = inspect.currentframe().f_back
        if caller is None:
            raise RuntimeError("Pass the notebook namespace explicitly.")
        namespace = caller.f_globals

    from .isoplot import concordia

    pn.extension(comms=comms)

    def wrap_plot(function):
        @wraps(function)
        def display_plot(*args, **kwargs):
            return pn.pane.Bokeh(
                function(*args, **kwargs),
                sizing_mode="stretch_width",
            )

        return display_plot

    namespace.update(
        {
            "plot_selected_data": wrap_plot(plot_selected_data),
            "plot_series": wrap_plot(plot_series),
            "plot_time_series": wrap_plot(plot_time_series),
            "concordia": wrap_plot(concordia),
        }
    )


def selection_editor(
    experiment: pd.DataFrame,
    total_data: pd.DataFrame,
    *,
    primary_channel: str | None = None,
    channels: Iterable[str] | None = None,
    initial_spot: str | None = None,
    width: int = 1080,
    height: int = 430,
    comms: str = "vscode",
    on_apply: Callable[
        [pd.DataFrame, str, Mapping[str, tuple[float, float]]], object
    ] | None = None,
):
    """Build an inline editor for baseline and plateau windows.

    The returned Panel view keeps edits pending per spot until its ``Apply``
    button is pressed. Applying writes the selected spot's ordered time bounds
    directly to ``experiment`` using the same half-open window convention as
    the reduction functions. When supplied, ``on_apply`` runs after that
    committed update and can refresh selection-dependent calculations.
    """
    try:
        import panel as pn

        __import__("jupyter_bokeh.widgets")
    except ImportError as error:
        raise ImportError(
            "Interactive selection editing requires Panel and jupyter_bokeh. "
            "Install with `pip install 'UPbRPy[dev]'`."
        ) from error

    required_experiment_columns = {
        "spot_names",
        "baseline_start_times",
        "baseline_end_times",
        "plateau_start_times",
        "plateau_end_times",
    }
    missing_experiment_columns = required_experiment_columns - set(experiment)
    if missing_experiment_columns:
        missing = ", ".join(sorted(missing_experiment_columns))
        raise KeyError(f"experiment is missing selection columns: {missing}")
    required_data_columns = {"spot_name", "Time"}
    missing_data_columns = required_data_columns - set(total_data)
    if missing_data_columns:
        missing = ", ".join(sorted(missing_data_columns))
        raise KeyError(f"total_data is missing required columns: {missing}")

    spot_names = [str(name) for name in experiment["spot_names"].dropna().unique()]
    if not spot_names:
        raise ValueError("experiment has no spots to edit.")

    numeric_channels = [
        column
        for column in total_data.columns
        if column not in {"Time", "spot_name", "sample"}
        and pd.api.types.is_numeric_dtype(total_data[column])
    ]
    if channels is not None:
        requested_channels = [str(channel) for channel in channels]
        numeric_channels = [
            channel for channel in requested_channels if channel in numeric_channels
        ]
    if not numeric_channels:
        raise ValueError("total_data has no numeric signal channels to display.")
    if primary_channel in numeric_channels:
        numeric_channels.remove(primary_channel)
        numeric_channels.insert(0, primary_channel)

    pn.extension(comms=comms)

    source = ColumnDataSource(data={"Time": [], "Signal": []})
    baseline_filter = IndexFilter(indices=[])
    plateau_filter = IndexFilter(indices=[])
    plateau_inlier_filter = IndexFilter(indices=[])
    plateau_outlier_filter = IndexFilter(indices=[])
    signal_start_filter = IndexFilter(indices=[])
    signal_end_filter = IndexFilter(indices=[])
    plot = figure(
        width=width,
        height=height,
        x_range=Range1d(0, 1),
        title="Selection editor",
        x_axis_label="Time (s)",
        y_axis_label="cps",
        tools="pan,wheel_zoom,box_zoom,reset,save",
        active_scroll="wheel_zoom",
    )
    plot.line(
        "Time", "Signal", source=source, color="#252a34", line_width=1.5,
        line_alpha=0.42,
        legend_label="Signal trace",
    )
    measurements = plot.scatter(
        "Time",
        "Signal",
        source=source,
        marker="circle",
        size=5,
        fill_color="#252a34",
        line_color="#252a34",
        fill_alpha=0.72,
        line_alpha=0.72,
    )
    plot.add_tools(
        HoverTool(
            renderers=[measurements],
            mode="vline",
            tooltips=[("Time (s)", "@Time{0.000}"), ("Signal", "@Signal{0.000}")],
        )
    )
    baseline_band = BoxAnnotation(
        fill_color="#c43d3d",
        fill_alpha=0.16,
        line_color="#c43d3d",
        line_width=1.5,
        level="underlay",
        editable=True,
        movable="none",
        resizable="x",
        visible=False,
    )
    plateau_band = BoxAnnotation(
        fill_color="#2b74a8",
        fill_alpha=0.16,
        line_color="#2b74a8",
        line_width=1.5,
        level="underlay",
        editable=True,
        movable="none",
        resizable="x",
        visible=False,
    )
    plot.add_layout(baseline_band)
    plot.add_layout(plateau_band)
    plot.scatter(
        "Time",
        "Signal",
        source=source,
        view=CDSView(filter=baseline_filter),
        marker="circle",
        size=7,
        fill_color="#c43d3d",
        line_color="#c43d3d",
        fill_alpha=0.95,
        line_alpha=0.95,
        legend_label="Baseline points",
    )
    plot.line(
        "Time",
        "Signal",
        source=source,
        view=CDSView(filter=plateau_filter),
        color="#2b74a8",
        line_width=2,
        line_dash="dashed",
        line_alpha=0.9,
        legend_label="Plateau trace",
    )
    plot.scatter(
        "Time",
        "Signal",
        source=source,
        view=CDSView(filter=plateau_inlier_filter),
        marker="circle",
        size=7,
        fill_color="#2b74a8",
        line_color="#2b74a8",
        fill_alpha=0.95,
        line_alpha=0.95,
        legend_label="Plateau included",
    )
    plot.scatter(
        "Time",
        "Signal",
        source=source,
        view=CDSView(filter=plateau_outlier_filter),
        marker="circle",
        size=8,
        fill_color="white",
        line_color="#2b74a8",
        fill_alpha=1,
        line_alpha=1,
        line_width=2,
        legend_label="Plateau excluded",
    )
    plot.scatter(
        "Time",
        "Signal",
        source=source,
        view=CDSView(filter=signal_start_filter),
        marker="diamond",
        size=17,
        fill_color="#00E676",
        line_color="white",
        line_width=1.5,
        legend_label="Signal start",
    )
    plot.scatter(
        "Time",
        "Signal",
        source=source,
        view=CDSView(filter=signal_end_filter),
        marker="square",
        size=12,
        fill_color="#e07a1f",
        line_color="white",
        line_width=1.5,
        legend_label="Signal end",
    )

    baseline_stats_source = ColumnDataSource(
        data={"Time": [], "Mean": [], "Upper": [], "Lower": []}
    )
    plateau_stats_source = ColumnDataSource(
        data={"Time": [], "Mean": [], "Upper": [], "Lower": []}
    )
    for stats_source, color, label in (
        (baseline_stats_source, "#c43d3d", "Baseline mean"),
        (plateau_stats_source, "#2b74a8", "Plateau mean"),
    ):
        plot.line(
            "Time",
            "Mean",
            source=stats_source,
            color=color,
            line_width=3,
            legend_label=label,
        )
    if plot.legend:
        legend = plot.legend[0]
        legend.orientation = "horizontal"
        legend.location = "center"
        legend.click_policy = "hide"
        legend.label_text_font_size = "8pt"
        plot.add_layout(legend, "below")

    client_filter_code = """
const times = source.data.Time || [];
const values = source.data.Signal || [];
const indices = [];
if (band.left != null && band.right != null) {
  const start = Math.min(band.left, band.right);
  const end = Math.max(band.left, band.right);
  for (let index = 0; index < times.length; index += 1) {
    if (
      Number.isFinite(times[index]) &&
      Number.isFinite(values[index]) &&
      times[index] >= start &&
      times[index] < end
    ) {
      indices.push(index);
    }
  }
}
selection_filter.indices = indices;
selection_filter.change.emit();
"""
    for band, selection_filter in (
        (baseline_band, baseline_filter),
        (plateau_band, plateau_filter),
    ):
        callback = CustomJS(
            args={
                "band": band,
                "selection_filter": selection_filter,
                "source": source,
            },
            code=client_filter_code,
        )
        band.js_on_change("left", callback)
        band.js_on_change("right", callback)
        source.js_on_change("data", callback)

    selected_spot = initial_spot if initial_spot in spot_names else spot_names[0]
    spot_selector = pn.widgets.Select(
        name="Spot",
        options=spot_names,
        value=selected_spot,
    )
    channel_selector = pn.widgets.Select(
        name="Signal channel",
        options=numeric_channels,
        value=numeric_channels[0],
    )
    selection_module_options = {
        "Expanding baseline + σ": "excel_expanding_sigma",
        "Threshold": "threshold",
        "Rolling baseline + σ": "rolling_sigma",
    }
    selection_module_settings = {
        "excel_expanding_sigma": {
            "label": "expanding baseline + σ",
            "method": "excel_expanding_sigma",
        },
        "threshold": {
            "label": "threshold",
            "method": "threshold",
        },
        "rolling_sigma": {
            "label": "rolling baseline + σ",
            "method": "rolling_sigma",
        },
    }
    previous_spot_button = pn.widgets.Button(
        name="Previous spot",
        icon="chevron-left",
    )
    next_spot_button = pn.widgets.Button(
        name="Next spot",
        icon="chevron-right",
    )
    baseline_slider = pn.widgets.RangeSlider(
        name="Baseline window (s)",
        start=0,
        end=1,
        value=(0, 1),
        step=0.01,
    )
    plateau_slider = pn.widgets.RangeSlider(
        name="Plateau window (s)",
        start=0,
        end=1,
        value=(0, 1),
        step=0.01,
    )
    selection_module_selector = pn.widgets.Select(
        name="Selection module",
        options=selection_module_options,
        value="excel_expanding_sigma",
    )
    onset_sigma_input = pn.widgets.FloatInput(
        name="Onset sigma",
        value=8.0,
        start=0.1,
        step=0.5,
    )
    plateau_filter_selector = pn.widgets.Select(
        name="Plateau filter",
        options={
            "2σ trend filter (95%)": "excel_2sigma",
            "None": "none",
        },
        value="excel_2sigma",
    )
    auto_baseline_button = pn.widgets.Button(
        name="Auto-pick baseline",
        icon="wand",
    )
    auto_plateau_button = pn.widgets.Button(
        name="Auto-pick plateau",
        icon="wand",
    )
    apply_button = pn.widgets.Button(name="Apply", button_type="primary", icon="check")
    reset_button = pn.widgets.Button(name="Reset", icon="refresh")
    status = pn.pane.Markdown(sizing_mode="stretch_width")
    statistics = pn.pane.Markdown(sizing_mode="stretch_width", margin=(0, 5, 5, 5))
    instructions = pn.pane.Markdown(
        "#### Baseline and plateau windows\n"
        "Drag a colored band edge or set exact limits below. The green diamond and "
        "orange square mark signal start and end. Filled plateau symbols are included; "
        "hollow symbols are filtered out. The red and blue bars show means, and their "
        "shaded rectangles show mean ± 2σ. Apply commits the selected spot.",
        sizing_mode="stretch_width",
    )

    pending_windows: dict[str, dict[str, tuple[float, float]]] = {}
    current_frame = pd.DataFrame()
    current_spot = selected_spot
    current_signal_indices: tuple[int | None, int | None] = (None, None)
    syncing = False
    selection_feedback: str | None = None
    pending_signal_indices: dict[str, tuple[int, int]] = {}

    def spot_frame(spot_name: str) -> pd.DataFrame:
        frame = total_data.loc[
            total_data["spot_name"].astype(str) == spot_name
        ].copy()
        return frame.sort_values("Time").reset_index(drop=True)

    def time_limits(frame: pd.DataFrame) -> tuple[float, float, float]:
        times = pd.to_numeric(frame["Time"], errors="coerce").dropna().to_numpy()
        if times.size < 2:
            raise ValueError("A spot needs at least two finite time points to edit.")
        times = np.unique(np.sort(times.astype(float)))
        if times.size < 2 or times[-1] <= times[0]:
            raise ValueError("A spot needs a nonzero time span to edit.")
        spacing = np.diff(times)
        positive_spacing = spacing[spacing > 0]
        step = float(np.median(positive_spacing)) if positive_spacing.size else 0.01
        return float(times[0]), float(times[-1]), max(step, np.finfo(float).eps)

    def normalize_window(
        values: tuple[float, float],
        start_time: float,
        end_time: float,
        step: float,
    ) -> tuple[float, float]:
        start_value, end_value = sorted(float(value) for value in values)
        start_value = min(max(start_value, start_time), end_time)
        end_value = min(max(end_value, start_time), end_time)
        if end_value - start_value >= step:
            return start_value, end_value
        if start_value + step <= end_time:
            return start_value, start_value + step
        return end_time - step, end_time

    def saved_windows(
        spot_name: str,
        frame: pd.DataFrame,
    ) -> dict[str, tuple[float, float]]:
        start_time, end_time, step = time_limits(frame)
        row = experiment.loc[experiment["spot_names"].astype(str) == spot_name].iloc[0]

        def window(start_column: str, end_column: str, fallback: tuple[float, float]):
            values = pd.to_numeric(
                pd.Series([row[start_column], row[end_column]]), errors="coerce"
            ).to_numpy(dtype=float)
            if np.all(np.isfinite(values)) and values[1] > values[0]:
                return normalize_window(tuple(values), start_time, end_time, step)
            return normalize_window(fallback, start_time, end_time, step)

        span = end_time - start_time
        return {
            "baseline": window(
                "baseline_start_times",
                "baseline_end_times",
                (start_time, start_time + span * 0.2),
            ),
            "plateau": window(
                "plateau_start_times",
                "plateau_end_times",
                (end_time - span * 0.2, end_time),
            ),
        }

    def configured_window_values() -> dict[str, tuple[float, float]]:
        start_time, end_time, step = time_limits(current_frame)
        return {
            "baseline": normalize_window(
                tuple(baseline_slider.value), start_time, end_time, step
            ),
            "plateau": normalize_window(
                tuple(plateau_slider.value), start_time, end_time, step
            ),
        }

    def active_selection_options() -> tuple[Mapping[str, object], dict[str, object]]:
        settings = selection_module_settings[str(selection_module_selector.value)]
        method = str(settings["method"])
        options: dict[str, object] = {}
        if method == "excel_expanding_sigma":
            options = {
                "initial_window": 6,
                "sigma_threshold": float(onset_sigma_input.value),
                "consecutive": 1,
            }
        elif method == "rolling_sigma":
            options = {
                "rolling_window": 5,
                "sigma_threshold": float(onset_sigma_input.value),
                "consecutive": 2,
            }
        return settings, options

    def saved_signal_indices(spot_name: str, frame: pd.DataFrame) -> tuple[int | None, int | None]:
        if spot_name in pending_signal_indices:
            return pending_signal_indices[spot_name]
        row = experiment.loc[experiment["spot_names"].astype(str) == spot_name].iloc[0]
        times = pd.to_numeric(frame["Time"], errors="coerce").to_numpy(dtype=float)
        if {"signal_start_times", "signal_end_times"}.issubset(experiment.columns):
            saved_times = pd.to_numeric(
                pd.Series([row["signal_start_times"], row["signal_end_times"]]),
                errors="coerce",
            ).to_numpy(dtype=float)
            if np.all(np.isfinite(saved_times)):
                return (
                    int(np.nanargmin(np.abs(times - saved_times[0]))),
                    int(np.nanargmin(np.abs(times - saved_times[1]))),
                )
        return None, None

    def update_statistics_source(
        stats_source: ColumnDataSource,
        bounds: tuple[float, float],
        indices: list[int],
        values: np.ndarray,
        label: str,
    ) -> tuple[str, float | None, float | None]:
        band = baseline_band if label == "baseline" else plateau_band
        selected_values = values[np.asarray(indices, dtype=int)] if indices else np.array([])
        selected_values = selected_values[np.isfinite(selected_values)]
        if selected_values.size == 0:
            stats_source.data = {"Time": [], "Mean": [], "Upper": [], "Lower": []}
            band.visible = False
            return f"{label}: no finite sweeps", None, None
        mean = float(np.mean(selected_values))
        std = float(np.std(selected_values, ddof=1)) if selected_values.size > 1 else 0.0
        two_sigma = 2 * std
        stats_source.data = {
            "Time": [bounds[0], bounds[1]],
            "Mean": [mean, mean],
            "Upper": [mean + two_sigma, mean + two_sigma],
            "Lower": [mean - two_sigma, mean - two_sigma],
        }
        band.bottom = mean - two_sigma
        band.top = mean + two_sigma
        band.visible = True
        return (
            f"{label}: n={selected_values.size}, mean={mean:.6g}, 2σ={two_sigma:.6g}",
            mean,
            mean + two_sigma,
        )

    def update_selected_points(windows: Mapping[str, tuple[float, float]]) -> None:
        times = pd.to_numeric(current_frame["Time"], errors="coerce").to_numpy(dtype=float)
        values = pd.to_numeric(
            current_frame[channel_selector.value], errors="coerce"
        ).to_numpy(dtype=float)
        valid = np.isfinite(times) & np.isfinite(values)

        def selected_indices(bounds: tuple[float, float]) -> list[int]:
            mask = valid & (times >= bounds[0]) & (times < bounds[1])
            return np.flatnonzero(mask).tolist()

        baseline_indices = selected_indices(windows["baseline"])
        plateau_indices = selected_indices(windows["plateau"])
        plateau_inliers = plateau_indices
        plateau_outliers: list[int] = []
        if plateau_filter_selector.value == "excel_2sigma" and plateau_indices:
            local = np.asarray(plateau_indices, dtype=int)
            local_mask = excel_plateau_inlier_mask(times[local], values[local])
            plateau_inliers = local[local_mask].tolist()
            plateau_outliers = local[~local_mask].tolist()

        baseline_filter.indices = baseline_indices
        plateau_filter.indices = plateau_indices
        plateau_inlier_filter.indices = plateau_inliers
        plateau_outlier_filter.indices = plateau_outliers
        start_index, end_index = current_signal_indices
        signal_start_filter.indices = (
            [int(start_index)] if start_index is not None and 0 <= start_index < len(times) else []
        )
        signal_end_filter.indices = (
            [int(end_index)] if end_index is not None and 0 <= end_index < len(times) else []
        )

        baseline_message, _, _ = update_statistics_source(
            baseline_stats_source,
            windows["baseline"],
            baseline_indices,
            values,
            "baseline",
        )
        plateau_message, _, _ = update_statistics_source(
            plateau_stats_source,
            windows["plateau"],
            plateau_inliers,
            values,
            "plateau",
        )
        outlier_note = f", {len(plateau_outliers)} excluded" if plateau_outliers else ""
        statistics.object = (
            f"<span style='color:#a62f2f'><b>{baseline_message}</b></span>"
            " &nbsp;&nbsp; "
            f"<span style='color:#235f91'><b>{plateau_message}{outlier_note}</b></span>"
        )

    def update_status() -> None:
        windows = configured_window_values()
        channel = channel_selector.value
        messages = []
        valid = True
        for name, values in windows.items():
            selected = current_frame.loc[
                (current_frame["Time"] >= values[0])
                & (current_frame["Time"] < values[1])
            ]
            count = len(selected)
            valid &= count > 0
            if name == "plateau" and plateau_filter_selector.value == "excel_2sigma":
                included = len(plateau_inlier_filter.indices)
                excluded = len(plateau_outlier_filter.indices)
                messages.append(
                    f"{name}: {included}/{count} sweeps included ({excluded} excluded)"
                )
            else:
                messages.append(f"{name}: {count} sweeps")
        if windows["baseline"][1] > windows["plateau"][0]:
            messages.append("windows overlap")
        is_pending = (
            current_spot in pending_windows or current_spot in pending_signal_indices
        )
        state = "Pending" if is_pending else "Saved"
        spot_position = spot_names.index(current_spot) + 1
        if selection_feedback:
            messages.append(selection_feedback)
        if all(index is not None for index in current_signal_indices):
            times = pd.to_numeric(current_frame["Time"], errors="coerce").to_numpy(dtype=float)
            start_index, end_index = (int(index) for index in current_signal_indices)
            messages.append(
                f"signal: {times[start_index]:.3f}–{times[end_index]:.3f} s"
            )
        status.object = (
            f"**{state}**: {current_spot} ({spot_position}/{len(spot_names)}) | "
            f"{channel} | " + " | ".join(messages)
        )
        apply_button.disabled = not valid

    def set_band_bounds(windows: Mapping[str, tuple[float, float]]) -> None:
        baseline_band.left, baseline_band.right = windows["baseline"]
        plateau_band.left, plateau_band.right = windows["plateau"]

    def record_pending_windows() -> None:
        windows = configured_window_values()
        saved = saved_windows(current_spot, current_frame)
        equal_to_saved = all(
            np.allclose(windows[name], saved[name]) for name in windows
        )
        if equal_to_saved:
            pending_windows.pop(current_spot, None)
        else:
            pending_windows[current_spot] = windows
        update_selected_points(windows)
        update_status()

    def update_from_sliders(region: str, values: tuple[float, float]) -> None:
        nonlocal syncing, selection_feedback
        if syncing:
            return
        selection_feedback = None
        start_time, end_time, step = time_limits(current_frame)
        bounds = normalize_window(values, start_time, end_time, step)
        syncing = True
        try:
            if region == "baseline":
                baseline_slider.value = bounds
                baseline_band.left, baseline_band.right = bounds
            else:
                plateau_slider.value = bounds
                plateau_band.left, plateau_band.right = bounds
        finally:
            syncing = False
        record_pending_windows()

    def update_from_band(region: str) -> None:
        nonlocal syncing, selection_feedback
        if syncing:
            return
        selection_feedback = None
        band = baseline_band if region == "baseline" else plateau_band
        if band.left is None or band.right is None:
            return
        start_time, end_time, step = time_limits(current_frame)
        bounds = normalize_window(
            (float(band.left), float(band.right)), start_time, end_time, step
        )
        syncing = True
        try:
            if region == "baseline":
                baseline_band.left, baseline_band.right = bounds
                baseline_slider.value = bounds
            else:
                plateau_band.left, plateau_band.right = bounds
                plateau_slider.value = bounds
        finally:
            syncing = False
        record_pending_windows()

    def refresh_spot(*_events: object) -> None:
        nonlocal current_frame, current_spot, current_signal_indices, syncing, selection_feedback
        current_spot = str(spot_selector.value)
        current_frame = spot_frame(current_spot)
        selection_feedback = None
        start_time, end_time, step = time_limits(current_frame)
        windows = pending_windows.get(current_spot, saved_windows(current_spot, current_frame))
        channel = channel_selector.value
        row = experiment.loc[
            experiment["spot_names"].astype(str) == current_spot
        ].iloc[0]
        saved_error = row.get("selection_error")
        if pd.notna(saved_error) and str(saved_error).strip():
            selection_feedback = f"Auto-pick warning: {saved_error}"
        selected_method = str(row.get("selection_method", ""))
        if selected_method in selection_module_settings:
            selection_module_selector.value = selected_method
        saved_sigma = pd.to_numeric(
            pd.Series([row.get("selection_sigma_threshold")]), errors="coerce"
        ).iloc[0]
        if pd.notna(saved_sigma) and float(saved_sigma) > 0:
            onset_sigma_input.value = float(saved_sigma)

        current_signal_indices = saved_signal_indices(current_spot, current_frame)

        values = pd.to_numeric(current_frame[channel], errors="coerce").to_numpy(dtype=float)
        times = pd.to_numeric(current_frame["Time"], errors="coerce").to_numpy(dtype=float)
        syncing = True
        try:
            channel_selector.value = channel
            for slider, bounds in (
                (baseline_slider, windows["baseline"]),
                (plateau_slider, windows["plateau"]),
            ):
                slider.start = min(float(slider.start), start_time)
                slider.end = max(float(slider.end), end_time)
                slider.value = bounds
                slider.start = start_time
                slider.end = end_time
                slider.step = step
            set_band_bounds(windows)
            source.data = {"Time": times.tolist(), "Signal": values.tolist()}
            padding = max(step, (end_time - start_time) * 0.04)
            plot.x_range.start = start_time - padding
            plot.x_range.end = end_time + padding
            plot.title.text = f"{current_spot} | {channel}"
        finally:
            syncing = False
        update_selected_points(windows)
        update_status()

    def refresh_channel(*_events: object) -> None:
        nonlocal syncing, selection_feedback
        if current_frame.empty or syncing:
            return
        selection_feedback = None
        values = pd.to_numeric(
            current_frame[channel_selector.value], errors="coerce"
        ).to_numpy(dtype=float)
        times = pd.to_numeric(current_frame["Time"], errors="coerce").to_numpy(dtype=float)
        source.data = {"Time": times.tolist(), "Signal": values.tolist()}
        plot.title.text = f"{current_spot} | {channel_selector.value}"
        update_selected_points(configured_window_values())
        update_status()

    def change_spot(offset: int) -> None:
        current_index = spot_names.index(str(spot_selector.value))
        spot_selector.value = spot_names[(current_index + offset) % len(spot_names)]

    def auto_pick_region(region: str) -> None:
        nonlocal current_signal_indices, syncing, selection_feedback
        settings, method_options = active_selection_options()
        values = pd.to_numeric(
            current_frame[channel_selector.value], errors="coerce"
        ).to_numpy(dtype=float)
        times = pd.to_numeric(current_frame["Time"], errors="coerce").to_numpy(dtype=float)
        try:
            selection = select_signal_regions(
                values,
                method=str(settings["method"]),
                **method_options,
            )
            indices = (
                (selection.baseline_start, selection.baseline_end)
                if region == "baseline"
                else (selection.plateau_start, selection.plateau_end)
            )
            bounds = (float(times[indices[0]]), float(times[indices[1]]))
            start_time, end_time, step = time_limits(current_frame)
            bounds = normalize_window(bounds, start_time, end_time, step)
        except (IndexError, SelectionError, ValueError) as error:
            selection_feedback = (
                f"Could not auto-pick {region} with {settings['label']}: {error}"
            )
            update_status()
            return

        syncing = True
        try:
            if region == "baseline":
                baseline_slider.value = bounds
                baseline_band.left, baseline_band.right = bounds
            else:
                plateau_slider.value = bounds
                plateau_band.left, plateau_band.right = bounds
        finally:
            syncing = False
        current_signal_indices = (selection.signal_start, selection.signal_end)
        pending_signal_indices[current_spot] = (
            selection.signal_start,
            selection.signal_end,
        )
        selection_feedback = (
            f"Suggested {region} with {settings['label']} and signal edges; "
            "choose Apply to save"
        )
        record_pending_windows()

    def apply_windows(*_events: object) -> None:
        nonlocal selection_feedback
        windows = configured_window_values()
        row_mask = experiment["spot_names"].astype(str) == current_spot
        experiment.loc[row_mask, "baseline_start_times"] = windows["baseline"][0]
        experiment.loc[row_mask, "baseline_end_times"] = windows["baseline"][1]
        experiment.loc[row_mask, "plateau_start_times"] = windows["plateau"][0]
        experiment.loc[row_mask, "plateau_end_times"] = windows["plateau"][1]
        start_index, end_index = current_signal_indices
        if start_index is not None and end_index is not None:
            times = pd.to_numeric(current_frame["Time"], errors="coerce").to_numpy(dtype=float)
            experiment.loc[row_mask, "signal_start_times"] = times[int(start_index)]
            experiment.loc[row_mask, "signal_end_times"] = times[int(end_index)]
            experiment.loc[row_mask, "selection_detection_index"] = int(start_index)
            experiment.loc[row_mask, "selection_signal_end_index"] = int(end_index)
            experiment.loc[row_mask, "selection_method"] = str(
                selection_module_settings[str(selection_module_selector.value)]["method"]
            )
            experiment.loc[row_mask, "selection_channel"] = channel_selector.value
            experiment.loc[row_mask, "selection_error"] = None
            if str(selection_module_selector.value) != "threshold":
                experiment.loc[row_mask, "selection_sigma_threshold"] = float(
                    onset_sigma_input.value
                )
        pending_windows.pop(current_spot, None)
        pending_signal_indices.pop(current_spot, None)
        selection_feedback = None
        if on_apply is not None:
            try:
                result = on_apply(experiment, current_spot, windows)
            except Exception as error:
                selection_feedback = f"Saved windows; calculation refresh failed: {error}"
            else:
                if result is not None:
                    selection_feedback = str(result)
        update_status()

    def reset_windows(*_events: object) -> None:
        pending_windows.pop(current_spot, None)
        pending_signal_indices.pop(current_spot, None)
        refresh_spot()

    def update_selection_controls(*_events: object) -> None:
        onset_sigma_input.visible = str(selection_module_selector.value) != "threshold"
        if current_frame.empty or syncing:
            return
        update_status()

    def refresh_plateau_filter(*_events: object) -> None:
        if current_frame.empty or syncing:
            return
        update_selected_points(configured_window_values())
        update_status()

    spot_selector.param.watch(refresh_spot, "value")
    channel_selector.param.watch(refresh_channel, "value")
    selection_module_selector.param.watch(update_selection_controls, "value")
    onset_sigma_input.param.watch(update_selection_controls, "value")
    plateau_filter_selector.param.watch(refresh_plateau_filter, "value")
    baseline_slider.param.watch(
        lambda event: update_from_sliders("baseline", tuple(event.new)), "value"
    )
    plateau_slider.param.watch(
        lambda event: update_from_sliders("plateau", tuple(event.new)), "value"
    )
    for region, band in (("baseline", baseline_band), ("plateau", plateau_band)):
        band.on_change(
            "left", lambda _attr, _old, _new, name=region: update_from_band(name)
        )
        band.on_change(
            "right", lambda _attr, _old, _new, name=region: update_from_band(name)
        )
    apply_button.on_click(apply_windows)
    reset_button.on_click(reset_windows)
    previous_spot_button.on_click(lambda _event: change_spot(-1))
    next_spot_button.on_click(lambda _event: change_spot(1))
    auto_baseline_button.on_click(lambda _event: auto_pick_region("baseline"))
    auto_plateau_button.on_click(lambda _event: auto_pick_region("plateau"))

    update_selection_controls()
    refresh_spot()
    controls = pn.Column(
        spot_selector,
        pn.Row(previous_spot_button, next_spot_button),
        channel_selector,
        baseline_slider,
        plateau_slider,
        selection_module_selector,
        onset_sigma_input,
        plateau_filter_selector,
        pn.Row(auto_baseline_button, auto_plateau_button),
        pn.Row(apply_button, reset_button),
        status,
        width=320,
    )
    return pn.Column(
        instructions,
        pn.Row(
            controls,
            pn.Column(
                statistics,
                pn.pane.Bokeh(plot, width=width, sizing_mode="fixed"),
                width=width,
            ),
        ),
        sizing_mode="stretch_width",
    )


# Retain the former public entry point without retaining a HoloViews dependency.
initialize_holoviews = initialize_bokeh


def add_time_series(
    plot,
    dataframe: pd.DataFrame,
    y: str,
    *,
    x: str = "Time",
    color: str = "black",
    kind: str = "line",
    line_width: float = 1,
    size: float = 5,
    alpha: float = 1,
    label: str | None = None,
):
    """Add one line or scatter series to an existing Bokeh time-series plot."""
    if x not in dataframe or y not in dataframe:
        raise KeyError(f"Expected plotting columns {x!r} and {y!r}.")
    columns = [x, y]
    if "spot_name" in dataframe:
        columns.append("spot_name")
    source = ColumnDataSource(dataframe.loc[:, columns].dropna(subset=[x, y]))
    legend_label = label or y
    if kind == "line":
        renderer = plot.line(
            x=x,
            y=y,
            source=source,
            color=color,
            line_width=line_width,
            alpha=alpha,
            legend_label=legend_label,
        )
    elif kind == "scatter":
        renderer = plot.scatter(
            x=x,
            y=y,
            source=source,
            color=color,
            size=size,
            alpha=alpha,
            legend_label=legend_label,
        )
    else:
        raise ValueError("kind must be 'line' or 'scatter'.")

    tooltips = [(x, f"@{{{x}}}"), (y, f"@{{{y}}}")]
    if "spot_name" in source.data:
        tooltips.append(("spot", "@spot_name"))
    plot.add_tools(HoverTool(renderers=[renderer], tooltips=tooltips))
    return renderer


def plot_series(
    series: Iterable[Mapping[str, object]],
    *,
    x: str = "Time",
    title: str = "Time Series Plot",
    xlabel: str | None = None,
    ylabel: str | None = None,
    width: int = 1200,
    height: int = 400,
):
    """Plot multiple labelled DataFrame series on a native Bokeh figure."""
    plot = figure(
        width=width,
        height=height,
        title=title,
        x_axis_label=xlabel or x,
        y_axis_label=ylabel or "value",
        tools="pan,wheel_zoom,box_zoom,reset,save",
        active_scroll="wheel_zoom",
    )
    for spec in series:
        add_time_series(
            plot,
            spec["data"],
            str(spec["y"]),
            x=str(spec.get("x", x)),
            color=str(spec.get("color", "black")),
            kind=str(spec.get("kind", "line")),
            line_width=float(spec.get("line_width", 1)),
            size=float(spec.get("size", 5)),
            alpha=float(spec.get("alpha", 1)),
            label=str(spec["label"]) if spec.get("label") is not None else None,
        )
    if plot.legend:
        plot.legend.location = "top_right"
        plot.legend.click_policy = "hide"
    return plot

def plot_selected_data(experiment, total_data, channel, color='black', line_width=0.5, width=1200, height=400):
    """Plot one raw channel with the selected baseline and plateau rows."""
    windows = experiment.set_index("spot_names")
    spot_windows = windows.reindex(total_data["spot_name"])
    time = total_data["Time"].to_numpy()
    baseline_mask = (
        (time >= spot_windows["baseline_start_times"].to_numpy())
        & (time < spot_windows["baseline_end_times"].to_numpy())
    )
    plateau_mask = (
        (time >= spot_windows["plateau_start_times"].to_numpy())
        & (time < spot_windows["plateau_end_times"].to_numpy())
    )
    return plot_series(
        [
            {"data": total_data, "y": channel, "color": color, "line_width": line_width, "label": channel},
            {"data": total_data.loc[baseline_mask], "y": channel, "color": "#c43d3d", "kind": "scatter", "size": 4, "label": "baseline"},
            {"data": total_data.loc[plateau_mask], "y": channel, "color": "#2b74a8", "kind": "scatter", "size": 4, "label": "plateau"},
        ],
        title="Original data with selected baseline and plateau",
        xlabel="Time (s)",
        ylabel="cps",
        width=width,
        height=height,
    )

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
        bokeh.plotting.figure: A native Bokeh figure.
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

    return plot_series(
        [
            {
                "data": dataframe,
                "y": channel,
                "x": x,
                "color": channel_color,
                "kind": kind,
                "line_width": line_width,
                "label": channel,
            }
            for channel, channel_color in zip(y, color)
        ],
        x=x,
        title="Time Series Plot",
        xlabel=x,
        ylabel="cps",
        width=width,
        height=height,
    )

__all__ = [
    'add_time_series',
    'excel_plateau_inlier_mask',
    'initialize_bokeh',
    'initialize_holoviews',
    'initialize_notebook',
    'selection_editor',
    'plot_series',
    'plot_selected_data',
    'plot_time_series',
]
