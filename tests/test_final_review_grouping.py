"""Sample grouping shares review state and leaves finalized products intact."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from GUI.theme import THEMES, build_application, theme_color
from GUI.widgets import ResultsTableWidget
from UPbRPy.results import format_mswd, format_value_with_uncertainty, weighted_mean_and_mswd


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.fixture
def review(app):
    frame = pd.DataFrame({
        "sample": ["B", "A", "A", "B", "A", "C"],
        "spot_name": ["B_1", "A_10", "A_2", "B_2", "A_3", "C_1"],
        "preferred_date": [200., 103., 100., np.nan, 101., np.nan],
        "preferred_date_2e": [4., 3., 2., 4., 1., 1.],
        "preferred_date_method": ["206Pb/238U"] * 6,
        "preferred_date_pass": [True] * 6,
        "automatic_flags": [""] * 6,
        "disc%": [1.] * 6,
        "207Pb/235U": np.linspace(.2, .3, 6),
        "206Pb/238U": np.linspace(.03, .04, 6),
    })
    parallel = pd.concat([frame, frame.iloc[[0]].assign(sample="S", spot_name="S_9")], ignore_index=True)
    summary = pd.DataFrame({"sample": ["S", "B"], "n_total": [1, 10],
                            "n_flagged": [0, 7], "n": [1, 2],
                            "weighted_mean": [321., 1097.], "weighted_mean_2e": [3., 34.],
                            "mswd": [1060., .94], "accepted_age": [np.nan, 1065.4],
                            "accepted_age_2e": [np.nan, .6], "offset_percent": [np.nan, 3.]})
    finalized = SimpleNamespace(selected_results=frame, parallel_results=parallel,
                                secondary_standard_summary=summary)
    widget = ResultsTableWidget()
    try:
        widget.apply_theme(THEMES["light"])
        widget.show_results(finalized)
        widget.resize(1000, 600)
        widget.show()
        app.processEvents()
        yield widget, finalized
    finally:
        widget.close()
        widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def children(view, parent, column):
    model = view.model()
    return [model.index(row, column, parent).data() for row in range(model.rowCount(parent))]


def test_headers_counts_summary_and_inclusion(review):
    widget, finalized = review
    original = finalized.selected_results.copy(deep=True)
    widget.results_group_toggle.setChecked(True)
    model = widget.results_group_model
    assert model.rowCount() == 3
    header = model.item(0)
    assert header.text().startswith("A · 3 spots (3 included)")
    for included in (["A_10", "A_2", "A_3"], ["A_2", "A_3"]):
        data = original.loc[original.spot_name.isin(included)]
        expected = weighted_mean_and_mswd(data.preferred_date, data.preferred_date_2e)
        formatted = format_value_with_uncertainty(expected["weighted_mean"], expected["weighted_mean_2e"])
        assert f"{formatted} Ma (2SE); MSWD {format_mswd(expected['mswd'])}; n={expected['n']}" in header.text()
        widget.set_spot_included({"A_10"}, False)
    assert "3 spots (2 included)" in header.text()
    assert header.checkState() == QtCore.Qt.CheckState.PartiallyChecked
    assert "Ma" not in model.item(1).text()  # Only one finite B date.
    assert "Ma" not in model.item(2).text()
    header.setCheckState(QtCore.Qt.CheckState.Unchecked)
    assert not any(widget.include_flags[name] for name in ("A_10", "A_2", "A_3"))
    header.setCheckState(QtCore.Qt.CheckState.Checked)
    assert all(widget.include_flags[name] for name in ("A_10", "A_2", "A_3"))
    assert widget.include_flags["B_1"]
    pd.testing.assert_frame_equal(finalized.selected_results, original)
    pd.testing.assert_frame_equal(widget.results_model.frame, original)


@pytest.mark.parametrize("order", [QtCore.Qt.SortOrder.AscendingOrder, QtCore.Qt.SortOrder.DescendingOrder])
def test_sort_only_spots_within_groups(review, order):
    widget, finalized = review
    view = widget.results_tree
    column = finalized.selected_results.columns.get_loc("preferred_date") + 1
    view.sortByColumn(column, order)
    proxy = view.model()
    assert [proxy.index(row, 0).data().split(" · ")[0] for row in range(proxy.rowCount())] == ["A", "B", "C"]
    parent = proxy.index(0, 0)
    values = [float(value) for value in children(view, parent, column)]
    assert values == sorted(values, reverse=order == QtCore.Qt.SortOrder.DescendingOrder)


@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("query, expected", [("a_2", {"A_2"}), ("b", {"B_1", "B_2"}),
                                              ("no match", set()), ("", {"A_10", "A_2", "A_3", "B_1", "B_2", "C_1"})])
def test_filter_spots_and_empty_groups(review, grouped, query, expected):
    widget, _ = review
    widget.set_selected_spots({"C_1"})
    widget.results_group_toggle.setChecked(grouped)
    widget.results_filter.setText(query)
    view = widget.results_tree if grouped else widget.results_table
    assert widget._visible_spot_names(view) == expected
    assert widget.selected_spots == {"C_1"}
    if grouped:
        assert view.model().isRecursiveFilteringEnabled()
        assert view.model().rowCount() == len({name.split("_")[0] for name in expected})


@pytest.mark.parametrize("grouped", [False, True])
def test_filter_matches_sample_independently_of_spot_name(review, grouped):
    widget, finalized = review
    for frame in (finalized.selected_results, finalized.parallel_results):
        frame.loc[frame["sample"].eq("A"), "sample"] = "Aliased rock"
    widget.show_results(finalized)
    widget.results_group_toggle.setChecked(grouped)
    widget.spot_group_toggle.setChecked(grouped)
    widget.results_filter.setText("ALIASED")
    widget.spot_filter.setText("ALIASED")
    results = widget.results_tree if grouped else widget.results_table
    chooser = widget.spot_choice_tree if grouped else widget.spot_choice_list
    assert widget._visible_spot_names(results) == {"A_10", "A_2", "A_3"}
    assert widget._visible_spot_names(chooser) == {"A_10", "A_2", "A_3"}


def test_group_selection_space_and_child_deselection(review, app):
    widget, _ = review
    widget.results_group_toggle.setChecked(True)
    view = widget.results_tree
    parent = view.model().index(0, 0)
    flags = QtCore.QItemSelectionModel.SelectionFlag
    view.selectionModel().select(parent, flags.ClearAndSelect | flags.Rows)
    assert widget.selected_spots == {"A_10", "A_2", "A_3"}
    assert widget.spot_concordia.selected_spots == widget.selected_spots
    assert {item.text() for item in widget.spot_choice_list.selectedItems()} == widget.selected_spots
    child = view.model().index(0, 0, parent)
    name_column = widget.results_model.frame.columns.get_loc("spot_name") + 1
    name = child.siblingAtColumn(name_column).data()
    view.selectionModel().select(child, flags.Deselect | flags.Rows)
    assert widget.selected_spots == {"A_10", "A_2", "A_3"} - {name}
    QtTest.QTest.keyClick(view, QtCore.Qt.Key.Key_Space)
    assert not any(widget.include_flags[name] for name in widget.selected_spots)
    widget.include_selected()
    assert all(widget.include_flags.values())
    widget.select_same_sample()
    assert widget.selected_spots == {"A_10", "A_2", "A_3"}
    widget.results_filter.setText("B")
    QtTest.QTest.keySequence(view, QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.SelectAll))
    assert widget.selected_spots == {"B_1", "B_2"}
    app.processEvents()


@pytest.mark.parametrize("chooser", [False, True])
def test_grouped_native_shift_and_control_clicks(review, app, chooser):
    widget, _ = review
    if chooser:
        widget.spot_group_toggle.setChecked(True)
        widget.tabs.setCurrentWidget(widget.spot_concordia_page)
        view, column = widget.spot_choice_tree, 0
    else:
        widget.results_group_toggle.setChecked(True)
        view = widget.results_tree
        column = widget.results_model.frame.columns.get_loc("spot_name") + 1
    app.processEvents()
    parent = view.model().index(0, 0)
    def point(row):
        rect = view.visualRect(view.model().index(row, column, parent))
        return QtCore.QPoint(rect.right() - 5, rect.center().y())  # Away from check indicators.
    QtTest.QTest.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point(0))
    assert widget.selected_spots == {"A_10"}
    QtTest.QTest.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton,
                          QtCore.Qt.KeyboardModifier.ShiftModifier, pos=point(2))
    assert widget.selected_spots == {"A_10", "A_2", "A_3"}
    QtTest.QTest.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton,
                          QtCore.Qt.KeyboardModifier.ControlModifier, pos=point(1))
    assert widget.selected_spots == {"A_10", "A_3"}
    assert all(widget.include_flags.values())


@pytest.mark.parametrize("grouped", [False, True])
def test_chooser_group_inclusion_filter_and_selection(review, grouped):
    widget, _ = review
    widget.spot_group_toggle.setChecked(grouped)
    parent = widget.spot_choice_group_model.item(0)
    assert parent.text() == "A · 3/3"
    assert parent.toolTip() == "A (3 included / 3)"
    parent.setCheckState(QtCore.Qt.CheckState.Unchecked)
    assert not any(widget.include_flags[name] for name in ("A_10", "A_2", "A_3"))
    widget.set_spot_included({"A_2"}, True)
    assert parent.checkState() == QtCore.Qt.CheckState.PartiallyChecked
    assert parent.text() == "A · 1/3"
    assert parent.toolTip() == "A (1 included / 3)"
    widget.spot_filter.setText("a_2")
    view = widget.spot_choice_tree if grouped else widget.spot_choice_list
    assert widget._visible_spot_names(view) == {"A_2"}
    widget.set_all_spot_choices(False)
    assert not widget.include_flags["A_2"] and widget.include_flags["B_1"]
    widget.spot_filter.clear()
    widget.spot_group_toggle.setChecked(True)
    tree = widget.spot_choice_tree
    flags = QtCore.QItemSelectionModel.SelectionFlag
    tree.selectionModel().select(tree.model().index(0, 0), flags.ClearAndSelect | flags.Rows)
    assert widget.selected_spots == {"A_10", "A_2", "A_3"}


def test_secondary_summary_and_spot_children(review, app):
    widget, finalized = review
    original = finalized.secondary_standard_summary.copy(deep=True)
    widget.secondary_group_toggle.setChecked(True)
    widget.tabs.setCurrentWidget(widget.secondary_page)
    widget.resize(750, 600)  # Results occupy part of a laptop window.
    app.processEvents()
    view = widget.secondary_tree
    proxy = view.model()
    summaries = [proxy.index(row, 0).data() for row in range(proxy.rowCount())]
    assert summaries == [
        "B · 10 spots (7 flagged) · weighted mean 1097 ± 34 Ma (2SE) · MSWD 0.94 · accepted 1065.4 ± 0.6 Ma · offset +3.0%",
        "S · 1 spots (0 flagged) · weighted mean 321.0 ± 3.0 Ma (2SE) · MSWD 1060",
    ]
    model = widget.secondary_group_model
    headers = [model.headerData(col, QtCore.Qt.Orientation.Horizontal) for col in range(model.columnCount())]
    assert headers == ["Include", "spot_name", "preferred_date", "preferred_date_2e",
                       "preferred_date_method", "preferred_date_pass", "disc%", "automatic_flags"]
    spot_column = 1
    assert children(view, proxy.index(0, 0), spot_column) == ["B_1", "B_2"]
    assert children(view, proxy.index(1, 0), spot_column) == ["S_9"]
    parent = proxy.index(0, 0)
    assert children(view, parent, 2) == ["200", ""]
    assert children(view, parent, 3) == ["4", "4"]
    for row in range(proxy.rowCount()):
        assert view.isFirstColumnSpanned(row, QtCore.QModelIndex())
    child_rect = view.visualRect(proxy.index(0, spot_column, parent))
    assert child_rect.isValid() and view.viewport().rect().contains(child_rect)
    widget.set_spot_included({"B_1"}, False)
    assert proxy.index(0, 0).data() == summaries[0]
    widget.secondary_filter.setText("s_9")
    assert proxy.rowCount() == 1 and proxy.index(0, 0).data() == summaries[1]
    assert view.isFirstColumnSpanned(0, QtCore.QModelIndex())
    widget.secondary_group_toggle.setChecked(False)
    assert widget._secondary_stack.currentWidget() is widget.secondary_table
    pd.testing.assert_frame_equal(finalized.secondary_standard_summary, original)
    pd.testing.assert_frame_equal(widget.secondary_model.frame, original)


@pytest.mark.parametrize("summary,expected", [
    ({"sample": "B"}, "B"),
    ({"sample": "B", "n_total": np.nan, "n_flagged": np.inf,
      "weighted_mean": np.nan, "weighted_mean_2e": 2., "mswd": np.inf,
      "accepted_age": np.nan, "accepted_age_2e": .6, "offset_percent": np.nan}, "B"),
    ({"sample": "B", "weighted_mean": 201., "mswd": 1.,
      "accepted_age": 200., "offset_percent": -.5},
     "B · weighted mean 201 Ma (2SE) · MSWD 1.00 · accepted 200 Ma · offset -0.5%"),
])
def test_secondary_summary_optional_fields(review, summary, expected):
    widget, finalized = review
    finalized.secondary_standard_summary = pd.DataFrame([summary])
    widget.show_results(finalized)
    assert widget.secondary_group_model.item(0).text() == expected


def test_chooser_compact_label_at_laptop_width(review, app):
    widget, finalized = review
    frame = pd.concat([finalized.selected_results.iloc[[1]]] * 104, ignore_index=True)
    frame["sample"] = "SAMPLE-B"
    frame["spot_name"] = [f"SAMPLE-B_{index}" for index in range(104)]
    finalized.selected_results = finalized.parallel_results = frame
    widget.show_results(finalized)
    widget.spot_group_toggle.setChecked(True)
    widget.tabs.setCurrentWidget(widget.spot_concordia_page)
    app.processEvents()
    view = widget.spot_choice_tree
    parent = widget.spot_choice_group_model.item(0)
    assert parent.text() == "SAMPLE-B · 104/104"
    assert parent.toolTip() == "SAMPLE-B (104 included / 104)"
    index = view.model().index(0, 0)
    option = QtWidgets.QStyleOptionViewItem()
    view.itemDelegate().initStyleOption(option, index)
    option.rect = view.visualRect(index)
    text_rect = view.style().subElementRect(QtWidgets.QStyle.SubElement.SE_ItemViewItemText, option, view)
    assert view.width() <= 230
    assert option.fontMetrics.horizontalAdvance(parent.text()) <= text_rect.width()


def test_grouped_hover_reveals_child_without_changing_review_state(review, app):
    widget, _ = review
    widget.spot_group_toggle.setChecked(True)
    widget.tabs.setCurrentWidget(widget.spot_concordia_page)
    view = widget.spot_choice_tree
    view.collapseAll()
    widget.set_selected_spots({"B_1"})
    before = widget.include_flags.copy()
    notifications = []
    widget.spot_choice_group_model.dataChanged.connect(lambda *args: notifications.append(True))
    widget._scroll_hovered_spot("A_2")
    app.processEvents()
    assert notifications
    parent = view.model().index(0, 0)
    assert view.isExpanded(parent)
    child = view.model().index(1, 0, parent)
    assert child.data() == "A_2"
    assert child.data(QtCore.Qt.ItemDataRole.BackgroundRole).color() == theme_color(THEMES["light"].accent_soft)
    assert widget.include_flags == before and widget.selected_spots == {"B_1"}
    widget._scroll_hovered_spot("")
    assert child.data(QtCore.Qt.ItemDataRole.BackgroundRole) is None


def test_grouping_preferences_and_small_theme_layout(app, tmp_path):
    settings = QtCore.QSettings(str(tmp_path / "review.ini"), QtCore.QSettings.Format.IniFormat)
    for round_number in (0, 1):
        widget = ResultsTableWidget(settings=settings)
        try:
            toggles = (widget.results_group_toggle, widget.secondary_group_toggle, widget.spot_group_toggle)
            assert all(toggle.isChecked() == bool(round_number) for toggle in toggles)
            for toggle in toggles:
                toggle.setChecked(True)
            for theme in ("light", "dark"):
                widget.apply_theme(THEMES[theme])
                widget.resize(450, 430)  # A narrow plot column at laptop size.
                widget.show()
                app.processEvents()
                assert widget.results_filter.width() > 60
                assert widget.results_page.rect().contains(widget.results_group_toggle.geometry())
        finally:
            widget.close()
            widget.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            app.processEvents()
