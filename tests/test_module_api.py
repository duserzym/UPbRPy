"""Compatibility checks for the focused package-module layout."""

import inspect
from pathlib import Path

import bokeh.io
from bokeh.models import BoxAnnotation, MultiLine, Scatter, Segment
from bokeh.plotting import figure
import numpy as np
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
    pytest.importorskip("panel")
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

    assert isinstance(pane, visualization.StandaloneBokehPlot)
    assert pane.object.title.text == "Original data with selected baseline and plateau"
    html = pane._repr_html_()
    assert "Bokeh.embed.embed_items" in html
    assert "application/vnd.jupyter.widget-view+json" not in html
    assert "static/extensions/" not in html
    assert html.count("cdn.bokeh.org") == 1
    assert "bokeh-widgets" not in html
    assert {"concordia", "plot_series", "plot_time_series"} <= set(namespace)
    assert "calc_concentration_calibration_factor" in namespace
    assert {
        "calc_DH_fractionations_rad",
        "calc_DH_fractionations_TW",
        "calc_session_fractionation_rad",
        "calc_session_fractionation_TW",
    } <= set(namespace)
    assert "plot_tw_spot_mean_session_projection" in namespace
    assert "plot_tw_standard_calibration" in namespace


def test_standalone_plot_resources_ignore_panel_extension_assets():
    panel = pytest.importorskip("panel")
    panel.extension()
    standalone = visualization.StandaloneBokehPlot(figure(width=300, height=200))

    html = standalone._repr_html_()

    assert "static/extensions/panel" not in html
    assert html.count("cdn.bokeh.org") == 1
    assert "bokeh-gl" not in html
    assert "bokeh-widgets" not in html
    assert "bokeh-tables" not in html
    assert "bokeh-mathjax" not in html


def test_panel_extension_treats_legacy_vscode_comms_as_auto_detection():
    calls = []

    class ExtensionRecorder:
        _loaded = False

        def __call__(self, **kwargs):
            calls.append(kwargs)

    panel_module = type("PanelModule", (), {"extension": ExtensionRecorder()})()

    visualization.initialize_panel_extension(panel_module, comms="vscode")

    assert calls == [{}]


def test_inline_plot_grid_combines_figures_into_one_document():
    first = visualization.StandaloneBokehPlot(figure(width=300, height=200))
    second = visualization.StandaloneBokehPlot(figure(width=300, height=200))

    combined = visualization.inline_plot_grid([first, second], ncols=2)
    html = combined._repr_html_()

    assert isinstance(combined, visualization.StandaloneBokehPlot)
    assert len(combined.object.children) == 2
    assert "GridPlot" in html
    assert html.count("Bokeh.embed.embed_items") == 1


def test_notebook_display_sampling_retains_spot_endpoints_and_spike():
    frame = pd.DataFrame(
        {
            "spot_name": ["a"] * 100 + ["b"] * 100,
            "Time": np.arange(200, dtype=float),
            "signal": np.r_[np.ones(49), 100.0, np.ones(50), np.ones(100)],
        }
    )
    frame.attrs["nested_frame"] = pd.DataFrame({"value": [1, 2]})

    rendered = visualization.plot_series(
        [{"data": frame, "y": "signal"}],
        max_display_points=24,
    )
    source = rendered.renderers[0].data_source.data

    assert len(source["Time"]) <= 24
    assert {0.0, 99.0, 100.0, 199.0} <= set(source["Time"])
    assert 100.0 in source["signal"]


def test_plot_display_limit_is_shared_across_series():
    frame = pd.DataFrame(
        {
            "Time": np.arange(200, dtype=float),
            "first": np.sin(np.arange(200, dtype=float)),
            "second": np.cos(np.arange(200, dtype=float)),
        }
    )

    rendered = visualization.plot_series(
        [
            {"data": frame, "y": "first"},
            {"data": frame, "y": "second"},
        ],
        max_display_points=40,
    )

    assert sum(
        len(renderer.data_source.data["Time"])
        for renderer in rendered.renderers
    ) <= 40


