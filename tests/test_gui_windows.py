"""Optional GUI tests for independent multi-session window management."""

from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6 import QtGui, QtWidgets  # noqa: E402

from GUI.icon import build_app_icon  # noqa: E402
from GUI.main import StudioWindowController  # noqa: E402
from GUI.theme import active_theme, build_application  # noqa: E402
from GUI.widgets import (  # noqa: E402
    DownholeFractionationWidget,
    ResultsTableWidget,
    SessionFractionationWidget,
    TracePlotWidget,
)


def test_window_controller_keeps_reduction_sessions_independent():
    """Changing one open reduction must not mutate another window's state."""
    application = build_application([], theme_mode="light")
    controller = StudioWindowController(
        theme_mode="light",
        app_icon=build_app_icon(),
    )
    first = controller.create_window(show=False)
    second = controller.create_window(show=False)

    first.session.primary_channel = "29Si"
    second.session.primary_channel = "44Ca"

    assert first.session is not second.session
    assert first.session.primary_channel == "29Si"
    assert second.session.primary_channel == "44Ca"
    assert len(controller.windows) == 2

    action_texts = {
        action.text()
        for action in first.findChildren(QtGui.QAction)
    }
    assert {
        "New Reduction Window",
        "Open Project in New Window…",
        "Open Experiment Folder in New Window…",
        "Next Reduction Window",
    } <= action_texts

    first.close()
    second.close()
    application.sendPostedEvents()
    application.processEvents()


def test_inspector_prioritizes_spot_and_channel_browsing():
    """High-frequency controls should not share space with calibration forms."""
    application = build_application([], theme_mode="light")
    controller = StudioWindowController(
        theme_mode="light",
        app_icon=build_app_icon(),
    )
    window = controller.create_window(show=False)
    window.resize(1780, 1050)
    window.show()
    application.processEvents()

    assert window.inspector_tabs.count() == 3
    assert [
        window.inspector_tabs.tabText(index)
        for index in range(window.inspector_tabs.count())
    ] == ["Browse", "Calibration", "Results"]
    assert window.inspector_panel.minimumWidth() >= 350
    assert not window.workspace_splitter.childrenCollapsible()
    assert window.workspace_splitter.handleWidth() >= 8
    assert window.spot_channel_splitter.count() == 2
    assert min(window.spot_channel_splitter.sizes()) >= 180
    assert window.header_card.height() <= 96
    assert window.workflow_context_bar.height() <= 66
    assert window.session_overview_plot.height() == 115
    assert window.save_project_button.text() == "Save project"
    assert window.export_results_button.text() == "Export results…"
    assert not window.save_project_button.isEnabled()
    assert not window.export_results_button.isEnabled()
    assert not window.repick_all_spots_button.isEnabled()
    assert window.repick_all_spots_button.text() == "Re-pick all spots"
    assert window.selection_method_combo.itemText(2) == (
        "Excel expanding mean + σ threshold"
    )

    channels_view = window.findChild(QtWidgets.QScrollArea, "ChannelsCard")
    assert channels_view is not None
    assert channels_view.height() >= 250

    window.close()
    application.sendPostedEvents()
    application.processEvents()


def test_trace_plot_layers_measurement_points_over_curves():
    """Raw sweep traces retain lines and expose each sampled observation."""
    application = build_application([], theme_mode="light")
    plot = TracePlotWidget()
    plot.apply_theme(active_theme(application))
    frame = pd.DataFrame(
        {
            "Time": [0.0, 1.0, 2.0, 3.0],
            "29Si": [10.0, 12.0, 11.0, 13.0],
            "238U": [2.0, 5.0, 4.0, 6.0],
        }
    )

    plot.show_spot(
        frame,
        ["29Si", "238U"],
        {"29Si": "#00885a", "238U": "#1f63cf"},
        {"baseline": None, "plateau": None, "signal": None},
    )

    measurement_layers = [
        item
        for item in plot.plot_item.listDataItems()
        if bool(item.property("measurement_points"))
    ]
    assert len(measurement_layers) == 2
    assert all(item.opts.get("symbol") == "o" for item in measurement_layers)
    assert all(item.zValue() > 2 for item in measurement_layers)

    plot.close()


