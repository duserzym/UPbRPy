"""Spot label ordering and per-project folder expansion state."""

import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore

from test_spot_tree_checks import app, folders, name, settle, window
from GUI.widgets import SPOT_TREE_COUNT_ROLE
from UPbRPy.naming import natural_sort_key


def roots(window):
    return [window.spot_list.topLevelItem(i) for i in range(window.spot_list.topLevelItemCount())]


def test_spots_and_folders_use_natural_order(window):
    names = [f"LV25-2_S_{n}" for n in (100, 11, 1, 10, 2)]
    names += ["sample10_1", "sample2_1", "91500-12", "91500-3"]
    samples = ["LV25-2"] * 5 + ["sample10", "sample2", "91500", "91500"]
    window.session.experiment = pd.DataFrame({"spot_names": names})
    window.session.total_data = pd.DataFrame({"spot_name": names, "sample": samples})
    window.session.selected_spot = None
    window._populate_spot_tree()
    assert window.session.spot_names == names
    for root in roots(window):
        labels = [root.child(i).text(0) for i in range(root.childCount())]
        assert labels == sorted(labels, key=natural_sort_key)
    for folder in folders(window):
        labels = [name(folder.child(i)) for i in range(folder.childCount())]
        assert labels == sorted(labels, key=natural_sort_key)


def test_toggle_keeps_roots_open_and_survives_rebuild(window, app):
    button = window.spot_tree_expand_button
    assert button.toolTip() == "Collapse all groups"
    icon = button.icon().cacheKey()
    counts = [folder.data(0, SPOT_TREE_COUNT_ROLE) for folder in folders(window)]
    button.click()
    assert all(root.isExpanded() for root in roots(window))
    assert all(not folder.isExpanded() for folder in folders(window))
    assert button.toolTip() == "Expand all groups"
    assert button.icon().cacheKey() != icon
    window._populate_spot_tree()
    settle(app)
    assert all(not folder.isExpanded() for folder in folders(window))
    assert [folder.data(0, SPOT_TREE_COUNT_ROLE) for folder in folders(window)] == counts
    button.click()
    assert all(folder.isExpanded() for folder in folders(window))
    assert button.toolTip() == "Collapse all groups"


def test_individual_folders_filter_and_new_groups(window, app):
    folder = folders(window)[0]
    label = name(folder)
    target = name(folder.child(0))
    folder.setExpanded(False)
    window.spot_tree_filter.setText(target)
    window._populate_spot_tree()
    assert not next(folder for folder in folders(window) if name(folder) == label).isExpanded()
    window.spot_tree_filter.clear()
    window._spot_folder_overrides[window.session.spot_names[-1]] = "new group"
    window._populate_spot_tree()
    assert next(folder for folder in folders(window) if folder.text(0) == "new group").isExpanded()
    assert all(root.isExpanded() for root in roots(window))


def test_collapsed_folders_save_and_reopen(window, app, tmp_path):
    folder = folders(window)[0]
    collapsed_name = name(folder)
    folder.setExpanded(False)
    project = tmp_path / "folders.upbr"
    window.save_project(project)
    window.spot_tree_expand_button.click()
    window.load_project_file(project, background=False)
    settle(app)
    assert not next(folder for folder in folders(window) if name(folder) == collapsed_name).isExpanded()
    assert all(folder.isExpanded() for folder in folders(window) if name(folder) != collapsed_name)
    assert all(root.isExpanded() for root in roots(window))


@pytest.mark.parametrize("size", [(1440, 900), (1280, 800)])
def test_tree_header_fits_narrow_inspector(window, app, size):
    window.resize(*size)
    window.workspace_splitter.setSizes([220, size[0] - 460, 240])
    window.spot_tree_count_label.setText("194/194")
    settle(app)
    buttons = [window.spot_tree_all_button, window.spot_tree_none_button,
               window.spot_tree_expand_button]
    section = window.spot_tree_expand_button.parentWidget()
    assert window.size().width() == size[0]
    assert section.width() <= 260
    for button in buttons:
        assert section.rect().contains(button.geometry())
    assert buttons[0].geometry().right() < buttons[1].geometry().left()
    assert buttons[1].geometry().right() < buttons[2].geometry().left()
    label = window.spot_tree_count_label
    assert label.width() >= label.fontMetrics().horizontalAdvance(label.text())


def test_review_choosers_sort_names_naturally(window, app):
    review = window.results_table
    names = ["S10_11", "S2_10", "S2_2"]
    from test_spot_tree_checks import finalized_for

    review.show_results(finalized_for(names, ["S10", "S2", "S2"]))
    review.spot_choice_tree.ensure_populated()
    model = review.spot_choice_tree.model()
    parent = model.index(0, 0)
    assert parent.data().startswith("S2 ·")
    assert [model.index(i, 0, parent).data() for i in range(model.rowCount(parent))] == ["S2_2", "S2_10"]
    table = review.results_table
    source = table.model().sourceModel()
    column = list(source.frame.columns).index("spot_name") + 1
    table.sortByColumn(column, QtCore.Qt.SortOrder.AscendingOrder)
    assert [table.model().index(i, column).data() for i in range(table.model().rowCount())] == [
        "S2_2", "S2_10", "S10_11",
    ]