def test_inline_correction_plots_wraps_figures_without_losing_fit_data():
    pytest.importorskip("panel")
    correction_plot = figure(width=400, height=250)
    fit = object()
    original = {
        "206Pb/238U": {
            "plot": correction_plot,
            "fit": fit,
        }
    }

    inline = visualization.inline_correction_plots(original)

    assert isinstance(
        inline["206Pb/238U"]["plot"], visualization.StandaloneBokehPlot
    )
    assert inline["206Pb/238U"]["plot"].object is correction_plot
    assert inline["206Pb/238U"]["fit"] is fit
    assert original["206Pb/238U"]["plot"] is correction_plot


def test_inline_plot_pair_wraps_concentration_plot_without_losing_result():
    pytest.importorskip("panel")
    calibration = pd.Series({"238U": 0.5})
    calibration_plot = figure(width=400, height=250)

    result, pane = visualization.inline_plot_pair(
        (calibration, calibration_plot)
    )

    assert result is calibration
    assert isinstance(pane, visualization.StandaloneBokehPlot)
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


def test_concentration_calibration_matches_elements_across_isotope_masses():
    nist_data = pd.DataFrame(
        {
            "sample": ["NIST610", "NIST612"],
            "spot_name": ["NIST610_1", "NIST612_1"],
            "151Eu": [100.0, 10.0],
            "153Eu": [101.0, 11.0],
        }
    )
    archive = pd.DataFrame(
        {"153Eu": [461.0, 35.0]},
        index=["NIST610", "NIST612"],
    )

    calibration, _ = concentration.calc_concentration_calibration_factor(
        nist_data,
        ["151Eu", "153Eu"],
        archive,
    )

    assert list(calibration.index) == ["153Eu"]
    assert calibration.attrs["reference_channels"]["153Eu"] == {
        "NIST610": "153Eu",
        "NIST612": "153Eu",
    }
    expected = np.dot([101.0, 11.0], [461.0, 35.0]) / np.dot(
        [101.0, 11.0],
        [101.0, 11.0],
    )
    assert calibration["153Eu"] == expected

    fallback, _ = concentration.calc_concentration_calibration_factor(
        nist_data,
        ["151Eu"],
        archive,
    )
    assert list(fallback.index) == ["151Eu"]
    assert fallback.attrs["reference_channels"]["151Eu"] == {
        "NIST610": "153Eu",
        "NIST612": "153Eu",
    }


def test_internal_channel_selection_is_isotope_mass_agnostic():
    assert concentration.channel_for_element(["43Ca", "238U"], "44Ca") == "43Ca"
    value, archive_channel = concentration.resolve_element_concentration(
        pd.DataFrame({"44Ca": [400443.0]}, index=["apatite"]),
        "apatite",
        "43Ca",
    )
    assert value == 400443
    assert archive_channel == "44Ca"