def test_trace_plot_can_navigate_session_beyond_active_selection_spot():
    """Full-session traces must not contaminate active-spot statistics."""
    application = build_application([], theme_mode="light")
    plot = TracePlotWidget()
    full_session = pd.DataFrame(
        {
            "Time": np.arange(8.0),
            "29Si": [
                10.0,
                11.0,
                12.0,
                13.0,
                100.0,
                101.0,
                102.0,
                103.0,
            ],
        }
    )
    active_spot = full_session.iloc[:4].copy()
    plot.show_spot(
        full_session,
        ["29Si"],
        {"29Si": "#00885a"},
        {"baseline": (0.0, 3.0), "plateau": None, "signal": None},
        stats_channel="29Si",
        selection_frame=active_spot,
    )

    assert len(plot._hover_series["29Si"][0]) == len(full_session)
    assert len(plot._region_overlays["baseline"]["frame"]) == len(
        active_spot
    )
    assert plot._region_overlays["baseline"]["included_count"] == 3
    plot.deleteLater()
    application.processEvents()


def test_trace_selections_snap_and_show_filtered_plateau_points():
    """Live selection styling mirrors the notebook's accepted/rejected sweeps."""
    application = build_application([], theme_mode="light")
    plot = TracePlotWidget()
    plot.apply_theme(active_theme(application))
    values = np.array(
        [10.0, 10.2, 9.8, 10.1, 10.0, 20.0]
        + [100.0 + index for index in range(10)]
        + [40.0, 12.0]
    )
    values[11] = 1000.0
    frame = pd.DataFrame({"Time": np.arange(18.0), "29Si": values})
    changed = []
    plot.regionChanged.connect(lambda name, bounds: changed.append((name, bounds)))
    plot.show_spot(
        frame,
        ["29Si"],
        {"29Si": "#00885a"},
        {
            "baseline": (0.0, 5.0),
            "plateau": (6.0, 16.0),
            "signal": (5.0, 16.0),
        },
        editable_regions=True,
        stats_channel="29Si",
        plateau_filter_method="excel_2sigma",
    )

    plateau = plot._region_overlays["plateau"]
    assert plateau["included_count"] == 9
    assert plateau["excluded_count"] == 1
    assert len(plateau["excluded_points"].getData()[0]) == 1
    assert plateau["region"].span != (0, 1)
    assert 0.0 <= plateau["region"].span[0] < plateau["region"].span[1] <= 1.0
    assert plot._signal_markers["signal_start"].opts["symbol"] == "d"
    assert plot._signal_markers["signal_end"].opts["symbol"] == "s"

    plateau["region"].setRegion((5.2, 15.7))
    application.processEvents()
    assert tuple(plateau["region"].getRegion()) == (5.0, 16.0)
    assert changed and changed[-1][0] == "plateau"
    plot.close()


def test_fractionation_diagnostics_toggle_one_and_two_sigma_bands():
    """The shared sigma control must redraw both correction envelopes."""
    application = build_application([], theme_mode="light")
    theme = active_theme(application)
    downhole = DownholeFractionationWidget()
    downhole_data = {
        "ratio_name": "206Pb/238U",
        "known_ratio": 0.1,
        "primary_standard": "PL",
        "spot_times": [pd.Series([0.0, 1.0, 2.0]).to_numpy()],
        "spot_ratios": [pd.Series([0.10, 0.095, 0.09]).to_numpy()],
        "unified_time": pd.Series([0.0, 1.0, 2.0]).to_numpy(),
        "mean_frac": pd.Series([1.0, 1.02, 1.04]).to_numpy(),
        "std_frac": 0.01,
        "fit_label": "Pointwise mean",
    }
    downhole.show_data(downhole_data, theme)
    assert downhole.sigma_level() == 2
    assert any(
        bool(getattr(item, "uncertainty_band", False))
        for item in downhole._plot_item.items
    )
    downhole.set_sigma_level(1)
    assert downhole.sigma_level() == 1

    session = SessionFractionationWidget()
    session_data = {
        "ratio_name": "206Pb/238U",
        "primary_standard": "PL",
        "spot_mean_times": pd.Series([0.0, 10.0, 20.0]).to_numpy(),
        "spot_mean_fracs": pd.Series([1.0, 1.02, 0.99]).to_numpy(),
        "spot_frac_errs": pd.Series([0.01, 0.01, 0.01]).to_numpy(),
        "fit_times": pd.Series([0.0, 10.0, 20.0]).to_numpy(),
        "fit_vals": pd.Series([1.0, 1.005, 1.01]).to_numpy(),
        "fit_std": 0.015,
        "fit_label": "Polynomial",
        "show_uncertainty": True,
    }
    session.show_data(session_data, theme)
    assert any(
        bool(getattr(item, "uncertainty_band", False))
        for item in session._plot_item.items
    )
    session.set_sigma_level(1)
    assert session.sigma_level() == 1

    downhole.close()
    session.close()


