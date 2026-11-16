"""Trace labels avoid floating controls after responsive layout changes."""

from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI import main_window
from GUI.main_window import MainWindow
from GUI.theme import build_application


RICH = Path(__file__).parent / "fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"
SIZES = ((800, 720), (1280, 800), (1512, 982))


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


def settle(app):
    for _ in range(15):
        app.processEvents()


@pytest.fixture(params=SIZES)
def window(app, monkeypatch, request):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    settings = QtCore.QSettings("UPbRPy", "Studio")
    saved = {key: settings.value(key) for key in settings.allKeys()}
    settings.clear()
    window = MainWindow(theme_mode="light")
    window.resize(*request.param)
    window.show()
    window.load_experiment_directory(str(RICH), background=False)
    settle(app)
    try:
        yield window
    finally:
        window.has_unsaved_changes = lambda: False
        window.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
        settings.clear()
        for key, value in saved.items():
            settings.setValue(key, value)


def assert_labels_clear(window):
    trace, scroll = window.trace_plot, window.plot_control_scroll
    # Map independently through global coordinates: mapTo(trace) is invalid
    # for the controls, since trace is a sibling rather than an ancestor.
    pill = QtCore.QRect(trace.mapFromGlobal(scroll.mapToGlobal(QtCore.QPoint())), scroll.size())
    assert trace._overlay_rect == pill
    spot_label = trace._spot_name_label
    assert spot_label.isVisible()
    spot_rect = QtCore.QRect(spot_label.mapTo(trace, QtCore.QPoint()), spot_label.size())
    assert trace.rect().contains(spot_rect)
    assert not spot_rect.intersects(pill), (spot_rect, pill)
    scene_pill = trace.mapToScene(pill).boundingRect()
    visible = []
    for name, line in trace._signal_boundaries.items():
        if line.label.isVisible():
            visible.append(name)
            rect = line.label.sceneBoundingRect()
            assert not rect.intersects(scene_pill), (name, rect, scene_pill)
    assert set(visible) == {"signal_start", "signal_end"}
    # Settled geometry must not keep scheduling label/layout work.
    assert not window._plot_control_overlay_timer.isActive()
    assert not trace._overlay_label_timer.isActive()


def test_labels_avoid_controls_after_moves_resizes_and_content_changes(window, app):
    initial_size = window.size()
    assert_labels_clear(window)
    for point in (QtCore.QPoint(0, 0), QtCore.QPoint(100, 0), QtCore.QPoint(250, 28),
                  window.plots_stack.rect().bottomRight()):
        window._move_plot_controls(point)
        window._remember_plot_control_place()
        settle(app)
        assert_labels_clear(window)

    for size in SIZES:
        window.resize(*size)
        settle(app)
        assert_labels_clear(window)

    window.resize(initial_size)
    window.reset_plot_controls()
    settle(app)
    assert_labels_clear(window)
    for mode in ("dark", "light"):
        window.apply_theme_mode(mode, persist=False)
        window.go_to_adjacent_spot(1)
        settle(app)
        assert_labels_clear(window)

    window.set_plot_controls_vertical(True)
    settle(app)
    assert_labels_clear(window)
    window._move_plot_controls(QtCore.QPoint(140, 0))
    window._remember_plot_control_place()
    settle(app)
    assert_labels_clear(window)
    window.reset_plot_controls()
    settle(app)
    assert_labels_clear(window)