def test_tw_standard_calibration_plot_projects_sweeps_and_spot_means():
    raw = pd.DataFrame(
        {
            "sample": ["MAD"] * 4,
            "spot_name": ["MAD 1", "MAD 1", "MAD 2", "MAD 2"],
            "Time": [1.0, 2.0, 3.0, 4.0],
            "206Pb/238U": [0.07, 0.071, 0.069, 0.0705],
            "207Pb/206Pb": [0.62, 0.61, 0.64, 0.63],
        }
    )
    downhole = raw[["sample", "spot_name", "Time"]].copy()
    downhole["206Pb/238U_DH_corrected"] = [0.075, 0.076, 0.074, 0.0755]
    downhole["207Pb/206Pb_DH_corrected"] = [0.60, 0.59, 0.62, 0.61]
    corrected = raw[["sample", "spot_name", "Time"]].copy()
    corrected["206Pb/238U_fractionation_corrected"] = [
        0.078,
        0.079,
        0.077,
        0.0785,
    ]
    corrected["207Pb/206Pb_fractionation_corrected"] = [
        0.58,
        0.57,
        0.60,
        0.59,
    ]
    archive = pd.DataFrame(
        {
            "206Pb/238U*": [0.08],
            "207Pb/206Pb*": [0.055],
            "207Pb/206Pb_i": [0.71],
        },
        index=["MAD"],
    )

    result = visualization.plot_tw_standard_calibration(
        raw,
        downhole,
        corrected,
        archive,
        "MAD",
    )

    assert {"layout", "sweepwise", "spot_mean"}.issubset(result)
    assert len(result["sweepwise_projection"]) == 4
    assert len(result["spot_mean_projection"]) == 2
    for plot in (result["sweepwise"], result["spot_mean"]):
        concordia = plot.select(name="full_earth_concordia")[0]
        legend = plot.legend[0]
        assert max(concordia.data_source.data["age"]) == pytest.approx(4567.3)
        assert plot.x_range.start == 0
        assert plot.x_range.end < max(concordia.data_source.data["x"])
        assert len(plot.select(name="concordia_age_labels")) == 1
        assert legend in plot.below
        assert legend.orientation == "horizontal"
        assert legend.ncols == 2
    slope = (0.055 - 0.71) / (1 / 0.08)
    projection = result["sweepwise_projection"]
    expected_y = 0.71 + slope * projection["x_target"]
    np.testing.assert_allclose(expected_y, projection["y_corrected"])

    namespace = {}
    visualization.initialize_notebook(namespace=namespace)
    inline_result = namespace["plot_tw_standard_calibration"](
        raw,
        downhole,
        corrected,
        archive,
        "MAD",
    )

    assert isinstance(inline_result["layout"], visualization.StandaloneBokehPlot)
    assert inline_result["layout"].object is not None
    assert not isinstance(
        inline_result["sweepwise"], visualization.StandaloneBokehPlot
    )
    assert "Bokeh.embed.embed_items" in inline_result["layout"]._repr_html_()


def test_tw_spot_mean_session_projection_matches_session_factor_geometry():
    downhole = pd.DataFrame(
        {
            "sample": ["MAD"] * 4,
            "spot_name": ["MAD 1", "MAD 1", "MAD 2", "MAD 2"],
            "Time": [1.0, 2.0, 3.0, 4.0],
            "206Pb/238U_DH_corrected": [0.075, 0.077, 0.073, 0.075],
            "207Pb/206Pb_DH_corrected": [0.60, 0.58, 0.62, 0.60],
        }
    )
    archive = pd.DataFrame(
        {
            "206Pb/238U*": [0.08],
            "207Pb/206Pb*": [0.055],
            "207Pb/206Pb_i": [0.71],
        },
        index=["MAD"],
    )
    session = {
        "207Pb/206Pb": {
            "predictor": lambda time: np.full(np.asarray(time).shape, 1.01),
        }
    }

    plot = visualization.plot_tw_spot_mean_session_projection(
        downhole,
        session,
        archive,
        "MAD",
    )
    source = plot.select(name="tw_session_spot_projection_source")[0]
    projection = pd.DataFrame(source.data)
    concordia = plot.select(name="full_earth_concordia")[0]
    concordia_source = concordia.data_source
    legend = plot.legend[0]

    assert len(projection) == 2
    assert len(concordia_source.data["age"]) == 2500
    assert max(concordia_source.data["age"]) == pytest.approx(4567.3)
    assert plot.x_range.start == 0
    assert plot.x_range.end < max(concordia_source.data["x"])
    assert len(plot.select(name="concordia_age_labels")) == 1
    assert legend in plot.below
    assert legend.orientation == "horizontal"
    assert legend.ncols == 2
    np.testing.assert_allclose(
        projection["y_pbpb_corrected"],
        projection["y_measured"] * 1.01,
    )
    slope = (0.055 - 0.71) / (1 / 0.08)
    np.testing.assert_allclose(
        0.71 + slope * projection["x_target"],
        projection["y_pbpb_corrected"],
    )
    np.testing.assert_allclose(
        projection["206Pb/238U_fractionation_factor"],
        projection["x_measured"] / projection["x_target"],
    )
    segment_renderers = [
        renderer
        for renderer in plot.renderers
        if isinstance(getattr(renderer, "glyph", None), Segment)
    ]
    assert {renderer.glyph.line_dash for renderer in segment_renderers} == {
        "dashed",
        "dotted",
    }


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
    widgets["Plateau filter"].value = "none"
    assert selected_filters["#2b74a8"].indices == [5, 6, 7, 8]
    widgets["Apply"].clicks += 1

    first_spot = experiment.loc[experiment["spot_names"] == "spot-a"].iloc[0]
    second_spot = experiment.loc[experiment["spot_names"] == "spot-b"].iloc[0]
    assert tuple(first_spot[["baseline_start_times", "baseline_end_times"]]) == (10.0, 12.0)
    assert tuple(first_spot[["plateau_start_times", "plateau_end_times"]]) == (5.0, 9.0)
    assert tuple(first_spot[["signal_start_times", "signal_end_times"]]) == (5.0, 27.0)
    assert first_spot["plateau_filter_method"] == "none"
    assert first_spot["plateau_filter_channel"] == "29Si"
    assert second_spot["plateau_filter_method"] == "excel_2sigma"
    assert tuple(second_spot[["baseline_start_times", "baseline_end_times"]]) == (100.0, 102.0)
    assert refreshed_calculations[-1][0] == "spot-a"
    assert refreshed_calculations[-1][1] == {
        "baseline": (10.0, 12.0),
        "plateau": (5.0, 9.0),
    }
    refreshed = refreshed_calculations[-1][2]
    assert refreshed.loc[refreshed["Time"] == 10.0, "29Si"].iloc[0] == 0.0


