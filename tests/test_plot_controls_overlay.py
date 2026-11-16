"""The drag / zoom / reset controls float inside the plot and can be moved."""

from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtTest import QTest

from GUI import main_window
from GUI.main_window import MainWindow
from GUI.theme import build_application


RICH = Path(__file__).parent / "fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


def settle(app):
    for _ in range(6):
        app.processEvents()


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    window = MainWindow(theme_mode="light")
    window.resize(1440, 900)
    window.show()
    window.load_experiment_directory(str(RICH), background=False)
    settle(app)
    yield window
    window.has_unsaved_changes = lambda: False
    window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def test_controls_float_inside_the_canvas_at_the_top_right(window):
    scroll, stack = window.plot_control_scroll, window.plots_stack
    assert scroll.parentWidget() is stack
    geometry = scroll.geometry()
    assert stack.rect().contains(geometry)
    assert stack.width() - geometry.right() < 30 and geometry.top() < 20
    # The spot name moves clear of the controls.
    label = window.trace_plot._spot_name_label
    if label.isVisible():
        assert not QtCore.QRect(label.mapTo(window.trace_plot, QtCore.QPoint()), label.size()).intersects(
            QtCore.QRect(scroll.mapTo(window.trace_plot, QtCore.QPoint()), scroll.size()))


def drag(window, app, delta):
    grip = window.plot_control_grip
    start = grip.rect().center()
    QTest.mousePress(grip, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier, start)
    event = QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseMove, QtCore.QPointF(start + delta),
        QtCore.QPointF(grip.mapToGlobal(start + delta)), QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)
    QtWidgets.QApplication.sendEvent(grip, event)
    QTest.mouseRelease(grip, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier, start + delta)
    settle(app)



def test_dragging_moves_and_remembers_the_place_per_kind_of_view(window, app):
    scroll, stack = window.plot_control_scroll, window.plots_stack
    before = scroll.pos()
    drag(window, app, QtCore.QPoint(-300, stack.height()))  # towards the bottom left
    assert scroll.pos() != before
    corner, _dx, dy = window._plot_control_anchors["plots"]
    assert corner.startswith("bottom")
    # Resizing keeps it at that corner.
    window.resize(1280, 800)
    settle(app)
    assert stack.height() - scroll.geometry().bottom() - 1 == dy
    # Concordia views keep their own place (default: top right).
    window.set_plot_mode("final_results", refresh=False)
    window._position_plot_controls()
    assert scroll.geometry().top() < 20
    window.set_plot_mode("raw", refresh=False)
    window._position_plot_controls()
    assert stack.height() - scroll.geometry().bottom() - 1 == dy


def test_controls_can_stand_in_a_column_and_reset(window, app):
    scroll, bar = window.plot_control_scroll, window.plot_control_bar
    window.set_plot_controls_vertical(True)
    settle(app)
    assert bar.layout().direction() == QtWidgets.QBoxLayout.Direction.TopToBottom
    assert scroll.height() > scroll.width()
    assert window.plots_stack.rect().contains(scroll.geometry())
    saved = window.settings.value("plot_controls_v1", "", type=str)
    assert '"vertical": true' in saved
    window.reset_plot_controls()
    settle(app)
    assert bar.layout().direction() == QtWidgets.QBoxLayout.Direction.LeftToRight
    assert scroll.width() > scroll.height()
    assert window._plot_control_anchors == {}
