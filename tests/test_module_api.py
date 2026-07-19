"""Compatibility checks for the focused package-module layout."""

import inspect

from bokeh.models import BoxAnnotation, MultiLine, Scatter
from bokeh.plotting import figure
import pandas as pd
import pytest

import UPbRPy
from UPbRPy import (
    concentration,
    constants,
    fractionation,
    io,
    isochron,
    isoplot,
    mpl_concordia,
    ratios,
    reduction,
    selection,
    thermometry,
    uncertainty,
    visualization,
)
from UPbRPy import UPbRPy as legacy


def test_legacy_module_reexports_focused_implementations():
    assert legacy.read_experiment_data is io.read_experiment_data
    assert legacy.apply_DH_correction is fractionation.apply_DH_correction
    assert legacy.calc_68_date is isoplot.calc_68_date
    assert (
        legacy.propagate_date_covariance
        is uncertainty.propagate_date_covariance
    )


def test_shared_constants_have_one_canonical_source():
    assert isochron.U238_U235 == constants.common_U238_U235
    assert isochron.LAMBDA_235 == constants.lambda_235
    assert isochron.LAMBDA_238 == constants.lambda_238


def test_selection_implementation_lives_in_io_module():
    assert io.select_signal_regions.__module__ == "UPbRPy.io"
    assert selection.select_signal_regions is io.select_signal_regions
    assert selection.SelectionResult is io.SelectionResult


def test_package_star_exports_include_core_and_isochron_apis():
    namespace = {}
    exec("from UPbRPy import *", namespace)

    assert namespace["calc_raw_ratios"] is UPbRPy.calc_raw_ratios
    assert namespace["LineFit"] is UPbRPy.LineFit
    assert namespace["initialize_notebook"] is visualization.initialize_notebook


def test_initialize_notebook_wraps_public_plot_helpers():
    panel = pytest.importorskip("panel")
    namespace = {}

    visualization.initialize_notebook(namespace=namespace)

    experiment = pd.DataFrame(
        {
            "spot_names": ["spot-1"],
            "baseline_start_times": [0.0],
            "baseline_end_times": [1.0],
            "plateau_start_times": [1.0],
            "plateau_end_times": [2.0],
        }
    )
    total_data = pd.DataFrame(
        {
            "spot_name": ["spot-1"] * 4,
            "Time": [0.25, 0.75, 1.25, 1.75],
            "29Si": [10.0, 11.0, 20.0, 21.0],
        }
    )

    pane = namespace["plot_selected_data"](experiment, total_data, "29Si")

    assert isinstance(pane, panel.pane.Bokeh)
    assert {"concordia", "plot_series", "plot_time_series"} <= set(namespace)
    assert "calc_concentration_calibration_factor" in namespace
    assert {
        "calc_DH_fractionations_rad",
        "calc_DH_fractionations_TW",
        "calc_session_fractionation_rad",
        "calc_session_fractionation_TW",
    } <= set(namespace)


def test_inline_correction_plots_wraps_figures_without_losing_fit_data():
    panel = pytest.importorskip("panel")
    correction_plot = figure(width=400, height=250)
    fit = object()
    original = {
        "206Pb/238U": {
            "plot": correction_plot,
            "fit": fit,
        }
    }

    inline = visualization.inline_correction_plots(original)

    assert isinstance(inline["206Pb/238U"]["plot"], panel.pane.Bokeh)
    assert inline["206Pb/238U"]["plot"].object is correction_plot
    assert inline["206Pb/238U"]["fit"] is fit
    assert original["206Pb/238U"]["plot"] is correction_plot


def test_inline_plot_pair_wraps_concentration_plot_without_losing_result():
    panel = pytest.importorskip("panel")
    calibration = pd.Series({"238U": 0.5})
    calibration_plot = figure(width=400, height=250)

    result, pane = visualization.inline_plot_pair(
        (calibration, calibration_plot)
    )

    assert result is calibration
    assert isinstance(pane, panel.pane.Bokeh)
    assert pane.object is calibration_plot


def test_concentration_calibration_legend_is_outside_in_two_columns():
    nist_data = pd.DataFrame(
        {
            "sample": ["NIST610", "NIST612"],
            "spot_name": ["NIST610_1", "NIST612_1"],
            "238U": [100.0, 50.0],
            "232Th": [80.0, 40.0],
        }
    )
    archive = pd.DataFrame(
        {
            "238U": [461.0, 37.4],
            "232Th": [457.0, 37.8],
        },
        index=["NIST610", "NIST612"],
    )

    _, calibration_plot = concentration.calc_concentration_calibration_factor(
        nist_data,
        ["238U", "232Th"],
        archive,
    )

    assert len(calibration_plot.legend) == 1
    legend = calibration_plot.legend[0]
    assert legend in calibration_plot.right
    assert legend not in calibration_plot.center
    assert legend.orientation == "horizontal"
    assert legend.ncols == 2


