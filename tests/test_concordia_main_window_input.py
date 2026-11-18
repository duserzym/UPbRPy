"""Concordia mouse dispatch retains keyboard focus in the full workspace."""

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from GUI import main_window
from GUI.main_window import MainWindow
from GUI.studio_window import StudioWindow
from GUI.theme import build_application
from UPbRPy.isochron import wetherill_point


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.fixture(params=["main", "studio"])
def workspace(request, app, tmp_path, monkeypatch):
    settings = QtCore.QSettings(str(tmp_path / "input.ini"), QtCore.QSettings.Format.IniFormat)
    monkeypatch.setattr(QtCore, "QSettings", lambda *args, **kwargs: settings)
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    shell = StudioWindow(theme_mode="light") if request.param == "studio" else MainWindow(theme_mode="light")
    window = shell.active_reduction if request.param == "studio" else shell
    rich = Path(__file__).parent / "fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"
    window.load_experiment_directory(str(rich), background=False)
    names = window.session.spot_names
    x, y = wetherill_point(np.linspace(500., 1000., len(names)))
    frame = pd.DataFrame({
        "spot_name": names, "sample": [name.rsplit("_", 1)[0] for name in names],
        "207Pb/235U": x, "206Pb/238U": y,
        "207Pb/235U_2e": x * .01, "206Pb/238U_2e": y * .01,
    })
    window.session.reduction_cache["finalized_results"] = SimpleNamespace(
        selected_results=frame.copy(), parallel_results=frame)
    window.set_plot_mode("final_results", refresh=True)
    review = window.results_table
    review.tabs.setCurrentWidget(review.spot_concordia_page)
    window._apply_interaction_controls_to_current_plot()
    shell.resize(1280, 800)
    shell.show()
    for _ in range(8):
        app.processEvents()
    yield window, review, review.spot_concordia
    window.has_unsaved_changes = lambda: False
    shell.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def position(canvas, index=0):
    name, x, y, _ = canvas._points[index]
    scene = canvas._plot_item.vb.mapViewToScene(QtCore.QPointF(x, y))
    return name, canvas.mapFromScene(scene)


def click(canvas, app, index=0, modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
    name, pos = position(canvas, index)
    QtTest.QTest.mouseClick(canvas.viewport(), QtCore.Qt.MouseButton.LeftButton, modifiers, pos)
    app.processEvents()
    return name


def key(app, value, modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
    # Use the real focus owner, so dispatch cannot mask lost plot focus.
    target = app.focusWidget()
    assert target is not None
    QtTest.QTest.keyClick(target, value, modifiers)
    app.processEvents()


def test_single_click_shortcuts_and_card_mouse_buttons(workspace, app):
    window, review, canvas = workspace
    window.spot_list.setFocus()
    name = click(canvas, app)
    assert app.focusWidget() is canvas
    assert review.selected_spots == {name}
    assert review.spot_card.isVisible()
    assert window.spot_list.currentItem().data(0, QtCore.Qt.ItemDataRole.UserRole) == name
    key(app, QtCore.Qt.Key.Key_Escape)
    assert not review.selected_spots
    assert not review.spot_card.isVisible()
    click(canvas, app)
    key(app, QtCore.Qt.Key.Key_H)
    assert review.excluded_spots == {name}
    key(app, QtCore.Qt.Key.Key_H)
    assert not review.excluded_spots
    key(app, QtCore.Qt.Key.Key_E)
    assert review.excluded_spots == {name}
    review.set_spot_included({name}, False)
    key(app, QtCore.Qt.Key.Key_H, QtCore.Qt.KeyboardModifier.ShiftModifier)
    assert review.include_flags[name]
    assert review.excluded_spots == {name}
    click(canvas, app)
    key(app, QtCore.Qt.Key.Key_H)
    assert not review.excluded_spots
    QtTest.QTest.keySequence(app.focusWidget(), QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.SelectAll))
    assert review.selected_spots == {point[0] for point in canvas._points}
    key(app, QtCore.Qt.Key.Key_Escape)
    click(canvas, app)
    QtTest.QTest.keyPress(app.focusWidget(), QtCore.Qt.Key.Key_Space)
    assert canvas._space_pan
    assert canvas.interaction_mode() == "select"
    QtTest.QTest.keyRelease(app.focusWidget(), QtCore.Qt.Key.Key_Space)
    assert not canvas._space_pan

    card = review.spot_card
    for button in (card.more_button, card.exclude_button, card.close_button):
        assert button.focusPolicy() == QtCore.Qt.FocusPolicy.NoFocus
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        app.processEvents()
        assert app.focusWidget() is canvas
    assert card.more_button.isChecked()
    assert review.excluded_spots == {name}
    assert not card.isVisible()
    click(canvas, app, index=1)
    signals = QtTest.QSignalSpy(card.showSignalRequested)
    QtTest.QTest.mouseClick(card.show_signal_button, QtCore.Qt.MouseButton.LeftButton)
    assert signals.count() == 1
    assert window.session.plot_mode == "raw"


def test_focus_is_retained_after_linked_selection_callbacks(workspace, app):
    window, review, canvas = workspace
    # Exercise focus changes during synchronous selection dispatch, after the
    # ViewBox's initial setFocus (native platforms can transfer focus here).
    def follow_with_focus(_names):
        window.spot_list.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)

    review.selectedSpotsChanged.connect(follow_with_focus)
    click(canvas, app)
    assert app.focusWidget() is canvas
    key(app, QtCore.Qt.Key.Key_Escape)
    assert not review.selected_spots
    # An empty-space click must also retain focus after clearing a selection.
    click(canvas, app)
    vb = canvas._plot_item.vb
    pos = canvas.mapFromScene(vb.sceneBoundingRect().topRight() + QtCore.QPointF(-20, 20))
    QtTest.QTest.mouseClick(canvas.viewport(), QtCore.Qt.MouseButton.LeftButton,
                          QtCore.Qt.KeyboardModifier.NoModifier, pos)
    app.processEvents()
    assert not review.selected_spots
    assert app.focusWidget() is canvas