def test_selection_editor_autopick_inherits_long_plateau_import_settings():
    pytest.importorskip("panel")
    pytest.importorskip("jupyter_bokeh.widgets")
    signal = np.asarray([10.0] * 40 + [100.0] * 390 + [10.0] * 30)
    times = np.arange(signal.size, dtype=float)
    experiment = pd.DataFrame(
        {
            "spot_names": ["apatite 1"],
            "baseline_start_times": [20.0],
            "baseline_end_times": [35.0],
            "plateau_start_times": [45.0],
            "plateau_end_times": [410.0],
        }
    )
    experiment.attrs["selection_parameters"] = {
        "baseline_sweep_n": 15,
        "peak_sweep_n": 365,
        "threshold": 0.2,
        "filter_percentile": 90,
        "baseline_offset": 4,
        "plateau_offset": 2,
    }
    total_data = pd.DataFrame(
        {
            "spot_name": "apatite 1",
            "Time": times,
            "44Ca": signal,
        }
    )
    editor = visualization.selection_editor(
        experiment,
        total_data,
        primary_channel="44Ca",
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
    widgets["Selection module"].value = "threshold"
    widgets["Auto-pick plateau"].clicks += 1

    expected = io.select_signal_regions(
        signal,
        time=times,
        method="threshold",
        baseline_sweep_n=15,
        peak_sweep_n=365,
        threshold=0.2,
        filter_percentile=90,
        baseline_offset=4,
        plateau_offset=2,
    )
    expected_bounds = (
        times[expected.plateau_start],
        times[expected.plateau_end],
    )
    assert tuple(widgets["Plateau window (s)"].value) == expected_bounds
    assert expected.plateau_end - expected.plateau_start > 300


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


def test_concordia_keeps_full_earth_curve_but_data_focused_initial_range():
    data = pd.DataFrame(
        {
            "207Pb/235U": [0.24],
            "206Pb/238U": [0.038],
            "207Pb/235U_2e": [0.012],
            "206Pb/238U_2e": [0.0024],
            "err_corr": [0.65],
            "sample": ["synthetic"],
        }
    )

    plot = isoplot.concordia(data)
    ages, full_x, _full_y = isoplot.full_concordia_curve("Wetherill")

    assert ages[-1] == pytest.approx(isoplot.EARTH_AGE_MA)
    assert plot.x_range.end < full_x[-1]
    assert len(plot.select(name="full_earth_concordia")) == 1
    assert len(plot.select(name="concordia_age_labels")) == 1
    assert plot.x_range.js_property_callbacks
    assert plot.y_range.js_property_callbacks


def test_vector_pdf_export_normalizes_bokeh_svg_colors(monkeypatch, tmp_path):
    def fake_export_svg(_plot, *, filename, webdriver):
        Path(filename).write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
            '<rect width="10" height="10" fill="rgb(0 128 0 / 0.2)"/>'
            '</svg>',
            encoding="utf-8",
        )
        return [str(filename)]

    monkeypatch.setattr(bokeh.io, "export_svg", fake_export_svg)
    output = tmp_path / "plot.pdf"

    result = visualization.export_bokeh_vector(
        figure(width=100, height=100),
        output,
        webdriver=object(),
    )

    assert result == output.resolve()
    assert output.read_bytes().startswith(b"%PDF")


