"""Concordia views open in Pan; live isochron fits start off for zircon only."""

from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI import main_window
from GUI.main_window import MainWindow
from GUI.theme import build_application
from GUI.workflows import WORKFLOW_PRESETS, get_workflow_preset


RICH = Path(__file__).parent / "fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args[1:]))
    window = MainWindow(theme_mode="light")
    window.resize(1280, 800)
    window.show()
    yield window
    window.has_unsaved_changes = lambda: False
    window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
    assert not errors


def fit_states(window):
    return {kind: panel.fit_button.isChecked() for kind, panel in window.results_table.fit_panels.items()}


def test_presets_default_live_fits_on_only_for_common_pb_phases():
    assert not get_workflow_preset("zircon").live_fit_by_default
    for key in ("apatite", "titanite", "rutile"):
        assert get_workflow_preset(key).live_fit_by_default
    assert {key for key, preset in WORKFLOW_PRESETS.items() if preset.live_fit_by_default} == {
        key for key, preset in WORKFLOW_PRESETS.items() if preset.common_pb_calibration != "radiogenic"}


def test_new_zircon_session_opens_concordia_in_pan_with_fits_off(window, app):
    # A per-user preference left on (for example from an apatite session).
    for panel in window.results_table.fit_panels.values():
        panel.fit_button.setChecked(True)
    window.load_experiment_directory(str(RICH), background=False)
    assert window.workflow_preset.key == "zircon"
    assert fit_states(window) == {"spot": False, "depth": False}
    window.set_plot_mode("final_results", refresh=False)
    window.results_table.tabs.setCurrentWidget(window.results_table.spot_concordia_page)
    window._apply_interaction_controls_to_current_plot()
    assert window._selected_interaction_mode() == "pan"
    assert window.results_table.spot_concordia.interaction_mode() == "pan"
    # Box stays available, and trace plots keep Box.
    window.set_interaction_mode("zoom")
    assert window.results_table.spot_concordia.interaction_mode() == "zoom"
    window.set_plot_mode("raw", refresh=False)
    window._apply_interaction_controls_to_current_plot()
    assert window._selected_interaction_mode() == "zoom"
    window.set_interaction_mode("pan")
    window.set_plot_mode("final_results", refresh=False)
    window._apply_interaction_controls_to_current_plot()
    assert window._selected_interaction_mode() == "zoom"  # the concordia choice is kept
    window.set_plot_mode("raw", refresh=False)
    window._apply_interaction_controls_to_current_plot()
    assert window._selected_interaction_mode() == "pan"


def test_new_apatite_session_keeps_fits_on(window):
    window.load_experiment_directory(str(RICH), background=False)
    assert fit_states(window) == {"spot": False, "depth": False}
    # The slice is zircon data; install it as a new session under the
    # apatite preset, as an apatite import does.
    window.workflow_preset = get_workflow_preset("apatite")
    window.install_loaded_session(window.session, str(RICH))
    assert fit_states(window) == {"spot": True, "depth": True}


def test_saved_project_keeps_its_fit_state(window, app, tmp_path):
    window.load_experiment_directory(str(RICH), background=False)
    window.results_table.fit_panels["spot"].fit_button.setChecked(True)
    project = tmp_path / "fit-on.upbr"
    window.save_project(project)
    window.load_project_file(project, background=False)
    assert fit_states(window)["spot"] is True
