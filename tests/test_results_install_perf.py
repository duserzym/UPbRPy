"""Bound results installation work and preserve deferred review state."""

from time import perf_counter
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI.theme import THEMES, build_application
from GUI.widgets import ResultsTableWidget, _SPOT_NAMES_ROLE
from UPbRPy.isochron import tera_wasserburg_point, wetherill_point


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.fixture
def finalized():
    # Same ratio/uncertainty layout as test_live_concordia, with the corrected
    # quantities and dates present in a saved reduction's wider results table.
    ages = np.linspace(499., 501., 400)
    x, y = wetherill_point(ages)
    tx, ty = tera_wasserburg_point(ages)
    frame = pd.DataFrame({
        "spot_name": [f"{chr(65 + i // 100)}_{i + 1}" for i in range(400)],
        "sample": np.repeat(list("ABCD"), 100),
        "207Pb/235U": x, "206Pb/238U": y,
        "238U/206Pb": tx, "207Pb/206Pb": ty,
        "preferred_date": ages, "preferred_date_2e": np.full(400, 2.),
        "preferred_date_method": ["206Pb/238U"] * 400,
        "preferred_date_pass": [True] * 400,
        "automatic_flags": [""] * 400, "disc%": np.full(400, 1.),
    })
    for ratio in ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb"):
        for suffix in ("", "_fractionation_corrected"):
            frame[ratio + suffix] = frame[ratio]
            frame[f"{ratio}{suffix}_2e_with_calibration"] = frame[ratio] * .02
            frame[f"{ratio}{suffix}_2e_no_calibration"] = frame[ratio] * .01
        frame[f"{ratio}_age"] = ages
        frame[f"{ratio}_age_2e_with_calibration"] = 2.
        frame[f"{ratio}_age_2e_no_calibration"] = 1.
    return SimpleNamespace(
        selected_results=frame.copy(), parallel_results=frame,
        secondary_standard_summary=pd.DataFrame({"sample": list("ABCD"), "n": [100] * 4}),
    )


@pytest.fixture
def review(app):
    widget = ResultsTableWidget()
    widget.apply_theme(THEMES["light"])
    try:
        yield widget
    finally:
        widget.close()
        widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_results_install_bounded_data_calls(review, finalized, monkeypatch):
    proxy = review.results_table.model()
    original = proxy.data
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(proxy, "data", counted)
    start = perf_counter()
    review.show_results(finalized)
    elapsed = perf_counter() - start
    print(f"400 spots: show_results={elapsed:.3f}s, results proxy data calls={calls}")
    assert calls < 60_000
    header = review.results_table.horizontalHeader()
    for column in range(proxy.columnCount()):
        label = str(proxy.headerData(column, QtCore.Qt.Orientation.Horizontal))
        assert header.sectionSize(column) >= header.fontMetrics().horizontalAdvance(label)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_deferred_trees_keep_review_state(review, finalized, app, monkeypatch, theme):
    views = (review.results_tree, review.secondary_tree, review.spot_choice_tree)
    expansions = dict.fromkeys(views, 0)
    for view in views:
        original = view.expandAll

        def expanded(view=view, original=original):
            expansions[view] += 1
            original()

        monkeypatch.setattr(view, "expandAll", expanded)
    review.show_results(finalized)
    for view in views:
        assert view in review._pending_group_views
        assert view.model().sourceModel().rowCount(QtCore.QModelIndex()) == 0
    hidden = set(finalized.selected_results.spot_name.iloc[::7])
    selected = {"A_1", "B_101"}
    review.set_spot_included(hidden, False)
    review.set_spot_excluded(selected, True)
    review.set_selected_spots(selected)
    review.apply_theme(THEMES[theme])
    review.resize(1000, 600)
    review.show()
    app.processEvents()
    for view, toggle, page in (
        (review.results_tree, review.results_group_toggle, review.results_page),
        (review.secondary_tree, review.secondary_group_toggle, review.secondary_page),
        (review.spot_choice_tree, review.spot_group_toggle, review.spot_concordia_page),
    ):
        toggle.setChecked(True)
        review.tabs.setCurrentWidget(page)
        app.processEvents()
        assert view.isVisible()
        assert view not in review._pending_group_views
        assert expansions[view] == 1
        model = view.model().sourceModel()
        found = set()
        for row in range(model.rowCount()):
            parent = model.item(row)
            assert parent.checkState() == QtCore.Qt.CheckState.PartiallyChecked
            for child_row in range(parent.rowCount()):
                child = parent.child(child_row)
                name, = child.data(_SPOT_NAMES_ROLE)
                found.add(name)
                expected = (QtCore.Qt.CheckState.Unchecked if name in hidden
                            else QtCore.Qt.CheckState.Checked)
                assert child.checkState() == expected
                for column in range(parent.columnCount()):
                    cell = parent.child(child_row, column)
                    assert cell.font().italic() == (name in selected)
                    assert ("Excluded from calculations" in cell.toolTip()) == (name in selected)
                    if name in selected:
                        assert cell.foreground().color().name() == THEMES[theme].muted
        assert found == set(finalized.selected_results.spot_name)
        assert {name for index in view.selectionModel().selectedRows()
                for name in index.data(_SPOT_NAMES_ROLE)} == selected
        toggle.setChecked(False)
        toggle.setChecked(True)
        assert expansions[view] == 1


def test_visible_tree_refreshes_once_and_hidden_trees_use_new_results(review, finalized, app, monkeypatch):
    review.show_results(finalized)
    review.results_group_toggle.setChecked(True)
    review.resize(1000, 600)
    review.show()
    app.processEvents()
    styles = []
    original = review._update_exclusion_styles

    def styled(**kwargs):
        styles.append(kwargs)
        original(**kwargs)

    monkeypatch.setattr(review, "_update_exclusion_styles", styled)
    finalized.selected_results["sample"] = "Reinstalled"
    finalized.parallel_results["sample"] = "Reinstalled"
    finalized.secondary_standard_summary = pd.DataFrame({"sample": ["Reinstalled"], "n": [400]})
    review.show_results(finalized)
    assert len(styles) == 1
    assert review.results_group_model.rowCount() == 1
    assert review.results_group_model.item(0).child(399) is not None
    assert review.results_group_model.item(0).text().startswith("Reinstalled · 400 spots")
    assert review.spot_choice_tree in review._pending_group_views
    review.spot_group_toggle.setChecked(True)
    review.tabs.setCurrentWidget(review.spot_concordia_page)
    app.processEvents()
    assert review.spot_choice_group_model.rowCount() == 1
    assert review.spot_choice_group_model.item(0).text() == "Reinstalled · 400/400"


def test_depth_tree_is_deferred_until_its_page_is_shown(review, finalized, app):
    review.show_results(finalized)
    review.show_depth_profile(finalized.parallel_results, "A_1")
    view = review.depth_choice_tree
    assert view in review._pending_group_views
    assert view.model().sourceModel().rowCount(QtCore.QModelIndex()) == 0
    review.set_spot_included({"A_1"}, False)
    review.set_selected_spots({"B_101"})
    review.tabs.setCurrentWidget(review.depth_concordia_page)
    review.resize(1000, 600)
    review.show()
    app.processEvents()
    assert view not in review._pending_group_views
    model = review.depth_choice_group_model
    assert sum(model.item(row).rowCount() for row in range(model.rowCount())) == 400
    assert model.item(0).child(0).checkState() == QtCore.Qt.CheckState.Unchecked
    assert {name for index in view.selectionModel().selectedRows()
            for name in index.data(_SPOT_NAMES_ROLE)} == {"B_101"}