def test_depth_profile_editor_buttons_refresh_spot_and_diagram():
    rows = []
    for spot_name, offset in (("spot-a", 0.0), ("spot-b", 0.02)):
        for sweep in range(3):
            ratio_68 = 0.038 + offset + sweep * 0.0005
            rows.append(
                {
                    "spot_name": spot_name,
                    "sample": "synthetic",
                    "Time": float(sweep),
                    "206Pb/238U_fractionation_corrected": ratio_68,
                    "206Pb/238U_fractionation_corrected_2e": 0.001,
                    "207Pb/206Pb_fractionation_corrected": 0.047,
                    "207Pb/206Pb_fractionation_corrected_2e": 0.001,
                    "207Pb/235U_fractionation_corrected": 0.24 + offset,
                    "207Pb/235U_fractionation_corrected_2e": 0.008,
                    "238U/206Pb_fractionation_corrected": 1 / ratio_68,
                    "238U/206Pb_fractionation_corrected_2e": 0.7,
                    "err_corr": 0.5,
                }
            )

    editor = visualization.depth_profile_concordia_editor(pd.DataFrame(rows))
    interactive_layout = editor.object
    spot_controls = interactive_layout.children[0]
    isochron_controls = interactive_layout.children[1]
    plot = interactive_layout.children[4]
    previous_button, spot_selector, next_button, diagram_selector = (
        spot_controls.children
    )
    view_selector, previous_sweep, sweep_slider, next_sweep = (
        isochron_controls.children
    )

    assert plot.width == 1100
    assert plot.height == 500
    assert plot.width_policy == "fixed"
    assert plot.height_policy == "fixed"
    assert plot.title.text.startswith("spot-a | Wetherill")
    assert isinstance(editor, visualization.StandaloneBokehPlot)
    assert plot.output_backend == "svg"
    save_tool = plot.select(type=visualization.SaveTool)[0]
    assert save_tool.filename == "spot-a_Wetherill_depth_profile.svg"
    assert spot_selector.js_property_callbacks
    assert diagram_selector.js_property_callbacks
    assert previous_button.js_event_callbacks
    assert next_button.js_event_callbacks
    assert view_selector.disabled
    assert sweep_slider.disabled
    assert previous_sweep.disabled
    assert next_sweep.disabled
    assert len(
        list(interactive_layout.select({"name": "depth_profile_switch_data"}))
    ) == 1
    assert "application/vnd.jupyter.widget-view+json" not in editor._repr_html_()
    assert visualization.find_single_bokeh_plot(editor) is plot


