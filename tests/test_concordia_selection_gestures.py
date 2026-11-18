"""Concordia selection, calculation hiding and viewer display preferences."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from GUI import widgets
from GUI.theme import THEMES, build_application
from GUI.widgets import ConcordiaReviewWidget, ResultsTableWidget
from UPbRPy.isochron import tera_wasserburg_point, wetherill_point


NO = QtCore.Qt.KeyboardModifier.NoModifier
SHIFT = QtCore.Qt.KeyboardModifier.ShiftModifier
CONTROL = QtCore.Qt.KeyboardModifier.ControlModifier
META = QtCore.Qt.KeyboardModifier.MetaModifier
LEFT = QtCore.Qt.MouseButton.LeftButton
RIGHT = QtCore.Qt.MouseButton.RightButton


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.fixture
def finalized():
    ages = np.array([500., 700., 900.])
    x, y = wetherill_point(ages)
    tx, ty = tera_wasserburg_point(ages)
    frame = pd.DataFrame(dict(spot_name=["A_1", "A_2", "B_3"], sample=["A", "A", "B"],
                              **{"207Pb/235U": x, "206Pb/238U": y,
                                 "238U/206Pb": tx, "207Pb/206Pb": ty}))
    for ratio in ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb"):
        frame[ratio + "_2e_with_calibration"] = frame[ratio] * .02
        frame[ratio + "_2e_no_calibration"] = frame[ratio] * .01
    return SimpleNamespace(parallel_results=frame, selected_results=frame.copy())


def dispose(widget, app):
    widget.close()
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture
def review(app, finalized, tmp_path):
    settings = QtCore.QSettings(str(tmp_path / "viewer.ini"), QtCore.QSettings.Format.IniFormat)
    widget = ResultsTableWidget(settings=settings)
    widget.apply_theme(THEMES["light"])
    widget.show_results(finalized)
    depth = finalized.parallel_results.rename(columns={
        column: column.replace(ratio, ratio + "_fractionation_corrected")
        for column in finalized.parallel_results
        for ratio in ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb")
        if column.startswith(ratio)})
    widget.show_depth_profile(depth, "A_1")
    widget._depth_sample_choices = True
    widget.refresh_depth_concordia()
    widget.resize(1100, 650)
    widget.show()
    widget.tabs.setCurrentWidget(widget.spot_concordia_page)
    app.processEvents()
    yield widget
    dispose(widget, app)


def activate(review, kind, app):
    review.tabs.setCurrentWidget(getattr(review, kind + "_concordia_page"))
    app.processEvents()
    canvas = getattr(review, kind + "_concordia")
    canvas.restore_default_view()
    app.processEvents()
    return canvas


def centre(canvas, name):
    return next(QtCore.QPointF(x, y) for n, x, y, _ in canvas._points if n == name)


def pixel(canvas, xy):
    return canvas.mapFromScene(canvas._plot_item.vb.mapViewToScene(xy))


def click(canvas, xy, modifiers=NO, button=LEFT):
    QtTest.QTest.mouseMove(canvas.viewport(), pixel(canvas, xy))
    QtWidgets.QApplication.processEvents()
    QtTest.QTest.mouseClick(canvas.viewport(), button, modifiers, pixel(canvas, xy))
    QtWidgets.QApplication.processEvents()


def empty_centre(canvas):
    rect = canvas._plot_item.vb.viewRect()
    return QtCore.QPointF(rect.left() + .03 * rect.width(), rect.bottom() - .03 * rect.height())


@pytest.mark.parametrize("kind", ["spot", "depth"])
@pytest.mark.parametrize("mode", ["pan", "zoom", "select"])
def test_click_shift_command_and_empty_space(review, app, kind, mode):
    canvas = activate(review, kind, app)
    review.set_interaction_mode(mode)
    click(canvas, centre(canvas, "A_1"))
    assert review.selected_spots == {"A_1"}
    click(canvas, centre(canvas, "A_2"), SHIFT)
    assert review.selected_spots == {"A_1", "A_2"}
    click(canvas, centre(canvas, "A_1"), CONTROL)
    assert review.selected_spots == {"A_2"}
    click(canvas, centre(canvas, "B_3"), CONTROL)
    assert review.selected_spots == {"A_2", "B_3"}
    click(canvas, empty_centre(canvas), SHIFT)
    click(canvas, empty_centre(canvas), CONTROL)
    assert review.selected_spots == {"A_2", "B_3"}
    click(canvas, empty_centre(canvas))
    assert not review.selected_spots


class Drag:
    """A pyqtgraph drag in ViewBox coordinates, including intermediate moves."""

    def __init__(self, vb, start, end, modifiers=NO, *, finish=False):
        self.start = vb.mapFromView(start)
        self.end = vb.mapFromView(end)
        self._modifiers = modifiers
        self._finish = finish
        self.accepted = False

    def button(self):
        return LEFT

    def buttonDownPos(self):
        return self.start

    def pos(self):
        return self.end

    def modifiers(self):
        return self._modifiers

    def isFinish(self):
        return self._finish

    def accept(self):
        self.accepted = True


@pytest.mark.parametrize("kind", ["spot", "depth"])
@pytest.mark.parametrize("mode,modifiers", [("pan", SHIFT), ("zoom", SHIFT), ("select", NO),
                                             ("select", SHIFT), ("pan", CONTROL | SHIFT),
                                             ("zoom", CONTROL | SHIFT), ("select", CONTROL)])
def test_drag_selects_only_on_release_without_changing_range(review, app, monkeypatch, kind, mode, modifiers):
    canvas = activate(review, kind, app)
    review.set_interaction_mode(mode)
    review.set_selected_spots({"A_1", "B_3"})
    vb = canvas._plot_item.vb
    before = np.array(vb.viewRange())
    c = centre(canvas, "A_2")
    # Start in empty space; the rectangle encloses just the middle spot.
    dx, dy = np.ptp(before, axis=1) * .05
    start, end = c - QtCore.QPointF(dx, dy), c + QtCore.QPointF(dx, dy)
    calls = []
    select = canvas.select_rectangle

    def select_on_release(*args, **kwargs):
        calls.append(1)
        select(*args, **kwargs)

    monkeypatch.setattr(canvas, "select_rectangle", select_on_release)
    monkeypatch.setattr(canvas, "spots_at", lambda *a: pytest.fail("drag must not hit-test ellipses"))
    for fraction in np.linspace(.1, 1., 10):
        event = Drag(vb, start, start + (end - start) * fraction, modifiers)
        vb.mouseDragEvent(event)
        assert event.accepted and not calls
    assert vb._selection_box.isVisible()
    assert not vb.rbScaleBox.isVisible()
    assert vb._selection_box.pen().color() == QtGui.QColor(THEMES["light"].accent)
    assert 0 < vb._selection_box.brush().color().alphaF() < .3
    # Releasing modifiers mid-drag must not change the gesture's operation.
    vb.mouseDragEvent(Drag(vb, start, end, NO, finish=True))
    assert calls == [1]
    assert not vb._selection_box.isVisible()
    expected = ({"A_1", "A_2", "B_3"} if modifiers & (SHIFT | CONTROL) else {"A_2"})
    assert review.selected_spots == expected
    np.testing.assert_allclose(vb.viewRange(), before)
    if modifiers & CONTROL:
        # Toggle the same rectangle again, including duplicate depth rows by spot key.
        vb.mouseDragEvent(Drag(vb, start, end, modifiers, finish=True))
        assert review.selected_spots == {"A_1", "B_3"}


def test_select_drag_can_start_on_an_ellipse(review, app):
    canvas = activate(review, "spot", app)
    review.set_interaction_mode("select")
    vb = canvas._plot_item.vb
    start = centre(canvas, "A_1") * .999
    end = centre(canvas, "B_3") * 1.001
    assert canvas.spots_at(vb.mapViewToScene(start)) == ["A_1"]
    vb.mouseDragEvent(Drag(vb, start, end, finish=True))
    assert review.selected_spots == {"A_1", "A_2", "B_3"}


def test_rectangle_hit_set_uses_numpy_centres_and_only_reads_matching_keys(app, monkeypatch):
    canvas = ConcordiaReviewWidget()
    try:
        xy = np.column_stack((np.arange(6000), np.arange(6000)))
        canvas._point_xy = xy

        class IndexedPoints:
            def __iter__(self):
                pytest.fail("rectangle hit testing must not loop over every point")

            def __getitem__(self, index):
                return (f"spot_{index // 2}", *xy[index], None)

        canvas._points = IndexedPoints()
        selections = []
        monkeypatch.setattr(canvas, "select_spots", lambda names, **kw: selections.append((names, kw)))
        canvas.select_rectangle(QtCore.QRectF(120., 120., 3., 3.), toggle=True)
        assert selections == [({"spot_60", "spot_61"}, dict(extend=False, toggle=True))]
    finally:
        # Restore the retained point data before normal widget teardown.
        canvas._points = []
        dispose(canvas, app)


def intercept_canvas_menu(review, monkeypatch, choose):
    """Replace each menu's blocking exec; PySide caches it on the instance."""
    original = review._action_menu

    def action_menu(parent, kind):
        menu = original(parent, kind)
        menu.exec = lambda *args: choose(menu)
        return menu

    monkeypatch.setattr(review, "_action_menu", action_menu)