def test_plot_selected_data_skips_missing_selection_bounds():
    experiment = pd.DataFrame(
        {
            "spot_names": ["selected", "needs-review"],
            "baseline_start_times": [0.0, None],
            "baseline_end_times": [1.0, None],
            "plateau_start_times": [1.0, None],
            "plateau_end_times": [2.0, None],
        }
    )
    total_data = pd.DataFrame(
        {
            "spot_name": ["selected"] * 3 + ["needs-review"] * 3,
            "Time": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0],
            "29Si": [10.0, 20.0, 30.0, 11.0, 21.0, 31.0],
        }
    )

    plot = visualization.plot_selected_data(experiment, total_data, "29Si")
    scatter_renderers = [
        renderer
        for renderer in plot.renderers
        if isinstance(getattr(renderer, "glyph", None), Scatter)
    ]

    assert "1 spot missing selections" in plot.title.text
    assert len(scatter_renderers) == 2
    assert all(
        renderer.data_source.data["spot_name"].tolist() == ["selected"]
        for renderer in scatter_renderers
    )


def test_selection_editor_commits_only_active_spot_windows():
    panel = pytest.importorskip("panel")
    pytest.importorskip("jupyter_bokeh.widgets")
    experiment = pd.DataFrame(
        {
            "spot_names": ["spot-a", "spot-b"],
            "selection_channel": ["238U", "238U"],
            "baseline_start_times": [0.0, 100.0],
            "baseline_end_times": [2.0, 102.0],
            "plateau_start_times": [4.0, 104.0],
            "plateau_end_times": [8.0, 108.0],
        }
    )
    total_data = pd.DataFrame(
        {
            "spot_name": ["spot-a"] * 30 + ["spot-b"] * 30,
            "Time": list(range(30)) + list(range(100, 130)),
            "29Si": (
                [10.0] * 10
                + [100.0] * 10
                + [160.0]
                + [100.0] * 9
                + [10.0] * 10
                + [100.0] * 20
            ),
            "238U": [5.0] * 10 + [50.0] * 20 + [5.0] * 10 + [50.0] * 20,
        }
    )
    refreshed_calculations = []

    def refresh_baseline_calculation(updated_experiment, spot_name, windows):
        corrected = io.subtract_baseline(
            updated_experiment.copy(),
            ["29Si"],
            total_data.copy(),
        )
        refreshed_calculations.append((spot_name, windows, corrected))
        return "Baseline subtraction refreshed"

    editor = visualization.selection_editor(
        experiment,
        total_data,
        primary_channel="29Si",
        on_apply=refresh_baseline_calculation,
    )

    def descendants(component):
        yield component
        for child in getattr(component, "objects", []):
            yield from descendants(child)

    widgets = {
        component.name: component
        for component in descendants(editor)
        if getattr(component, "name", None)
    }
    pane = next(
        component for component in descendants(editor)
        if isinstance(component, panel.pane.Bokeh)
    )
    bands = {
        band.fill_color: band
        for band in pane.object.select({"type": BoxAnnotation})
        if band.editable
    }
    scatter_renderers = [
        renderer
        for renderer in pane.object.renderers
        if isinstance(getattr(renderer, "glyph", None), Scatter)
    ]
    primary_source = next(
        renderer.data_source
        for renderer in scatter_renderers
        if renderer.glyph.fill_color == "#252a34"
    )
    selected_filters = {
        renderer.glyph.fill_color: renderer.view.filter
        for renderer in scatter_renderers
        if renderer.glyph.fill_color in {"#c43d3d", "#2b74a8"}
    }
    plateau_line_renderers = [
        renderer
        for renderer in pane.object.renderers
        if isinstance(getattr(renderer, "glyph", None), MultiLine)
    ]

    assert pane.object.width == 1080
    assert len(scatter_renderers) == 6
    assert widgets["Selection module"].value == "excel_expanding_sigma"
    assert all("Excel" not in label for label in widgets["Selection module"].options)
    assert widgets["Onset sigma"].value == 8.0
    assert widgets["Max signal duration (s)"].value is None
    assert widgets["Plateau filter"].value == "excel_2sigma"
    assert tuple(widgets["Signal start/end (s)"].value) == (0.0, 29.0)
    assert all("Excel" not in label for label in widgets["Plateau filter"].options)
    assert widgets["Signal channel"].value == "29Si"
    assert bands["#c43d3d"].bottom is not None
    assert bands["#c43d3d"].top is not None
    assert bands["#2b74a8"].bottom is not None
    assert bands["#2b74a8"].top is not None
    assert primary_source.data["Time"][:2] == [0.0, 1.0]
    assert selected_filters["#c43d3d"].indices == [0, 1]
    assert selected_filters["#2b74a8"].indices == [4, 5, 6, 7]

    widgets["Previous spot"].clicks += 1
    assert widgets["Spot"].value == "spot-b"
    assert widgets["Signal channel"].value == "29Si"
    assert primary_source.data["Time"][:2] == [100.0, 101.0]
    assert selected_filters["#c43d3d"].indices == [0, 1]
    assert selected_filters["#2b74a8"].indices == [4, 5, 6, 7]
    widgets["Next spot"].clicks += 1
    assert widgets["Spot"].value == "spot-a"
    assert primary_source.data["Time"][:2] == [0.0, 1.0]

    widgets["Auto-pick baseline"].clicks += 1
    assert tuple(widgets["Baseline window (s)"].value) == (0.0, 10.0)
    assert tuple(widgets["Plateau window (s)"].value) == (4.0, 8.0)
    widgets["Selection module"].value = "threshold"
    widgets["Auto-pick plateau"].clicks += 1
    assert tuple(widgets["Baseline window (s)"].value) == (0.0, 10.0)
    assert tuple(widgets["Plateau window (s)"].value) == (12.0, 29.0)
    dashed_plateau = next(
        renderer
        for renderer in plateau_line_renderers
        if renderer.glyph.line_dash == "dashed"
    )
    assert dashed_plateau.data_source.data["xs"] == [
        [19.0, 20.0],
        [20.0, 21.0],
    ]

    widgets["Signal start/end (s)"].value = (15.2, 20.4)
    assert tuple(widgets["Signal start/end (s)"].value) == (12.0, 28.0)

    widgets["Baseline window (s)"].value = (10.0, 12.0)
    bands["#2b74a8"].left = 5.0
    bands["#2b74a8"].right = 9.0
    assert selected_filters["#c43d3d"].indices == [10, 11]
    assert selected_filters["#2b74a8"].indices == [5, 6, 7, 8]
    assert tuple(widgets["Signal start/end (s)"].value) == (5.0, 28.0)
    widgets["Signal start/end (s)"].value = (6.2, 27.4)
    assert tuple(widgets["Signal start/end (s)"].value) == (5.0, 27.0)
    widgets["Apply"].clicks += 1

    first_spot = experiment.loc[experiment["spot_names"] == "spot-a"].iloc[0]
    second_spot = experiment.loc[experiment["spot_names"] == "spot-b"].iloc[0]
    assert tuple(first_spot[["baseline_start_times", "baseline_end_times"]]) == (10.0, 12.0)
    assert tuple(first_spot[["plateau_start_times", "plateau_end_times"]]) == (5.0, 9.0)
    assert tuple(first_spot[["signal_start_times", "signal_end_times"]]) == (5.0, 27.0)
    assert tuple(second_spot[["baseline_start_times", "baseline_end_times"]]) == (100.0, 102.0)
    assert refreshed_calculations[-1][0] == "spot-a"
    assert refreshed_calculations[-1][1] == {
        "baseline": (10.0, 12.0),
        "plateau": (5.0, 9.0),
    }
    refreshed = refreshed_calculations[-1][2]
    assert refreshed.loc[refreshed["Time"] == 10.0, "29Si"].iloc[0] == 0.0


def test_excel_plateau_filter_rejects_outlier_around_linear_trend():
    time = pd.Series(range(20), dtype=float)
    signal = 100 - 2 * time + 0.1 * (time % 2)
    signal.iloc[8] += 30

    inliers = visualization.excel_plateau_inlier_mask(time, signal)

    assert inliers.sum() == 19
    assert not inliers[8]


def test_all_package_helpers_are_public_and_exported():
    modules = (
        concentration,
        fractionation,
        io,
        isochron,
        isoplot,
        mpl_concordia,
        ratios,
        reduction,
        thermometry,
        uncertainty,
        visualization,
    )
    for module in modules:
        module_functions = {
            name
            for name, value in inspect.getmembers(module, inspect.isfunction)
            if value.__module__ == module.__name__
        }
        assert not {name for name in module_functions if name.startswith("_")}
        assert module_functions <= set(module.__all__)


def test_former_helpers_are_available_from_package_root():
    for name in (
        "coerce_trace",
        "excel_expanding_sigma_onset",
        "find_intersection_ages",
        "finite_array",
        "inverse_standard_deviation_weights",
        "ellipse_from_covariance",
        "initialize_holoviews",
    ):
        assert name in UPbRPy.__all__
        assert hasattr(UPbRPy, name)
