"""The Spots tree's ticks are the concordia display choice (one state)."""

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets
from PySide6.QtTest import QTest

from GUI import main_window
from GUI.main_window import MainWindow
from GUI.theme import build_application
from GUI.widgets import SPOT_TREE_COUNT_ROLE
from UPbRPy.isochron import tera_wasserburg_point, wetherill_point


RICH = Path(__file__).parent / "fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"
CHECKED, UNCHECKED = QtCore.Qt.CheckState.Checked, QtCore.Qt.CheckState.Unchecked


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


def finalized_for(names, samples):
    ages = np.linspace(400.0, 900.0, len(names))
    x, y = wetherill_point(ages)
    tx, ty = tera_wasserburg_point(ages)
    frame = pd.DataFrame({"spot_name": names, "sample": samples, "207Pb/235U": x, "206Pb/238U": y,
                          "238U/206Pb": tx, "207Pb/206Pb": ty})
    for ratio in ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb"):
        for branch in ("with_calibration", "no_calibration"):
            frame[f"{ratio}_2e_{branch}"] = frame[ratio] * 0.01
    return SimpleNamespace(parallel_results=frame, selected_results=frame[["spot_name", "sample"]].copy())


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    window = MainWindow(theme_mode="light")
    window.resize(1440, 900)
    window.show()
    window.load_experiment_directory(str(RICH), background=False)
    experiment = window.session.experiment
    names = experiment["spot_names"].astype(str).tolist()
    samples = (experiment["sample"].astype(str).tolist() if "sample" in experiment
               else ["S"] * len(names))
    window.session.reduction_cache["finalized_results"] = finalized_for(names, samples)
    window.set_plot_mode("final_results", refresh=True)
    app.processEvents()
    yield window
    window.has_unsaved_changes = lambda: False
    window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def leaves(item):
    out = []
    for index in range(item.childCount()):
        child = item.child(index)
        out.extend(leaves(child) if child.childCount() else [child])
    return out


def folders(window):
    out = []
    iterator = QtWidgets.QTreeWidgetItemIterator(window.spot_list)
    while iterator.value() is not None:
        item = iterator.value()
        if str(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or "").startswith("__folder__"):
            out.append(item)
        iterator += 1
    return out


def name(item):
    return str(item.data(0, QtCore.Qt.ItemDataRole.UserRole))


def settle(app):
    for _ in range(3):
        app.processEvents()


def test_ticks_start_from_the_shared_display_state(window):
    review = window.results_table
    for folder in folders(window):
        for leaf in leaves(folder):
            assert (leaf.checkState(0) == CHECKED) == review.spot_shown(name(leaf))
    total = len(review.include_flags)
    shown = sum(review.include_flags.values())
    assert window.spot_tree_count_label.text() == f"{shown}/{total}"


def test_folder_tick_equals_ticking_every_child_in_one_update(window, app, monkeypatch):
    review = window.results_table
    folder = next(item for item in folders(window) if item.checkState(0) == CHECKED)
    children = [name(leaf) for leaf in leaves(folder)]
    calls = []
    original = review._set_spot_inclusions
    monkeypatch.setattr(review, "_set_spot_inclusions", lambda changes: (calls.append(dict(changes)),
                                                                          original(changes)))
    folder.setCheckState(0, UNCHECKED)
    settle(app)
    assert len(calls) == 1 and set(calls[0]) == set(children)
    assert not any(review.include_flags[spot] for spot in children)
    assert all(spot not in {p[0] for p in review.spot_concordia._points} for spot in children)
    assert folder.data(0, SPOT_TREE_COUNT_ROLE) == f"0/{len(children)}"
    folder.setCheckState(0, CHECKED)
    settle(app)
    assert all(review.include_flags[spot] for spot in children)


def test_concordia_list_changes_show_in_the_tree(window, app):
    review = window.results_table
    review.clear_spot_choices()
    settle(app)
    assert all(leaf.checkState(0) == UNCHECKED for folder in folders(window) for leaf in leaves(folder))
    review.set_all_spot_choices(True)
    settle(app)
    assert all(leaf.checkState(0) == CHECKED for folder in folders(window) for leaf in leaves(folder)
               if review.spot_shown(name(leaf)))


def test_clicking_the_box_does_not_browse_and_clicking_the_name_does_not_tick(window, app):
    tree = window.spot_list
    leaf_items = leaves(next(item for item in folders(window) if item.childCount() >= 2))
    first, second = leaf_items[0], leaf_items[1]
    tree.setCurrentItem(first)
    settle(app)
    tree.scrollToItem(second)
    settle(app)
    index = tree.indexFromItem(second)
    box = tree.check_rect(index).center()
    before = second.checkState(0)
    QTest.mouseClick(tree.viewport(), QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier, box)
    settle(app)
    assert tree.currentItem() is first
    assert second.checkState(0) != before
    assert window.results_table.include_flags[name(second)] == (second.checkState(0) == CHECKED)
    text_point = tree.visualItemRect(second).center()
    after = second.checkState(0)
    QTest.mouseClick(tree.viewport(), QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier, text_point)
    settle(app)
    assert tree.currentItem() is second
    assert second.checkState(0) == after


def test_filter_hides_rows_without_changing_ticks(window, app):
    review = window.results_table
    flags = dict(review.include_flags)
    target = name(leaves(folders(window)[0])[0])
    window.spot_tree_filter.setText(target[-4:])
    settle(app)
    visible = [name(leaf) for folder in folders(window) for leaf in leaves(folder) if not leaf.isHidden()]
    assert target in visible and all(target[-4:] in spot for spot in visible)
    window.spot_tree_filter.clear()
    settle(app)
    assert review.include_flags == flags
    assert all(not leaf.isHidden() for folder in folders(window) for leaf in leaves(folder))


def test_saved_project_reopens_with_the_same_displayed_spots(window, app, tmp_path):
    review = window.results_table
    shown_before = {spot for spot, shown in review.include_flags.items() if shown}
    project = tmp_path / "ticks.upbr"
    window.save_project(project)
    window.load_project_file(project, background=False)
    window.session.reduction_cache["finalized_results"] = window.session.reduction_cache.get(
        "finalized_results") or finalized_for(sorted(shown_before), ["S"] * len(shown_before))
    window.set_plot_mode("final_results", refresh=True)
    settle(app)
    shown_after = {spot for spot, shown in window.results_table.include_flags.items() if shown}
    assert shown_after == shown_before
    for folder in folders(window):
        for leaf in leaves(folder):
            assert (leaf.checkState(0) == CHECKED) == window.results_table.spot_shown(name(leaf))