@pytest.mark.parametrize("kind", ["spot", "depth"])
def test_right_click_selects_then_hides_and_unhides_drawn_spot(review, app, monkeypatch, kind):
    canvas = activate(review, kind, app)
    review.set_selected_spots({"A_1", "A_2"})
    menus = []

    def choose_toggle(menu, *args):
        menus.append([a.text() for a in menu.actions() if not a.isSeparator()])
        menu.actions()[0].trigger()

    intercept_canvas_menu(review, monkeypatch, choose_toggle)
    click(canvas, centre(canvas, "B_3"), button=RIGHT)
    assert review.selected_spots == {"B_3"}
    assert review.excluded_spots == {"B_3"}
    assert all(review.include_flags.values())
    assert "B_3" in canvas.spots_at(canvas._plot_item.vb.mapViewToScene(centre(canvas, "B_3")))
    assert menus[-1] == ["Hide B_3 from calculations\tH", "Show only selected",
                         "Remove selected from plot", "Show all\tShift+H", "Fit selected",
                         "Select all shown\tCmd/Ctrl+A", "Clear selection\tEsc"]
    click(canvas, centre(canvas, "B_3"), button=RIGHT)
    assert menus[-1][0] == "Unhide B_3\tH"
    assert not review.excluded_spots
    review.set_selected_spots({"A_1", "A_2"})
    click(canvas, centre(canvas, "A_1"), button=RIGHT)
    assert review.selected_spots == {"A_1", "A_2"}
    assert menus[-1][0] == "Hide 2 spots from calculations\tH"
    assert review.excluded_spots == {"A_1", "A_2"}
    click(canvas, empty_centre(canvas), button=RIGHT)
    assert review.selected_spots == {"A_1", "A_2"}
    assert menus[-1][0] == "Unhide 2 spots\tH"
    assert not review.excluded_spots
    review.set_spot_included({"B_3"}, False)
    assert not canvas.spots_at(canvas._plot_item.vb.mapViewToScene(
        QtCore.QPointF(*wetherill_point(900.))))


