"""Switching workflow cards must not recalculate, save or redraw unchanged plots.

With the results view showing, each card switch used to rebuild the results
table, grouped trees and concordia (1.3 s with 283 analyses).
"""

import copy
from pathlib import Path
import time

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets
from shiboken6 import isValid

from GUI import main_window
from GUI.main_window import MainWindow
from GUI.theme import build_application
from test_export_table import synthetic_pipeline


RICH = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice/Zircon_04Nov26_slice_Rich.csv"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


def wait_for(app, condition, timeout=60):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert condition(), "Timed out"


def idle(app, window):
    wait_for(app, lambda: window._active_worker is None and not window.pending_region_updates)


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args[1:]))
    window = MainWindow(theme_mode="light")
    window.resize(1280, 800)
    window.show()
    window.load_experiment_directory(str(RICH), background=False)
    idle(app, window)
    # The slice fixture lacks 202Hg, so show synthetic finalized results.
    window.session.reduction_cache["finalized_results"] = synthetic_pipeline().run_finalize()
    window.set_plot_mode("final_results", refresh=True)
    app.processEvents()
    yield window
    if isValid(window):
        idle(app, window)
        window.has_unsaved_changes = lambda: False
        window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
    assert not errors


class Calls:
    def __init__(self, monkeypatch, window):
        self.names = []
        for owner, name in (
            (window, "run_background"),
            (window, "_write_project"),
            (window, "_autosave_selection_edits"),
            (window, "_recompute_selection_stages"),
            (window.results_table, "show_results"),
            (window.trace_plot, "show_spot"),
            (window.trace_plot, "show_series"),
            (window.downhole_frac_plot, "show_data"),
            (window.session_frac_plot, "show_data"),
        ):
            original = getattr(owner, name)

            def record(*args, _name=name, _original=original, **kwargs):
                self.names.append(_name)
                return _original(*args, **kwargs)

            monkeypatch.setattr(owner, name, record)


def switch(app, window, row):
    started = time.perf_counter()
    window.workflow_step_list.setCurrentRow(row)
    app.processEvents()
    return time.perf_counter() - started


def test_card_switch_does_no_recalculation_save_or_redraw(window, app, monkeypatch):
    calls = Calls(monkeypatch, window)
    rows = [row for row in range(2, window.workflow_step_list.count())
            if row != window.workflow_step_list.currentRow()]
    durations = [switch(app, window, row) for row in rows]
    assert calls.names == []
    assert window.session.plot_mode == "final_results"
    assert window.step_actions_title.text().startswith(f"{rows[-1] + 1} ·")
    # Generous bound; the results view alone took 0.5–1.3 s per switch
    # with full sessions before.
    assert max(durations) < 0.25, durations


def test_card_switch_redraws_when_a_result_was_recalculated(window, app, monkeypatch):
    switch(app, window, 3)
    calls = Calls(monkeypatch, window)
    cache = window.session.reduction_cache
    # A background recalculation replaces the cached result object.
    cache["finalized_results"] = copy.copy(cache["finalized_results"])
    switch(app, window, 4)
    assert calls.names.count("show_results") == 1
    switch(app, window, 5)
    assert calls.names.count("show_results") == 1


def test_switch_to_selections_shows_editable_raw_traces(window, app, monkeypatch):
    calls = Calls(monkeypatch, window)
    switch(app, window, 1)
    assert window.session.plot_mode == "raw"
    assert calls.names.count("show_spot") == 1
    assert window.trace_plot._region_overlays["plateau"]["region"].movable
    # Import keeps the same raw, editable view: no redraw.
    switch(app, window, 0)
    assert calls.names.count("show_spot") == 1
    # Baseline makes the windows read-only, which needs one redraw.
    switch(app, window, 2)
    assert calls.names.count("show_spot") == 2
    assert not window.trace_plot._region_overlays["plateau"]["region"].movable
    assert "run_background" not in calls.names