def test_modified_clicks_and_box_drag_keep_selection_mode(workspace, app):
    window, review, canvas = workspace
    vb = canvas._plot_item.vb
    before = vb.viewRange()
    first = click(canvas, app)
    second = click(canvas, app, 1, QtCore.Qt.KeyboardModifier.ShiftModifier)
    assert review.selected_spots == {first, second}
    click(canvas, app, 0, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert review.selected_spots == {second}
    key(app, QtCore.Qt.Key.Key_Escape)
    assert not review.selected_spots
    points = np.asarray([point[1:3] for point in canvas._points[:2]])
    margin = np.ptp(points, axis=0) * .1
    start = vb.mapFromView(QtCore.QPointF(*(points.min(axis=0) - margin)))
    end = vb.mapFromView(QtCore.QPointF(*(points.max(axis=0) + margin)))
    drag = SimpleNamespace(
        button=lambda: QtCore.Qt.MouseButton.LeftButton,
        buttonDownPos=lambda: start, pos=lambda: end,
        modifiers=lambda: QtCore.Qt.KeyboardModifier.NoModifier,
        isFinish=lambda: True, accept=lambda: None)
    vb.mouseDragEvent(drag)
    app.processEvents()
    assert review.selected_spots == {first, second}
    key(app, QtCore.Qt.Key.Key_H)
    assert review.excluded_spots == {first, second}
    key(app, QtCore.Qt.Key.Key_H)
    assert not review.excluded_spots
    key(app, QtCore.Qt.Key.Key_Escape)
    assert not review.selected_spots
    assert vb.viewRange() == before
    assert canvas.interaction_mode() == "select"
    assert window._selected_interaction_mode() == "select"
    assert review.mouse_mode_buttons["select"].isChecked()


def test_tree_keys_refresh_and_double_click_preserve_mouse_mode(workspace, app, monkeypatch):
    window, review, canvas = workspace
    vb = canvas._plot_item.vb
    # Refresh must read the canvas, even if a cached toolbar value is stale.
    window._interaction_mode_by_view["concordia"] = "pan"
    calls = []
    for target in (review.spot_concordia, review.depth_concordia):
        original = target.set_interaction_mode

        def record(mode, original=original):
            calls.append(mode)
            original(mode)

        monkeypatch.setattr(target, "set_interaction_mode", record)
    vb.setRange(xRange=(.5, 1.5), yRange=(.06, .16), padding=0)
    before = vb.viewRange()
    plot_item, scatter = canvas._plot_item, canvas._scatter
    window._apply_interaction_controls_to_current_plot()
    assert not calls
    assert window._interaction_mode_by_view["concordia"] == "select"
    window.spot_list.setFocus()
    refreshes = []
    monkeypatch.setattr(window, "refresh_view", lambda: refreshes.append(True))
    for value in (QtCore.Qt.Key.Key_P, QtCore.Qt.Key.Key_H, QtCore.Qt.Key.Key_E,
                  QtCore.Qt.Key.Key_Down, QtCore.Qt.Key.Key_Escape):
        key(app, value)
    assert not refreshes
    assert vb.viewRange() == before
    assert canvas._plot_item is plot_item and canvas._scatter is scatter
    assert canvas.interaction_mode() == "select" and not calls
    assert review.mouse_mode_buttons["select"].isChecked()

    # Two quick empty-space clicks may reset the range, but never the mode.
    pos = canvas.mapFromScene(vb.sceneBoundingRect().topRight() + QtCore.QPointF(-20, 20))
    QtTest.QTest.mouseDClick(canvas.viewport(), QtCore.Qt.MouseButton.LeftButton,
                           QtCore.Qt.KeyboardModifier.NoModifier, pos)
    app.processEvents()
    np.testing.assert_allclose(vb.viewRange(), canvas._default_range)
    assert canvas.interaction_mode() == "select" and not calls
    assert app.focusWidget() is canvas
    for mode in ("pan", "zoom", "select"):
        review.mouse_mode_buttons[mode].click()
        assert calls[-2:] == [mode, mode]
        assert canvas.interaction_mode() == mode
        assert window._selected_interaction_mode() == mode


def test_first_escape_after_context_menu_clears_selection(workspace, app):
    _window, review, canvas = workspace
    name = click(canvas, app)
    closed = []

    def dismiss():
        menu = app.activePopupWidget()
        if isinstance(menu, QtWidgets.QMenu):
            QtTest.QTest.keyClick(menu, QtCore.Qt.Key.Key_Escape)
            closed.append(True)

    QtCore.QTimer.singleShot(0, dismiss)
    QtTest.QTest.mouseClick(canvas.viewport(), QtCore.Qt.MouseButton.RightButton,
                          QtCore.Qt.KeyboardModifier.NoModifier, position(canvas)[1])
    app.processEvents()
    # Offscreen can activate the now-hidden popup after exec returns. Supply
    # the document activation normally delivered by the window manager, and
    # verify its remembered focus without explicitly focusing the canvas.
    canvas.window().activateWindow()
    app.processEvents()
    assert closed
    assert review.selected_spots == {name}
    assert app.focusWidget() is canvas
    key(app, QtCore.Qt.Key.Key_Escape)
    assert not review.selected_spots
