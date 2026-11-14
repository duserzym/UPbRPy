"""A new reduction contains no acquisition or example data."""

from pathlib import Path
import time

import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI import main_window
from GUI.main_window import MainWindow, PLOT_MODE_LABELS
from GUI.state import ReductionSession
from GUI.studio_window import StudioWindow
from GUI.theme import build_application

RICH = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice/Zircon_04Nov26_slice_Rich.csv"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


@pytest.fixture(params=["standalone", "studio"])
def window(request, app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    settings = QtCore.QSettings("UPbRPy", "Studio")
    settings.clear()
    shell = MainWindow() if request.param == "standalone" else StudioWindow()
    shell.resize(1280, 800)
    shell.show()
    for _ in range(6):
        app.processEvents()
    reduction = shell if isinstance(shell, MainWindow) else shell.active_reduction
    yield reduction, shell
    reduction.has_unsaved_changes = lambda: False
    shell.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def assert_blank(window):
    assert not window.session.loaded
    assert window.session.total_data.empty
    assert window.session.experiment.empty
    for plot in (window.trace_plot, window.session_overview_plot):
        assert not plot.plot_item.listDataItems()
        assert not plot.plot_item.items
        legend = plot.plot_item.legend
        assert legend is None or not legend.items
        assert plot.plot_item.getAxis("bottom").labelText
        assert plot.plot_item.getAxis("left").labelText
    assert not window.trace_plot._hover_series
    assert not window.trace_plot._region_overlays
    assert not window.trace_plot._signal_boundaries
    assert window.session_overview_plot._window_region is None
    assert window.spot_list.topLevelItemCount() == 0
    assert window.channel_list.count() == 0
    assert not window.isotope_grid._buttons
    assert window.results_table._spot_frame.empty
    assert window.results_table._depth_frame.empty
    for model in (window.results_table.results_model, window.results_table.secondary_model,
                  window.results_table.depth_model):
        assert model.rowCount() == 0
    for model in (window.results_table.results_group_model, window.results_table.secondary_group_model):
        assert model.rowCount() == 0
    assert window.empty_state_overlay.isVisible()
    assert window.empty_import_button.isVisible()
    assert window.empty_import_button.text() == "Import Rich CSV…"
    assert window.plots_stack.rect().contains(window.empty_state_overlay.geometry())
    for action in [*window._session_actions, *window.selection_edit_actions,
                   window.submit_to_database_action, window.prefer_run_action,
                   window.save_project_button, window.export_results_button,
                   window.repick_all_spots_button]:
        assert not action.isEnabled(), action
        if action in (window.submit_to_database_action, window.prefer_run_action):
            # A missing database extra is the more useful reason.
            assert action.toolTip() == "Load a session first" or "[database]" in action.toolTip()
        else:
            assert action.toolTip() == "Load a session first"


@pytest.mark.isolated
def test_blank_navigation_has_no_data_or_redraw(window, app, monkeypatch, capsys):
    reduction, shell = window
    assert_blank(reduction)
    calls = []
    for owner, name in ((reduction.trace_plot, "show_empty"),
                        (reduction.session_overview_plot, "show_empty"),
                        (reduction, "_render_current_plot"),
                        (reduction.results_table, "show_results")):
        original = getattr(owner, name)

        def record(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, name, record)
    durations = []
    for size in ((1280, 800), (1440, 900)):
        shell.resize(*size)
        for row in range(reduction.workflow_step_list.count()):
            started = time.perf_counter()
            reduction.workflow_step_list.setCurrentRow(row)
            app.processEvents()
            durations.append(time.perf_counter() - started)
        for mode in PLOT_MODE_LABELS:
            reduction.set_plot_mode(mode, refresh=True)
            app.processEvents()
        for tabs in (reduction.inspector_tabs, reduction.results_table.tabs):
            for index in range(tabs.count()):
                tabs.setCurrentIndex(index)
                app.processEvents()
        reduction.restore_default_main_view()
        assert_blank(reduction)
    assert not calls
    assert max(durations) < 0.25
    assert "Traceback" not in capsys.readouterr().err
    assert "Traceback" not in reduction.log_view.toPlainText()
    assert not reduction._plot_control_overlay_timer.isActive()
    assert not reduction.trace_plot._overlay_label_timer.isActive()


def test_load_hides_overlay_and_return_to_blank_clears(window, app):
    reduction, _shell = window
    reduction.load_experiment_directory(str(RICH), background=False)
    app.processEvents()
    assert reduction.session.loaded
    assert not reduction.empty_state_overlay.isVisible()
    assert reduction.trace_plot.plot_item.listDataItems()
    # Returning a tab to a blank session must clear displayed tables too.
    reduction.results_table.results_model.set_frame(pd.DataFrame({"spot_name": ["real"]}))
    reduction.session = ReductionSession()
    reduction.refresh_view()
    app.processEvents()
    assert_blank(reduction)


def test_blank_direct_actions_report_reason(window):
    reduction, _shell = window
    for action in (reduction.save_project, reduction.choose_reduction_bundle_export,
                   reduction.run_baseline_preview, reduction.run_final_review,
                   reduction.run_depth_profile_review, reduction.run_autopick_all_spots):
        action()
        assert reduction.statusBar().currentMessage() == "Load a session first"
