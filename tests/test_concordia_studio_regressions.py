"""Concordia controls in the tabbed Studio workspace and device rendering."""

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from test_concordia_main_window_input import app, workspace, position
from test_concordia_sample_chips import results


def settle(app):
    for _ in range(8):
        app.processEvents()


@pytest.mark.parametrize("size", [(1440, 900), (1280, 800)])
def test_focus_controls_fold_restore_actual_panel_widths(workspace, app, size):
    window, review, canvas = workspace
    shell = window.window()
    shell.resize(*size)
    window.apply_default_layout()
    settle(app)
    before = window.workspace_splitter.sizes()
    width = canvas.width()
    assert window.sidebar_panel.isVisible() and window.inspector_panel.isVisible()
    assert before[0] > 26 and before[2] > 26
    QtTest.QTest.mouseClick(review._focus_buttons[0], QtCore.Qt.MouseButton.LeftButton)
    settle(app)
    assert window.workspace_splitter.sizes()[::2] == [26, 26]
    assert not window.sidebar_panel.isVisible() and not window.inspector_panel.isVisible()
    assert canvas.width() > width + 250
    window._apply_width_layout(force=True)
    window.apply_default_layout()
    window.persist_layout_state()
    settle(app)
    assert window.workspace_splitter.sizes()[::2] == [26, 26]
    # The native shell mirrors this same leaf QAction.
    window.plot_focus_action.trigger()
    settle(app)
    assert window.workspace_splitter.sizes() == before
    assert canvas.width() == width


def test_grid_focus_and_queued_repeat_cannot_hide_selection(workspace, app):
    window, review, canvas = workspace
    window.window().resize(1440, 900)
    window.apply_default_layout()
    settle(app)
    names = {p[0] for p in canvas._points[:3]}
    review.set_selected_spots(names)
    canvas.setFocus()
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_H)
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_H)
    assert not review.excluded_spots
    before = window.scientific_project_fingerprint()
    QtTest.QTest.mouseClick(review._grid_buttons[0], QtCore.Qt.MouseButton.LeftButton)
    assert app.focusWidget() is review._grid_buttons[0]
    QtTest.QTest.keyClick(app.focusWidget(), QtCore.Qt.Key.Key_H)
    # A repeat already queued by the platform must not reapply Hide.
    repeat = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_H,
                            QtCore.Qt.KeyboardModifier.NoModifier, "h", True)
    QtWidgets.QApplication.sendEvent(canvas, repeat)
    QtTest.QTest.mouseClick(review._focus_buttons[0], QtCore.Qt.MouseButton.LeftButton)
    settle(app)
    assert review.selected_spots == names and not review.excluded_spots
    assert window.scientific_project_fingerprint() == before
    assert window.workspace_splitter.sizes()[::2] == [26, 26]


def test_first_press_after_cursor_jump_selects_with_hover(workspace, app):
    window, review, canvas = workspace
    name, pos = position(canvas)
    canvas.scene().lastHoverEvent = None
    QtTest.QTest.mouseMove(canvas.viewport(), pos)
    settle(app)
    assert canvas._hover_text.isVisible()
    QtTest.QTest.mouseClick(canvas.viewport(), QtCore.Qt.MouseButton.LeftButton,
                          QtCore.Qt.KeyboardModifier.NoModifier, pos)
    settle(app)
    assert review.selected_spots == {name}
    assert app.focusWidget() is canvas


@pytest.mark.parametrize("size", [(1280, 800), (1440, 900)])
def test_samples_popup_long_names_stays_open_and_inside_window(workspace, app, size):
    window, review, canvas = workspace
    window.window().resize(*size)
    review.show_results(results([f"Long sample name {i:02d} " * 5 for i in range(19)] + ["NIST612"]))
    review.set_show_glasses(True)
    settle(app)
    area = review.spot_sample_chips
    button = area.menu_button
    button.showMenu()
    settle(app)
    popup = button.menu()
    bounds = QtCore.QRect(window.window().mapToGlobal(QtCore.QPoint()), window.window().size())
    assert bounds.contains(popup.geometry())
    assert popup.height() <= size[1] * .6
    glasses = next(b for b in popup.findChildren(QtWidgets.QToolButton) if b.text() == "Glasses")
    assert glasses.width() >= glasses.sizeHint().width()
    assert area.verticalScrollBar().maximum() > 0
    chip = next(chip for name, chip in area.chips.items() if name.startswith("Long sample"))
    assert chip.width() < chip.sizeHint().width()
    assert chip.text().rsplit(" ", 1)[0] in chip.toolTip()
    previous = dict(review.include_flags)
    QtTest.QTest.mouseClick(chip, QtCore.Qt.MouseButton.LeftButton)
    settle(app)
    assert review.include_flags != previous
    assert popup.isVisible()
    QtTest.QTest.keyClick(chip, QtCore.Qt.Key.Key_Escape)
    assert not popup.isVisible()


def test_vector_overlays_enable_antialiasing_and_reset_key(workspace, app, monkeypatch):
    window, review, canvas = workspace
    review.set_selected_spots({canvas._points[0][0]})
    canvas._set_hover(0)
    path = canvas._highlight.path()
    assert any(path.elementAt(i).isCurveTo() for i in range(path.elementCount()))
    assert canvas._highlight.pen().isCosmetic()
    items = (canvas._highlight, canvas._hover_ring, canvas._plot_item.vb._selection_box)
    hints = []
    def paint(self, painter, option, widget=None):
        hints.append(painter.testRenderHint(QtGui.QPainter.RenderHint.Antialiasing))
    monkeypatch.setattr(QtWidgets.QGraphicsPathItem, "paint", paint)
    monkeypatch.setattr(QtWidgets.QGraphicsRectItem, "paint", paint)
    for dpr in (1, 2):
        image = QtGui.QImage(80*dpr, 80*dpr, QtGui.QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(dpr)
        painter = QtGui.QPainter(image)
        for item in items:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
            item.paint(painter, QtWidgets.QStyleOptionGraphicsItem())
            assert item.cacheMode() == QtWidgets.QGraphicsItem.CacheMode.NoCache
        painter.end()
    assert hints == [True] * 6
    assert canvas._plot_item.buttonsHidden
    assert all(b.text() != "?" for b in review.spot_status_line.findChildren(QtWidgets.QToolButton))
    canvas._plot_item.vb.scaleBy((.5, .5))
    canvas.setFocus()
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_0)
    assert canvas._plot_item.vb.viewRange()[0] == pytest.approx(canvas._default_range[0])
