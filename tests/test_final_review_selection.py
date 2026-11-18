"""Review inclusion and linked spot selection remain independent of reduction data."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from GUI.theme import THEMES, _stylesheet, build_application
from GUI.widgets import ResultsTableWidget


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.fixture
def review(app):
    frame = pd.DataFrame({
        "sample": ["A"] * 3 + ["B"] * 3,
        "spot_name": [f"{sample}_{i}" for i, sample in enumerate("AAABBB")],
        "207Pb/235U": np.arange(6) * .02 + .2,
        "206Pb/238U": np.arange(6) * .003 + .03,
        "207Pb/235U_2e": [.01] * 6,
        "206Pb/238U_2e": [.002] * 6,
    })
    finalized = SimpleNamespace(selected_results=frame.copy(), parallel_results=frame.copy())
    widget = ResultsTableWidget()
    widget.apply_theme(THEMES["light"])
    widget.show_results(finalized)
    widget.resize(1000, 600)
    widget.show()
    app.processEvents()
    yield widget, finalized
    widget.close()
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def table_names(widget):
    proxy = widget.results_table.model()
    return {widget.results_model.spot_name(proxy.mapToSource(index).row())
            for index in widget.results_table.selectionModel().selectedRows()}


@pytest.fixture
def long_review(review, app):
    widget, finalized = review
    frame = pd.concat([finalized.selected_results] * 20, ignore_index=True)
    frame["spot_name"] = [f"spot_{i:03d}" for i in range(len(frame))]
    finalized.selected_results = frame
    finalized.parallel_results = frame.copy()
    widget.show_results(finalized)
    app.processEvents()
    return widget, finalized


@pytest.mark.parametrize("columns, expected", [
    (["spot_name", "sample"], 2),
    (["spot_name"], 1),
    (["value"], -1),
])
def test_show_results_restores_default_sort(review, columns, expected):
    widget, finalized = review
    frame = pd.DataFrame({"sample": ["B", "A"], "spot_name": ["B_0", "A_1"],
                          "value": [2, 1]})[columns]
    finalized.selected_results = frame
    finalized.parallel_results = frame.copy()
    view = widget.results_table
    for _ in range(2):
        widget.show_results(finalized)
        assert view.model().sortColumn() == expected
        assert view.horizontalHeader().sortIndicatorSection() == expected
        assert view.model().sortOrder() == QtCore.Qt.SortOrder.AscendingOrder
        if expected >= 0:
            assert view.model().index(0, expected).data() == frame.iloc[1, expected - 1]
            before = [view.model().mapToSource(view.model().index(row, 0)).row()
                      for row in range(view.model().rowCount())]
            widget.set_selected_spots({"B_0"})
            QtTest.QTest.keyClick(view, QtCore.Qt.Key.Key_Space)
            assert [view.model().mapToSource(view.model().index(row, 0)).row()
                    for row in range(view.model().rowCount())] == before
        view.sortByColumn(0, QtCore.Qt.SortOrder.DescendingOrder)


@pytest.mark.parametrize("view_name", ["results_table", "spot_choice_list"])
def test_control_click_keeps_origin_scroll_position(long_review, app, view_name):
    widget, _ = long_review
    view = getattr(widget, view_name)
    if view_name == "spot_choice_list":
        widget.tabs.setCurrentWidget(widget.spot_concordia_page)
    else:
        view.sortByColumn(2, QtCore.Qt.SortOrder.AscendingOrder)
    app.processEvents()
    # Keep the first selection far above the row added with Ctrl-click.
    widget.set_selected_spots({"spot_000"})
    view.scrollTo(view.model().index(75, 0), QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)
    app.processEvents()
    before = view.verticalScrollBar().value()
    assert before > 0
    index = view.model().index(75, 1 if view_name == "results_table" else 0)
    QtTest.QTest.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton,
                          QtCore.Qt.KeyboardModifier.ControlModifier, view.visualRect(index).center())
    app.processEvents()
    assert widget.selected_spots == {"spot_000", "spot_075"}
    assert view.verticalScrollBar().value() == before


def test_concordia_selection_reveals_first_row_in_visual_order(long_review, app, monkeypatch):
    widget, _ = long_review
    view = widget.results_table
    view.sortByColumn(2, QtCore.Qt.SortOrder.DescendingOrder)
    proxy = view.model()
    # The filtered and hidden selections must not be scroll targets.
    proxy.setFilterKeyColumn(2)
    proxy.setFilterRegularExpression(r"spot_0[0-8]")
    view.setRowHidden(0, True)
    view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
    app.processEvents()
    scrolled_rows = []
    scroll_to = view.scrollTo

    def record_scroll(index):
        scrolled_rows.append(index.row())
        scroll_to(index)

    monkeypatch.setattr(view, "scrollTo", record_scroll)
    widget.spot_concordia.select_spots({"spot_119", "spot_089", "spot_070", "spot_050"})
    app.processEvents()
    assert scrolled_rows == [19]  # spot_070 precedes spot_050 in descending order.
    index = proxy.index(19, 2)
    assert index.data() == "spot_070"
    assert view.viewport().rect().intersects(view.visualRect(index))
    before = view.verticalScrollBar().value()
    widget.spot_concordia.select_spots({"spot_070", "spot_080"})
    app.processEvents()
    assert scrolled_rows == [19]  # One selected row is already visible.
    assert view.verticalScrollBar().value() == before


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_item_indicator_states_and_icon_contrast(app, mode):
    theme = THEMES[mode]
    style = _stylesheet(theme)
    for selector in ("QAbstractItemView", "QListView", "QTreeView", "QTableView"):
        for state in ("checked", "unchecked", "indeterminate", "disabled"):
            assert f"{selector}::indicator:{state}" in style
    for name in ("check", "minus"):
        path = Path(__file__).parents[1] / f"GUI/assets/icons/control-{name}-{mode}.svg"
        assert path.exists()
        stroke = next(ET.parse(path).getroot().iter("{http://www.w3.org/2000/svg}path")).attrib["stroke"]
        assert stroke == theme.on_accent
        def luminance(value):
            rgb = QtGui.QColor(value).getRgbF()[:3]
            linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
            return sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))
        high, low = sorted((luminance(stroke), luminance(theme.accent)), reverse=True)
        assert (high + .05) / (low + .05) >= 3


def test_include_column_sort_and_chooser_share_state(review):
    widget, finalized = review
    original = finalized.selected_results.copy(deep=True)
    model = widget.results_model
    assert model.headerData(0, QtCore.Qt.Orientation.Horizontal) == "Include"
    assert model.flags(model.index(0, 0)) & QtCore.Qt.ItemFlag.ItemIsUserCheckable
    assert model.setData(model.index(0, 0), QtCore.Qt.CheckState.Unchecked, QtCore.Qt.ItemDataRole.CheckStateRole)
    assert widget.spot_choice_list.item(0).checkState() == QtCore.Qt.CheckState.Unchecked
    assert "A_0" not in {point[0] for point in widget.spot_concordia._points}
    widget.results_table.sortByColumn(0, QtCore.Qt.SortOrder.AscendingOrder)
    proxy = widget.results_table.model()
    assert model.spot_name(proxy.mapToSource(proxy.index(0, 0)).row()) == "A_0"
    widget.spot_choice_list.item(0).setCheckState(QtCore.Qt.CheckState.Checked)
    assert model.data(model.index(0, 0), QtCore.Qt.ItemDataRole.CheckStateRole) == QtCore.Qt.CheckState.Checked
    assert len(widget.spot_concordia._points) == 6
    pd.testing.assert_frame_equal(finalized.selected_results, original)
    pd.testing.assert_frame_equal(finalized.parallel_results, original)
    pd.testing.assert_frame_equal(model.frame, original)


@pytest.mark.parametrize("view_name", ["results_table", "spot_choice_list"])
def test_space_toggles_entire_selection_and_visible_select_all(review, view_name):
    widget, _ = review
    view = getattr(widget, view_name)
    assert view.selectionMode() == QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
    widget.set_selected_spots({"A_0", "B_4"})
    QtTest.QTest.keyClick(view, QtCore.Qt.Key.Key_Space)
    assert not widget.include_flags["A_0"] and not widget.include_flags["B_4"]
    assert widget.selected_spots == {"A_0", "B_4"}
    assert widget.include_flags["A_1"]
    widget.set_spot_included({"A_0"}, True)
    QtTest.QTest.keyClick(view, QtCore.Qt.Key.Key_Space)
    assert all(widget.include_flags.values())
    widget.spot_filter.setText("A_")
    key = QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.SelectAll)[0]
    QtTest.QTest.keySequence(view, QtGui.QKeySequence(key))
    expected = {"A_0", "A_1", "A_2"} if view_name == "spot_choice_list" else set(widget.include_flags)
    assert widget.selected_spots == expected


def test_context_handlers_and_filtered_all_none(review):
    widget, _ = review
    widget.set_selected_spots({"A_0", "B_4"})
    widget.exclude_selected()
    assert not widget.include_flags["B_4"]
    widget.include_selected()
    assert all(widget.include_flags.values())
    widget.set_selected_spots({"A_1"})
    widget.select_same_sample()
    assert widget.selected_spots == {"A_0", "A_1", "A_2"}
    widget.invert_selection()
    assert widget.selected_spots == {"B_3", "B_4", "B_5"}
    widget.spot_filter.setText("A_")
    widget.invert_selection(widget.spot_choice_list)
    assert widget.selected_spots == set(widget.include_flags)
    widget.set_all_spot_choices(False)
    assert {name for name, included in widget.include_flags.items() if included} == {"B_3", "B_4", "B_5"}
    widget.set_all_spot_choices(True)
    assert all(widget.include_flags.values())


@pytest.mark.parametrize("view_name", ["results_table", "spot_choice_list"])
def test_native_click_shift_and_control_selection(review, view_name):
    widget, _ = review
    view = getattr(widget, view_name)
    if view_name == "spot_choice_list":
        widget.tabs.setCurrentWidget(widget.spot_concordia_page)
        point = lambda row: view.visualItemRect(view.item(row)).center()
        names = [view.item(row).text() for row in range(view.count())]
    else:
        widget.results_table.sortByColumn(2, QtCore.Qt.SortOrder.AscendingOrder)
        proxy = view.model()
        point = lambda row: view.visualRect(proxy.index(row, 2)).center()
        names = [widget.results_model.spot_name(proxy.mapToSource(proxy.index(row, 0)).row())
                 for row in range(proxy.rowCount())]
    QtWidgets.QApplication.processEvents()
    QtTest.QTest.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point(1))
    assert widget.selected_spots == {names[1]}
    QtTest.QTest.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton,
                          QtCore.Qt.KeyboardModifier.ShiftModifier, point(3))
    assert widget.selected_spots == set(names[1:4])
    QtTest.QTest.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton,
                          QtCore.Qt.KeyboardModifier.ControlModifier, point(2))
    assert widget.selected_spots == {names[1], names[3]}
    assert all(widget.include_flags.values())


def test_checkbox_click_changes_only_clicked_row(review):
    widget, _ = review
    widget.set_selected_spots({"A_0", "A_1", "A_2"})
    view = widget.results_table
    proxy = view.model()
    index = proxy.mapFromSource(widget.results_model.index(1, 0))
    rect = view.visualRect(index)
    option = QtWidgets.QStyleOptionViewItem()
    option.initFrom(view)
    option.rect = rect
    option.features |= QtWidgets.QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
    indicator = view.style().subElementRect(QtWidgets.QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, view)
    QtTest.QTest.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=indicator.center())
    assert {name for name, included in widget.include_flags.items() if not included} == {"A_1"}


def test_sorted_table_and_chooser_selection_link_to_plot_without_redraw(review):
    widget, _ = review
    plot = widget.spot_concordia
    scatter = plot._scatter
    vb = plot._plot_item.vb
    vb.setRange(xRange=(.19, .26), yRange=(.025, .045), padding=0)
    before = vb.viewRange()
    widget.results_table.sortByColumn(2, QtCore.Qt.SortOrder.DescendingOrder)
    proxy = widget.results_table.model()
    index = proxy.index(0, 0)
    name = widget.results_model.spot_name(proxy.mapToSource(index).row())
    widget.results_table.selectionModel().select(index, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect
                                               | QtCore.QItemSelectionModel.SelectionFlag.Rows)
    assert plot.selected_spots == {name} == table_names(widget)
    assert {item.text() for item in widget.spot_choice_list.selectedItems()} == {name}
    assert plot._scatter is scatter
    assert vb.viewRange() == before
    sizes = scatter.data["size"]
    assert list(sizes).count(11) == 1 and list(sizes).count(8) == 5
    widget.spot_choice_list.item(1).setSelected(True)
    assert "A_1" in plot.selected_spots
    assert table_names(widget) == plot.selected_spots


def test_plot_selection_rectangle_and_click_modifiers_link_to_rows(review):
    widget, _ = review
    plot = widget.spot_concordia
    plot.select_spots({"B_5"})
    assert table_names(widget) == {"B_5"}
    assert widget.spot_choice_list.item(5).isSelected()
    plot.select_rectangle(QtCore.QRectF(.195, .029, .03, .005))
    assert table_names(widget) == {"A_0", "A_1"}
    plot.select_rectangle(QtCore.QRectF(.295, .044, .02, .01), extend=True)
    assert table_names(widget) == {"A_0", "A_1", "B_5"}
    plot.select_spots({"A_0"}, toggle=True)
    assert table_names(widget) == {"A_1", "B_5"}
    pos = plot._plot_item.vb.mapViewToScene(QtCore.QPointF(.24, .036))
    plot._select_at(pos, QtCore.Qt.KeyboardModifier.NoModifier)
    assert table_names(widget) == {"A_2"}
    plot._select_at(pos, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert not table_names(widget)
    plot.select_spots({"A_0"})
    plot._select_at(plot._plot_item.vb.mapViewToScene(QtCore.QPointF(1, 1)), QtCore.Qt.KeyboardModifier.NoModifier)
    assert not table_names(widget)


def test_select_drag_uses_data_rectangle_without_zoom(review):
    widget, _ = review
    plot = widget.spot_concordia
    vb = plot._plot_item.vb
    plot.set_interaction_mode("select")
    before = vb.viewRange()

    class Drag:
        def button(self):
            return QtCore.Qt.MouseButton.LeftButton

        def buttonDownPos(self):
            return vb.mapFromView(QtCore.QPointF(.195, .029))

        def pos(self):
            return vb.mapFromView(QtCore.QPointF(.225, .034))

        def modifiers(self):
            return QtCore.Qt.KeyboardModifier.NoModifier

        def isFinish(self):
            return True

        def accept(self):
            pass

    vb.mouseDragEvent(Drag())
    assert table_names(widget) == {"A_0", "A_1"}
    assert vb.viewRange() == before


def test_select_toolbar_available_for_both_concordia_views(app):
    from GUI.studio_window import StudioWindow

    settings = QtCore.QSettings("UPbRPy", "Studio")
    saved = {key: settings.value(key) for key in settings.allKeys()}
    settings.clear()
    shell = StudioWindow(theme_mode="light")
    window = shell.active_reduction
    try:
        button = window.interact_mode_buttons["select"]
        assert button.text() == "Select" and not button.isEnabled()
        window.session.plot_mode = "final_results"
        window.results_table.tabs.setCurrentWidget(window.results_table.spot_concordia_page)
        window._apply_interaction_controls_to_current_plot()
        assert button.isEnabled()
        window.plots_stack.setCurrentWidget(window.results_table)
        shell.show()
        for width, height in ((1280, 800), (1440, 900)):
            shell.resize(width, height)
            window.apply_default_layout()
            for _ in range(8):
                app.processEvents()
            for scroll, bar in window._toolbar_strips:
                if not scroll.isVisible():
                    continue
                assert scroll.horizontalScrollBar().maximum() == 0
                assert bar.width() <= scroll.viewport().width()
        window.set_interaction_mode("select")
        assert window.results_table.spot_concordia.interaction_mode() == "select"
        window.results_table.tabs.setCurrentWidget(window.results_table.depth_concordia_page)
        assert button.isEnabled()
        assert window._selected_interaction_mode() == "select"
        window.session.plot_mode = "raw"
        window._apply_interaction_controls_to_current_plot()
        window.set_interaction_mode("select")
        assert not button.isEnabled() and window._selected_interaction_mode() == "zoom"
    finally:
        window.has_unsaved_changes = lambda: False
        shell.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
        settings.clear()
        for key, value in saved.items():
            settings.setValue(key, value)
