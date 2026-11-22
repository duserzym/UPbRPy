"""The drag / zoom / reset controls float inside the plot and can be moved."""

import json
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
def window(app, monkeypatch, request):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    settings = QtCore.QSettings("UPbRPy", "Studio")
    saved_layout = settings.value("plot_controls_v1")
    settings.remove("plot_controls_v1")
    if layout := getattr(request, "param", None):
        settings.setValue("plot_controls_v1", json.dumps(layout))
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
    if saved_layout is None:
        settings.remove("plot_controls_v1")
    else:
        settings.setValue("plot_controls_v1", saved_layout)


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


def test_controls_default_to_a_column_and_can_switch_to_a_row(window, app):
    scroll, bar = window.plot_control_scroll, window.plot_control_bar
    assert bar.layout().direction() == QtWidgets.QBoxLayout.Direction.TopToBottom
    assert scroll.height() > scroll.width()
    assert window.plots_stack.rect().contains(scroll.geometry())

    window.set_plot_controls_vertical(False)
    settle(app)
    assert bar.layout().direction() == QtWidgets.QBoxLayout.Direction.LeftToRight
    assert scroll.width() > scroll.height()
    saved = json.loads(window.settings.value("plot_controls_v1", "", type=str))
    assert saved["orientation"] == "horizontal"


@pytest.mark.parametrize(("window", "direction"), [
    ({"vertical": False, "anchors": {"plots": ["top_right", 45, 42]}},
     QtWidgets.QBoxLayout.Direction.TopToBottom),
    ({"orientation": "horizontal", "anchors": {"plots": ["top_right", 45, 42]}},
     QtWidgets.QBoxLayout.Direction.LeftToRight),
], indirect=["window"], ids=["legacy-row", "chosen-row"])
def test_saved_position_is_restored_with_the_current_orientation(window, direction):
    scroll, stack = window.plot_control_scroll, window.plots_stack
    bar = window.plot_control_bar
    assert bar.layout().direction() == direction
    assert bar.rect().contains(window.sigma_toggle_button.geometry())
    assert scroll.pos() == QtCore.QPoint(stack.width() - scroll.width() - 45, 42)


def test_column_resizes_when_a_ratio_picker_appears(window, app):
    window.resize(1280, 800)
    settle(app)
    scroll = window.plot_control_scroll
    viewport = scroll.viewport()
    original = scroll.geometry()
    window.set_plot_mode("raw_ratios", refresh=False)
    settle(app)
    picker = window.ratio_combo
    assert picker.isVisible()
    rect = QtCore.QRect(picker.mapTo(viewport, QtCore.QPoint()), picker.size())
    assert viewport.rect().contains(rect)
    window.set_plot_mode("raw", refresh=False)
    settle(app)
    assert not picker.isVisible()
    assert scroll.geometry() == original


@pytest.mark.parametrize("vertical", [True, False])
def test_controls_keep_their_dragged_position_when_switching_spots(window, app, vertical):
    window.resize(1280, 800)
    window.set_plot_controls_vertical(vertical)
    settle(app)
    scroll = window.plot_control_scroll
    original = scroll.pos()
    drag(window, app, QtCore.QPoint(-120, 110))
    dragged = scroll.geometry()
    assert dragged.topLeft() != original
    saved = window.settings.value("plot_controls_v1", "", type=str)
    first = window.current_spot_name()

    window.go_to_adjacent_spot(1)
    settle(app)
    assert window.current_spot_name() != first
    assert scroll.geometry() == dragged
    window.go_to_adjacent_spot(-1)
    settle(app)
    assert window.current_spot_name() == first
    assert scroll.geometry() == dragged
    assert window.settings.value("plot_controls_v1", "", type=str) == saved

    window.reset_plot_controls()
    settle(app)
    assert scroll.pos() == original
    assert (scroll.height() > scroll.width()) == vertical
    assert window._plot_control_anchors == {}