@pytest.mark.parametrize("route", ["viewbox", "scatter"])
def test_macos_command_toggles_and_control_click_opens_context(review, app, monkeypatch, route):
    canvas = activate(review, "spot", app)
    contexts = []
    intercept_canvas_menu(review, monkeypatch, lambda menu: contexts.append(menu.actions()[0].text()))
    monkeypatch.setattr(widgets, "sys", SimpleNamespace(platform="darwin"))

    class Click:
        def button(self):
            return LEFT

        def scenePos(self):
            return canvas._plot_item.vb.mapViewToScene(centre(canvas, "B_3"))

        def modifiers(self):
            return self.mods

        def double(self):
            return False

        def accept(self):
            pass

    event = Click()
    review.set_selected_spots({"A_1"})
    event.mods = CONTROL
    if route == "viewbox":
        canvas._plot_item.vb.mouseClickEvent(event)
    else:
        canvas._spot_clicked(None, None, event)
    assert review.selected_spots == {"A_1", "B_3"} and not contexts
    event.mods = META
    if route == "viewbox":
        canvas._plot_item.vb.mouseClickEvent(event)
    else:
        canvas._spot_clicked(None, None, event)
    assert contexts == ["Hide 2 spots from calculations\tH"]
    assert review.selected_spots == {"A_1", "B_3"}


@pytest.mark.parametrize("kind", ["spot", "depth"])
def test_hide_keys_keep_spots_drawn_and_exclude_readout(review, app, kind):
    canvas = activate(review, kind, app)
    review.set_selected_spots({"A_1", "A_2"})
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_H)
    app.processEvents()
    assert review.excluded_spots == {"A_1", "A_2"}
    assert all(review.include_flags.values())
    assert review.concordia_readout is None
    assert {p[0] for p in canvas._points} == set(review.include_flags)
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_E)
    assert not review.excluded_spots
    review.set_spot_excluded({"A_1"}, True)
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_H)
    assert review.excluded_spots == {"A_1", "A_2"}  # mixed selection hides all
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_H)
    assert not review.excluded_spots
    review.exclude_selected()
    assert not review.include_flags["A_1"]
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_H, SHIFT)
    assert all(review.include_flags.values())
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_A, CONTROL)
    assert review.selected_spots == set(review.include_flags)
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_Escape)
    assert not review.selected_spots