def test_depth_profile_editor_exposes_selected_anchored_isochron_view():
    rows = []
    for sweep in range(3):
        ratio_68 = 0.04 + sweep * 0.001
        ratio_86 = 1.0 / ratio_68
        rows.append(
            {
                "spot_name": "apatite-1",
                "sample": "apatite",
                "signal_relative_time": float(sweep) * 0.25,
                "206Pb/238U_fractionation_corrected": ratio_68,
                "206Pb/238U_fractionation_corrected_2e": 0.001,
                "207Pb/206Pb_fractionation_corrected": 0.15,
                "207Pb/206Pb_fractionation_corrected_2e": 0.002,
                "207Pb/235U_fractionation_corrected": 0.8,
                "207Pb/235U_fractionation_corrected_2e": 0.02,
                "238U/206Pb_fractionation_corrected": ratio_86,
                "238U/206Pb_fractionation_corrected_2e": 0.6,
                "err_corr": 0.4,
                "tw_initial_207Pb_206Pb": 0.90,
                "tw_initial_207Pb_206Pb_1s": 0.001,
                "tw_line_slope": -0.03,
                "tw_line_intercept": 0.90,
                "tw_lower_intercept_age": 950.0 + sweep,
                "tw_lower_intercept_238U_206Pb": 7.5,
                "tw_lower_intercept_207Pb_206Pb": 0.075,
            }
        )

    editor = visualization.depth_profile_concordia_editor(
        pd.DataFrame(rows),
        initial_type="TW",
        initial_view="isochron",
    )
    layout = editor.object
    plot = layout.children[4]
    view_selector, previous_sweep, sweep_slider, next_sweep = (
        layout.children[1].children
    )

    assert not view_selector.disabled
    assert view_selector.active == 1
    assert not sweep_slider.disabled
    assert sweep_slider.end == 2
    assert previous_sweep.js_event_callbacks
    assert next_sweep.js_event_callbacks
    assert plot.js_event_callbacks
    for name in (
        "depth_profile_selected_ellipse_source",
        "depth_profile_common_pb_anchor_source",
        "depth_profile_selected_isochron_source",
        "depth_profile_intercept_source",
    ):
        assert len(list(layout.select({"name": name}))) == 1


def test_depth_age_profile_uses_signal_time_until_depth_is_calibrated():
    frame = pd.DataFrame(
        {
            "spot_name": ["MA1 31"] * 3,
            "Time": [100.0, 100.25, 100.50],
            "tw_lower_intercept_age": [950.0, 940.0, 930.0],
            "tw_lower_intercept_age_2s": [20.0, 22.0, 24.0],
        }
    )

    plot = visualization.plot_depth_age_profile(frame)

    assert plot.xaxis[0].axis_label == "Time from signal start (s)"
    assert plot.yaxis[0].axis_label == "Lower-intercept age (Ma)"
    assert plot.output_backend == "svg"
    save_tool = plot.select(type=visualization.SaveTool)[0]
    assert save_tool.filename == "MA1_31_TW_depth_age.svg"
    source = plot.renderers[0].data_source
    np.testing.assert_allclose(source.data["x"], [0.0, 0.25, 0.50])
    np.testing.assert_allclose(source.data["lower"], [930.0, 918.0, 906.0])
    np.testing.assert_allclose(source.data["upper"], [970.0, 962.0, 954.0])


def test_concordia_initial_range_includes_discordant_data_and_curve():
    data = pd.DataFrame(
        {
            "207Pb/235U": [0.2],
            "206Pb/238U": [0.2],
            "207Pb/235U_2e": [0.01],
            "206Pb/238U_2e": [0.01],
            "err_corr": [0.0],
            "sample": ["discordant"],
        }
    )

    plot = isoplot.concordia(data)
    curve = plot.select(name="full_earth_concordia")[0].data_source.data
    visible = (
        (curve["x"] >= plot.x_range.start)
        & (curve["x"] <= plot.x_range.end)
        & (curve["y"] >= plot.y_range.start)
        & (curve["y"] <= plot.y_range.end)
    )

    assert visible.any()
    assert plot.x_range.start <= 0.2 <= plot.x_range.end
    assert plot.y_range.start <= 0.2 <= plot.y_range.end
    assert len(plot.select(name="full_earth_concordia_source")) == 1
