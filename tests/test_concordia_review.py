"""Native concordia ranges, age labels, hover metadata and sample colours."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
import pyqtgraph as pg
import shiboken6

from GUI.theme import THEMES, build_application, theme_color
from GUI.widgets import ConcordiaReviewWidget, ResultsTableWidget
from UPbRPy.isochron import tera_wasserburg_point, wetherill_point


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.fixture
def review(app):
    ages = np.array([300., 500., 700., 900.])
    x, y = wetherill_point(ages)
    tx, ty = tera_wasserburg_point(ages)
    frame = pd.DataFrame({
        "spot_name": ["Z_10", "B_11", "A_12", "B_13"],
        "sample": ["Z", "B", "A", "B"],
        "207Pb/235U": x, "206Pb/238U": y,
        "238U/206Pb": tx, "207Pb/206Pb": ty,
        "207Pb/235U_2e": [.01] * 4, "206Pb/238U_2e": [.002] * 4,
        "238U/206Pb_2e": [.1] * 4, "207Pb/206Pb_2e": [.001] * 4,
        "206Pb/238U_age": [12.34] * 4, "206Pb/238U_age_2e": [.21] * 4,
        "207Pb/235U_age": [563.9] * 4, "207Pb/235U_age_2e": [24.] * 4,
        "207Pb/206Pb_age": [563.9] * 4, "207Pb/206Pb_age_2e": [24.] * 4,
        "disc%": [12.345] * 4, "U": [123.456] * 4,
        "Th": [45.678] * 4, "Th/U": [.123456] * 4,
    })
    selected = frame.copy()
    selected["preferred_date"] = 530.
    selected["preferred_date_2e"] = 19.
    selected["preferred_date_method"] = "206Pb/238U"
    selected["preferred_date_pass"] = False
    selected["automatic_flags"] = "high discordance"
    finalized = SimpleNamespace(parallel_results=frame, selected_results=selected)
    widget = ResultsTableWidget()
    widget.apply_theme(THEMES["light"])
    widget.show_results(finalized)
    widget.tabs.setCurrentWidget(widget.spot_concordia_page)
    widget.resize(1100, 700)
    widget.show()
    app.processEvents()
    yield widget, finalized
    widget.close()
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def set_age_range(plot, low, high):
    point = wetherill_point if plot._diagram == "wetherill" else tera_wasserburg_point
    x, y = point(np.array([low, high]))
    plot._plot_item.setRange(xRange=sorted(x), yRange=sorted(y), padding=0)


@pytest.mark.parametrize("diagram", ["wetherill", "tera_wasserburg"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_dynamic_age_labels_and_real_ratio_axes(review, app, diagram, theme):
    widget, _ = review
    widget.apply_theme(THEMES[theme])
    widget.spot_diagram_buttons[diagram].click()
    plot = widget.spot_concordia
    for axis in ("bottom", "left"):
        assert not plot._plot_item.getAxis(axis).autoSIPrefix
    for low, high, step in ((.001, 1000, 100), (550, 650, 10), (563, 563.8, .1)):
        set_age_range(plot, low, high)
        app.processEvents()
        assert plot._label_step == step
        assert 1 <= len(plot._label_ages) <= 12
        assert np.all((plot._label_ages >= low) & (plot._label_ages <= high))
        labels = plot._age_items[::2]
        assert all(plot._plot_item.vb.sceneBoundingRect().contains(item.sceneBoundingRect())
                   for item in labels)
        for i, label in enumerate(labels):
            assert not any(label.sceneBoundingRect().intersects(other.sceneBoundingRect())
                           for other in labels[:i])
            text = label.toPlainText()
            assert len(text) < 8
            if step < 1:
                assert not text.endswith(".0")
        if step < 1:
            assert any("." in item.toPlainText() for item in labels)
        # The curve uses this age neighbourhood even at sub-Ma zoom.
        cx, cy = plot._curve.getData()
        assert len(cx) == 1200
        if low > 1:
            point = wetherill_point if diagram == "wetherill" else tera_wasserburg_point
            assert np.ptp(cx) < np.ptp(point(np.array([1, 4500]))[0])
        assert np.all(np.isfinite(cy))


def test_labels_debounce_multiple_ranges(review, app, monkeypatch):
    plot = review[0].spot_concordia
    calls = []
    original = plot._label_timer.timeout
    original.disconnect()
    original.connect(lambda: calls.append(True))
    for low in (500, 510, 520):
        set_age_range(plot, low, low + 10)
    assert calls == []
    app.processEvents()
    assert calls == [True]


@pytest.mark.parametrize("diagram", ["wetherill", "tera_wasserburg"])
@pytest.mark.parametrize("kind", ["spot", "depth"])
def test_reset_keeps_items_and_review_state(review, app, monkeypatch, diagram, kind):
    widget, finalized = review
    widget.set_selected_spots({"B_11"})
    widget.set_spot_included({"A_12"}, False)
    widget.spot_diagram_buttons[diagram].click()
    plot = widget.spot_concordia
    if kind == "depth":
        frame = finalized.parallel_results.rename(columns={
            col: col + "_fractionation_corrected" for col in
            ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb")})
        plot.show_data(frame, diagram=diagram, kind=kind,
                       include_calibration_uncertainty=True, theme=THEMES["light"])
    plot._set_hover(1)
    items = (plot._scatter, plot._curve, plot._hover_text, tuple(plot._ellipses))
    default = np.array(plot._default_range)
    monkeypatch.setattr(plot, "_draw", lambda **kw: pytest.fail("reset redrew data"))
    set_age_range(plot, 560, 561)
    plot.restore_default_view()
    np.testing.assert_allclose(plot._plot_item.vb.viewRange(), default)
    set_age_range(plot, 570, 571)
    # Send an actual double-click to empty plot area, away from a data point.
    pos = plot.mapFromScene(plot._plot_item.vb.sceneBoundingRect().center())
    QtTest.QTest.mouseDClick(plot.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=pos)
    QtTest.QTest.mouseRelease(plot.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=pos)
    app.processEvents()
    np.testing.assert_allclose(plot._plot_item.vb.viewRange(), default)
    assert items == (plot._scatter, plot._curve, plot._hover_text, tuple(plot._ellipses))
    assert plot._hover_index == 1
    assert widget.selected_spots == {"B_11"}
    assert not widget.include_flags["A_12"]


def test_include_colour_changes_keep_range_new_results_and_diagram_reset(review, app):
    widget, finalized = review
    plot = widget.spot_concordia
    set_age_range(plot, 560, 561)
    expected = plot._plot_item.vb.viewRange()
    plot._set_hover(2)
    original_colours = {name: colour.name() for name, x, y, colour in plot._points}
    widget.set_spot_included({"B_11"}, False)
    np.testing.assert_allclose(plot._plot_item.vb.viewRange(), expected)
    assert all(colour.name() == original_colours[name] for name, x, y, colour in plot._points)
    widget.spot_colour_buttons["sample"].click()
    np.testing.assert_allclose(plot._plot_item.vb.viewRange(), expected)
    assert plot._points[plot._hover_index][0] == "A_12"
    assert plot._hover_text.isVisible()
    widget.set_all_spot_choices(False)
    np.testing.assert_allclose(plot._plot_item.vb.viewRange(), expected)
    widget.set_all_spot_choices(True)
    np.testing.assert_allclose(plot._plot_item.vb.viewRange(), expected)
    widget.show_results(finalized)
    assert not np.allclose(plot._plot_item.vb.viewRange(), expected)
    set_age_range(plot, 560, 561)
    widget.spot_diagram_buttons["tera_wasserburg"].click()
    np.testing.assert_allclose(plot._plot_item.vb.viewRange(), plot._default_range)
    pd.testing.assert_frame_equal(finalized.parallel_results, widget._spot_frame)


def test_full_double_click_sequence_keeps_pre_click_selection(review, app):
    widget, _ = review
    plot = widget.spot_concordia
    widget.set_selected_spots({"B_11"})
    set_age_range(plot, 560, 561)
    app.processEvents()
    pos = plot.mapFromScene(plot._plot_item.vb.sceneBoundingRect().center())
    QtTest.QTest.mouseClick(plot.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=pos)
    QtTest.QTest.mouseDClick(plot.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=pos)
    QtTest.QTest.mouseRelease(plot.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=pos)
    app.processEvents()
    assert widget.selected_spots == {"B_11"}
    np.testing.assert_allclose(plot._plot_item.vb.viewRange(), plot._default_range)


def test_ellipse_data_extent_independent_of_range(review):
    plot = review[0].spot_concordia
    _, cx, cy, _ = plot._points[0]
    for low, high in ((1, 1000), (563, 564)):
        set_age_range(plot, low, high)
        ex, ey = plot._ellipses[0][1].getData()
        np.testing.assert_allclose([ex.min(), ex.max(), ey.min(), ey.max()],
                                   [cx - .01, cx + .01, cy - .002, cy + .002])
        x, y = ConcordiaReviewWidget.ellipse_coordinates(4, 7, .2, .3, 0)
        np.testing.assert_allclose([x.min(), x.max(), y.min(), y.max()], [3.8, 4.2, 6.7, 7.3])
    for sx, sy, rho in ((0, .2, 0), (-1, .2, 0), (.1, np.nan, 0), (np.inf, .2, 0)):
        assert not ConcordiaReviewWidget.ellipse_coordinates(1, 1, sx, sy, rho)[0].size


@pytest.mark.parametrize("diagram,column", [("wetherill", "err_corr"), ("tera_wasserburg", "tw_rho")])
@pytest.mark.parametrize("rho,expected", [(np.nan, 0.), (1., .999), (-1., -.999)])
def test_missing_and_rounded_correlations_keep_ellipses(review, diagram, column, rho, expected):
    plot = review[0].spot_concordia
    frame = review[1].parallel_results.copy()
    frame[column] = rho
    plot.show_data(frame, diagram=diagram, kind="spot",
                   include_calibration_uncertainty=True, theme=THEMES["light"])
    assert len(plot._ellipses) == len(frame)
    for actual, reference in zip(ConcordiaReviewWidget.ellipse_coordinates(4, 7, .2, .3, rho),
                                 ConcordiaReviewWidget.ellipse_coordinates(4, 7, .2, .3, expected)):
        np.testing.assert_allclose(actual, reference)
    _, x, y, _ = plot._points[0]
    sx, sy = ((.01, .002) if diagram == "wetherill" else (.1, .001))
    for actual, reference in zip(plot._ellipses[0][1].getData(),
                                 ConcordiaReviewWidget.ellipse_coordinates(x, y, sx, sy, expected)):
        np.testing.assert_allclose(actual, reference)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_hover_background_is_opaque_and_themed(review, mode):
    widget, _ = review
    widget.apply_theme(THEMES[mode])
    hover = widget.spot_concordia._hover_text
    assert hover.fill.color().name() == QtGui.QColor(THEMES[mode].surface).name()
    assert hover.fill.color().alpha() == 245
    assert hover.border.color() == theme_color(THEMES[mode].input_border)


def test_spot_tooltip_and_hover_scroll_without_selection(review, app, monkeypatch):
    widget, _ = review
    plot = widget.spot_concordia
    widget.set_selected_spots({"B_11"})
    scrolled = []
    chooser = widget.spot_choice_list
    monkeypatch.setattr(widget.results_table, "scrollTo", lambda index: pytest.fail("scrolled hidden table"))
    monkeypatch.setattr(chooser, "scrollTo", lambda index: scrolled.append(index))
    index = 0
    _, x, y, _ = plot._points[index]
    scene_pos = plot._plot_item.vb.mapViewToScene(QtCore.QPointF(x, y))
    assert plot.nearest_point(scene_pos + QtCore.QPointF(3, 2)) == index
    assert plot.nearest_point(scene_pos + QtCore.QPointF(8, 0)) is None
    plot._on_mouse_moved(scene_pos)
    text = plot.tooltip_text(index)
    for expected in ("Z_10", "Sample: Z", "Spot number: 10",
                     "Preferred date: 530 ± 19 Ma (206Pb/238U, fail)", "12.34 ± 0.21 Ma",
                     "Discordance: 12.3%", "U: 123 ug/g", "Th: 45.7 ug/g",
                     "Th/U: 0.123", "high discordance", "Included in review: yes"):
        assert expected in text
    assert text.count("Preferred date:") == 1
    assert "Method:" not in text
    assert not scrolled  # This chooser row is already visible.
    assert chooser.item(index).background().style() != QtCore.Qt.BrushStyle.NoBrush
    assert not chooser.item(index).isSelected()
    assert widget.selected_spots == {"B_11"}
    assert plot._hover_text.isVisible()
    assert len(plot._hover_ring.getData()[0]) == 1
    plot._on_mouse_moved(QtCore.QPointF(-100, -100))
    assert plot._hover_index is None
    assert not plot._hover_text.isVisible()
    assert len(plot._hover_ring.getData()[0]) == 0
    assert chooser.item(index).background().style() == QtCore.Qt.BrushStyle.NoBrush
    plot._point_rows[0]["grain"] = "g7"
    assert "Grain: g7" in plot.tooltip_text(0)
    assert "Spot number:" not in plot.tooltip_text(0)
    widget.include_flags["Z_10"] = False
    assert "Included in review: no" in plot.tooltip_text(0)


def test_depth_tooltip_and_no_ellipse_without_errors(review):
    plot = review[0].depth_concordia
    frame = pd.DataFrame({"signal_relative_time": [1.2345],
                          "207Pb/235U_fractionation_corrected": [.2],
                          "206Pb/238U_fractionation_corrected": [.03],
                          "206Pb/238U_fractionation_corrected_2e": [.0021]})
    plot.show_data(frame, diagram="wetherill", kind="depth",
                   include_calibration_uncertainty=True, theme=THEMES["light"])
    assert "Sweep time: 1.23 s" in plot.tooltip_text(0)
    assert "206Pb/238U: 0.0300 ± 0.0021" in plot.tooltip_text(0)
    assert not plot._ellipses
    assert not plot._plot_item.getAxis("bottom").autoSIPrefix
    assert not plot._plot_item.getAxis("left").autoSIPrefix


def test_stable_sample_colours_and_drawn_legend(review):
    widget, finalized = review
    plot = widget.spot_concordia
    widget.spot_colour_buttons["sample"].click()
    palette = plot._sample_colors.copy()
    colours = {name: color.name() for name, x, y, color in plot._points}
    assert list(palette) == ["A", "B", "Z"]
    assert [label.text for _, label in plot._legend.items] == ["A", "B", "Z"]
    widget.set_spot_included({"A_12"}, False)
    assert plot._sample_colors == palette
    assert [label.text for _, label in plot._legend.items] == ["B", "Z"]
    assert all(colours[name] == color.name() for name, x, y, color in plot._points)
    widget.filter_spot_choices("B")
    assert plot._sample_colors == palette
    widget.set_spot_included({"Z_10"}, False)
    assert plot._legend is None
    widget.set_spot_included({"A_12"}, True)
    assert [label.text for _, label in plot._legend.items] == ["A", "B"]


def test_legend_moves_to_empty_corner_and_labels_avoid_it(review, app):
    widget, finalized = review
    frame = finalized.parallel_results.copy()
    # Initially occupy every corner except bottom left.
    frame["207Pb/235U"] = [.03, .97, .97, .97]
    frame["206Pb/238U"] = [.97, .97, .03, .97]
    plot = widget.spot_concordia
    plot.show_data(frame, diagram="wetherill", kind="spot", colour_mode="sample",
                   include_calibration_uncertainty=True, theme=THEMES["light"])
    plot._plot_item.setRange(xRange=(0, 1), yRange=(0, 1), padding=0)
    app.processEvents()
    vb = plot._plot_item.vb
    bounds = vb.sceneBoundingRect()
    rect = plot._legend.sceneBoundingRect()
    assert rect.center().x() < bounds.center().x()
    assert rect.center().y() > bounds.center().y()
    # Pan left: the same points now occupy the two right corners only.
    plot._plot_item.setRange(xRange=(-.94, .06), yRange=(0, 1), padding=0)
    app.processEvents()
    rect = plot._legend.sceneBoundingRect()
    assert rect.center().x() < bounds.center().x()
    assert rect.center().y() < bounds.center().y()
    assert all(not label.sceneBoundingRect().intersects(rect) for label in plot._age_items[::2])


def test_hover_reveals_chooser_once_and_results_tab_reveals_selection(review, app, monkeypatch):
    widget, finalized = review
    frame = pd.concat([finalized.parallel_results] * 40, ignore_index=True)
    frame["spot_name"] = [f"spot_{i:03d}" for i in range(len(frame))]
    finalized.parallel_results = frame
    finalized.selected_results = frame.copy()
    widget.show_results(finalized)
    app.processEvents()
    table, chooser = widget.results_table, widget.spot_choice_list
    table.sortByColumn(1, QtCore.Qt.SortOrder.DescendingOrder)
    chooser_scrolls, table_scrolls = [], []
    scroll_to = chooser.scrollTo
    def scroll(index):
        chooser_scrolls.append(index.row())
        scroll_to(index)
    monkeypatch.setattr(chooser, "scrollTo", scroll)
    scroll_table_to = table.scrollTo
    def table_scroll(index):
        table_scrolls.append(index.row())
        scroll_table_to(index)
    monkeypatch.setattr(table, "scrollTo", table_scroll)
    widget.set_selected_spots({"spot_090", "spot_120"}, origin=chooser)
    assert not table_scrolls
    plot = widget.spot_concordia
    plot._set_hover(150)
    app.processEvents()
    assert chooser_scrolls == [150]
    assert not table_scrolls
    assert chooser.viewport().rect().intersects(chooser.visualItemRect(chooser.item(150)))
    plot._set_hover(None)
    plot._set_hover(150)
    assert chooser_scrolls == [150]
    assert widget.selected_spots == {"spot_090", "spot_120"}
    plot._set_hover(None)
    assert chooser.item(150).background().style() == QtCore.Qt.BrushStyle.NoBrush
    widget.tabs.setCurrentWidget(table)
    app.processEvents()
    selected = sorted(table.selectionModel().selectedRows(), key=lambda index: index.row())
    assert table_scrolls == [selected[0].row()]
    assert table.viewport().rect().intersects(table.visualRect(selected[0]))
    widget.tabs.setCurrentWidget(widget.spot_concordia_page)
    widget.tabs.setCurrentWidget(table)
    assert table_scrolls == [selected[0].row()]


def test_scrolled_review_controls_and_timers_die_before_restyle(app):
    frame = pd.DataFrame({"spot_name": [f"spot_{i:03d}" for i in range(245)],
                          "sample": ["A", "B", "C", "D", "E"] * 49,
                          "207Pb/235U": np.linspace(.1, 2, 245),
                          "206Pb/238U": np.linspace(.01, .2, 245)})
    for _ in range(3):
        widget = ResultsTableWidget()
        timer = None
        try:
            widget.apply_theme(THEMES["light"])
            widget.show_results(SimpleNamespace(parallel_results=frame, selected_results=frame))
            widget.resize(1000, 600)
            widget.tabs.setCurrentWidget(widget.spot_concordia_page)
            widget.show()
            app.processEvents()
            canvas = widget.spot_concordia
            plot = canvas._plot_item
            controls = (plot.ctrlMenu, plot.vb.menu)
            scrollbars = controls[0].findChildren(QtWidgets.QScrollBar)
            timer, scene = canvas._label_timer, canvas.scene()
            assert scrollbars
            assert all(menu.parent() is canvas for menu in controls)
            for index in (240, 20, 200):
                canvas._set_hover(index)
                widget.set_selected_spots({f"spot_{index:03d}"})
                widget.refresh_spot_concordia()
                app.processEvents()
                assert canvas._plot_item is plot
            set_age_range(canvas, 500, 510)
            assert timer.isActive()
        finally:
            widget.close()
            timer_stopped = timer is None or not timer.isActive()
            widget.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            app.processEvents()
        assert timer_stopped
        # Retain the Python wrappers: Qt ownership must still destroy every
        # control, including the menus' otherwise parentless scroll areas.
        assert all(not shiboken6.isValid(obj) for obj in
                   (widget, canvas, plot, timer, scene, *controls, *scrollbars))
        build_application([], "light")


def test_nearest_point_for_large_frame_at_different_ranges(review, app):
    plot = review[0].spot_concordia
    ages = np.linspace(300, 900, 330)
    x, y = wetherill_point(ages)
    frame = pd.DataFrame({"spot_name": [f"large_{i}" for i in range(330)],
                          "207Pb/235U": x, "206Pb/238U": y})
    plot.show_data(frame, diagram="wetherill", kind="spot",
                   include_calibration_uncertainty=True, theme=THEMES["light"])
    app.processEvents()
    for low, high in ((200, 1000), (ages[159] - .1, ages[159] + .1)):
        set_age_range(plot, low, high)
        app.processEvents()
        pos = plot._plot_item.vb.mapViewToScene(QtCore.QPointF(x[159], y[159]))
        assert plot.nearest_point(pos) == 159
    assert len(plot._scatter.getData()[0]) == 330
    assert not plot._ellipses


@pytest.mark.parametrize("mode,enabled", [("xy", (True, True)), ("x", (True, False)),
                                         ("y", (False, True))])
def test_wheel_box_zoom_and_pan_respect_axes(review, app, mode, enabled):
    plot = review[0].spot_concordia
    vb = plot._plot_item.vb
    plot.set_zoom_mode(mode)
    start, end = pg.Point(200, 150), pg.Point(400, 300)

    class Drag:
        def button(self):
            return QtCore.Qt.MouseButton.LeftButton

        def buttonDownPos(self, *args):
            return start

        def pos(self):
            return end

        def lastPos(self):
            return start

        def isFinish(self):
            return True

        def accept(self):
            pass

    class Wheel:
        def delta(self):
            return 120

        def pos(self):
            return start

        def accept(self):
            pass

    plot.set_interaction_mode("zoom")
    before = np.array(vb.viewRange())
    vb.mouseDragEvent(Drag())
    boxed = np.array(vb.viewRange())
    vb.wheelEvent(Wheel())
    wheeled = np.array(vb.viewRange())
    plot.set_interaction_mode("pan")
    vb.mouseDragEvent(Drag())
    panned = np.array(vb.viewRange())
    for axis, changed in enumerate(enabled):
        assert (not np.allclose(before[axis], boxed[axis])) == changed
        assert (np.ptp(wheeled[axis]) < np.ptp(boxed[axis])) == changed
        assert (not np.allclose(panned[axis], wheeled[axis])) == changed
        np.testing.assert_allclose(np.ptp(panned[axis]), np.ptp(wheeled[axis]))