def test_final_results_offer_selectable_native_concordia_reviews():
    """Spot and depth products keep tables and add lightweight plot controls."""
    application = build_application([], theme_mode="light")
    widget = ResultsTableWidget()
    widget.apply_theme(active_theme(application))
    spot_frame = pd.DataFrame(
        {
            "sample": ["unknown", "unknown"],
            "spot_name": ["spot_1", "spot_2"],
            "207Pb/235U": [0.20, 0.22],
            "207Pb/235U_2e_with_calibration": [0.02, 0.02],
            "206Pb/238U": [0.030, 0.032],
            "206Pb/238U_2e_with_calibration": [0.003, 0.003],
            "238U/206Pb": [33.3, 31.25],
            "238U/206Pb_2e_with_calibration": [3.0, 2.8],
            "207Pb/206Pb": [0.050, 0.052],
            "207Pb/206Pb_2e_with_calibration": [0.005, 0.005],
            "err_corr": [0.2, 0.25],
        }
    )
    decision = type(
        "Decision",
        (),
        {"common_pb_method": "none", "include_calibration_uncertainty": True},
    )()
    finalized = type(
        "Finalized",
        (),
        {
            "selected_results": spot_frame,
            "secondary_standard_summary": pd.DataFrame(),
            "parallel_results": spot_frame,
            "decision": decision,
        },
    )()
    widget.show_results(finalized)
    assert widget.tabs.count() == 5
    assert widget.spot_choice_list.count() == 2

    depth_frame = pd.DataFrame(
        {
            "sample": ["unknown", "unknown"],
            "spot_name": ["spot_1", "spot_1"],
            "signal_relative_time": [0.0, 1.0],
            "207Pb/235U_fractionation_corrected": [0.20, 0.21],
            "207Pb/235U_fractionation_corrected_2e_with_calibration": [0.02, 0.02],
            "206Pb/238U_fractionation_corrected": [0.030, 0.031],
            "206Pb/238U_fractionation_corrected_2e_with_calibration": [0.003, 0.003],
            "238U/206Pb_fractionation_corrected": [33.3, 32.3],
            "238U/206Pb_fractionation_corrected_2e_with_calibration": [3.0, 3.0],
            "207Pb/206Pb_fractionation_corrected": [0.050, 0.051],
            "207Pb/206Pb_fractionation_corrected_2e_with_calibration": [0.005, 0.005],
            "err_corr": [0.2, 0.2],
        }
    )
    widget.show_depth_profile(depth_frame, "spot_1")
    assert widget.depth_spot_combo.count() == 1
    widget.depth_diagram_buttons["tera_wasserburg"].click()
    assert widget.selected_diagram(widget.depth_diagram_buttons) == "tera_wasserburg"
    widget.tabs.setCurrentWidget(widget.depth_concordia_page)
    widget.set_zoom_mode("x")
    widget.set_interaction_mode("pan")
    assert widget.zoom_mode() == "x"
    assert widget.interaction_mode() == "pan"
    widget.close()