def test_selection_actions_refresh_toggle_and_spot_card_wording(review, app):
    review.set_selected_spots({"A_1"})
    app.processEvents()
    assert review.spot_card.exclude_button.text() == "Hide"
    menu = review._action_menu(review, "spot")
    try:
        menu.aboutToShow.emit()
        assert menu.actions()[0].text() == "Hide A_1 from calculations\tH"
        review.spot_card.exclude_button.click()
        assert review.spot_card.exclude_button.text() == "Unhide"
        menu.aboutToShow.emit()
        assert menu.actions()[0].text() == "Unhide A_1\tH"
        menu.actions()[0].trigger()
        assert not review.excluded_spots
    finally:
        menu.deleteLater()


def test_grid_preference_survives_redraw_diagram_theme_and_new_viewer(review, app):
    assert review.concordia_grid()  # preserve the grid previously enabled by axis styling
    for enabled in (False, True, False):
        review.set_concordia_grid(enabled)
        for kind in ("spot", "depth"):
            canvas = activate(review, kind, app)
            for space in ("tera_wasserburg", "wetherill"):
                getattr(review, kind + "_diagram_buttons")[space].click()
                for theme in ("dark", "light"):
                    review.apply_theme(THEMES[theme])
                    app.processEvents()
                    assert canvas.grid_visible() == enabled
                    for axis_name in ("bottom", "left"):
                        axis = canvas._plot_item.getAxis(axis_name)
                        assert bool(axis.grid) == enabled
                        if enabled:
                            assert axis.grid == pytest.approx(255 * .25, abs=1)
                        assert axis.tickPen().color() == QtGui.QColor(THEMES[theme].axis_text)
    review._settings.sync()
    settings = QtCore.QSettings(review._settings.fileName(), QtCore.QSettings.Format.IniFormat)
    assert settings.value("review_concordia_grid_v1", type=bool) is False
    second = ResultsTableWidget(settings=settings)
    try:
        assert not second.concordia_grid()
        assert not second.spot_concordia.grid_visible()
        assert not second.depth_concordia.grid_visible()
    finally:
        dispose(second, app)


def test_toolbar_modes_grid_visibility_and_workspace_key(app, monkeypatch, finalized):
    from pathlib import Path
    from GUI import main_window
    from GUI.main_window import MainWindow

    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    window = MainWindow(theme_mode="light")
    try:
        rich = Path(__file__).parent / "fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"
        window.load_experiment_directory(str(rich), background=False)
        window.session.reduction_cache["finalized_results"] = finalized
        window.set_plot_mode("final_results", refresh=True)
        review = window.results_table
        review.tabs.setCurrentWidget(review.spot_concordia_page)
        window._apply_interaction_controls_to_current_plot()
        window._set_plot_controls_enabled(True)
        window.show()
        app.processEvents()
        assert window.concordia_grid_button.isVisible()
        for mode in ("select", "zoom", "pan"):
            review.mouse_mode_buttons[mode].click()
            assert window._selected_interaction_mode() == mode
            assert window._interaction_mode_by_view["concordia"] == mode
            for canvas in (review.spot_concordia, review.depth_concordia):
                assert canvas.interaction_mode() == mode
        for kind in ("spot", "depth"):
            review.tabs.setCurrentWidget(getattr(review, kind + "_concordia_page"))
            for mode in ("select", "pan", "zoom"):
                window.interact_mode_buttons[mode].click()
                assert review.mouse_mode_buttons[mode].isChecked()
                assert review.interaction_mode() == mode
        assert window.interact_mode_buttons["zoom"].text() == "Zoom"
        for enabled in (False, True):
            window.concordia_grid_button.setChecked(enabled)
            assert review.concordia_grid() == enabled
        review.set_selected_spots({"A_1"})
        QtTest.QTest.keyClick(review.depth_concordia, QtCore.Qt.Key.Key_H)
        workspace = window.project_workspace_state()
        assert workspace["review_excluded_spots"] == ["A_1"]
        assert not any("grid" in key for key in workspace)
        review.set_spot_excluded({"A_1"}, False)
        review.restore_review_state(workspace, spot_names=list(review.include_flags))
        assert review.excluded_spots == {"A_1"}
        review.tabs.setCurrentWidget(review.results_page)
        assert window.concordia_grid_button.isHidden()
        window.session.plot_mode = "raw"
        window._apply_interaction_controls_to_current_plot()
        assert window.concordia_grid_button.isHidden()
    finally:
        window.has_unsaved_changes = lambda: False
        dispose(window, app)
