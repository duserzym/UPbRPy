"""Clicking a concordia ellipse selects that spot and shows its spot card."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI.theme import THEMES, build_application
from GUI.widgets import ResultsTableWidget
from UPbRPy.isochron import tera_wasserburg_point, wetherill_point
from UPbRPy.results import format_value_with_uncertainty


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.fixture
def finalized():
    # "big" and "small" share a centre region; "small" lies inside "big".
    # "far" is on its own.
    ages = np.array([500.0, 500.0, 900.0])
    x, y = wetherill_point(ages)
    tx, ty = tera_wasserburg_point(ages)
    frame = pd.DataFrame({"spot_name": ["big", "small", "far"], "sample": ["A", "A", "B"],
                          "207Pb/235U": x, "206Pb/238U": y, "238U/206Pb": tx, "207Pb/206Pb": ty,
                          "206Pb/238U_age": ages, "206Pb/238U_age_2e": [20.0, 4.0, 9.0],
                          "U": [310.0, 205.0, 990.0], "Th/U": [0.41, 0.33, 0.8], "disc%": [1.2, -0.4, 3.0]})
    scale = {"big": 0.06, "small": 0.006, "far": 0.01}
    for ratio in ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb"):
        for branch in ("with_calibration", "no_calibration"):
            frame[f"{ratio}_2e_{branch}"] = frame[ratio] * frame.spot_name.map(scale)
    selected = frame[["spot_name", "sample"]].assign(
        preferred_date=ages, preferred_date_2e=[20.0, 4.0, 9.0],
        preferred_date_method="206Pb/238U", automatic_flags=["", "high uncertainty", ""])
    return SimpleNamespace(parallel_results=frame, selected_results=selected,
                           decision=SimpleNamespace(common_pb_method="none",
                                                    include_calibration_uncertainty=True,
                                                    age_transition_ma=1000.0))


@pytest.fixture
def review(app, finalized):
    widget = ResultsTableWidget()
    widget.apply_theme(THEMES["light"])
    widget.show_results(finalized)
    widget.tabs.setCurrentWidget(widget.spot_concordia_page)
    widget.resize(1100, 650)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def scene_at(canvas, x, y):
    return canvas._plot_item.vb.mapViewToScene(QtCore.QPointF(x, y))


def centre(canvas, name):
    return next((x, y) for point_name, x, y, _ in canvas._points if point_name == name)


def offset_inside(canvas, name, fraction=0.5):
    """A point inside only the big ellipse: halfway to its edge, away from centres."""
    names, boxes, _areas, _polygons = canvas._ellipse_hit_data()
    box = boxes[names.index(name)]
    x, y = centre(canvas, name)
    return x + fraction * 0.5 * (box[1] - x), y


@pytest.mark.parametrize("diagram", ["wetherill", "tera_wasserburg"])
def test_hit_test_picks_smallest_and_cycles_through_overlaps(review, app, diagram):
    review.spot_diagram_buttons[diagram].click()
    app.processEvents()
    canvas = review.spot_concordia
    canvas._plot_item.vb.autoRange()
    app.processEvents()
    x, y = centre(canvas, "small")
    hits = canvas.spots_at(scene_at(canvas, x, y))
    assert hits[:2] == ["small", "big"]  # smaller ellipse first
    no = QtCore.Qt.KeyboardModifier.NoModifier
    canvas._select_at(scene_at(canvas, x, y), no)
    assert canvas.selected_spots == {"small"}
    canvas._select_at(scene_at(canvas, x, y), no)
    assert canvas.selected_spots == {"big"}  # a repeated click steps to the next ellipse
    canvas._select_at(scene_at(canvas, x, y), no)
    assert canvas.selected_spots == {"small"}
    bx, by = offset_inside(canvas, "big")
    assert canvas.spots_at(scene_at(canvas, bx, by)) == ["big"]
    # Cmd/Ctrl-click adds; a click on empty plot area clears.
    fx, fy = centre(canvas, "far")
    canvas._select_at(scene_at(canvas, fx, fy), QtCore.Qt.KeyboardModifier.ControlModifier)
    assert canvas.selected_spots == {"small", "far"}
    view = canvas._plot_item.vb.viewRect()
    canvas._select_at(scene_at(canvas, view.left() + 0.01 * view.width(),
                               view.top() + 0.99 * view.height()), no)
    assert canvas.selected_spots == set()


def test_selection_highlight_updates_without_redrawing(review, app):
    canvas = review.spot_concordia
    scatter, batch = canvas._scatter, canvas._ellipse_batch
    review.set_selected_spots({"far"})
    assert canvas._scatter is scatter and canvas._ellipse_batch is batch
    assert not canvas._highlight.path().isEmpty()
    assert canvas._highlight.zValue() > batch.zValue()
    review.set_selected_spots(set())
    assert canvas._highlight.path().isEmpty()


def test_spot_card_matches_results_table_and_toggles_exclusion(review, app, finalized):
    canvas = review.spot_concordia
    x, y = centre(canvas, "small")
    canvas._select_at(scene_at(canvas, x, y), QtCore.Qt.KeyboardModifier.NoModifier)
    app.processEvents()
    assert review.selected_spots == {"small"}
    card = review.spot_card
    assert card.isVisible() and card.spot_name() == "small"
    text = card.body.text()
    row = finalized.selected_results.set_index("spot_name").loc["small"]
    assert format_value_with_uncertainty(row.preferred_date, row.preferred_date_2e) in text
    assert "206Pb/238U" in text and "high uncertainty" in text and "205" in text
    assert "1000 Ma" in text  # the method footnote
    # The results table selects the same row.
    selected_rows = review.results_table.selectionModel().selectedRows()
    assert {review.results_model.spot_name(review.results_table.model().mapToSource(i).row())
            for i in selected_rows} == {"small"}
    card.exclude_button.click()
    assert "small" in review.excluded_spots
    assert card.exclude_button.text() == "Include"
    card.exclude_button.click()
    assert "small" not in review.excluded_spots
    shown = []
    review.showSignalRequested.connect(shown.append)
    card.show_signal_button.click()
    assert shown == ["small"]
    review.set_selected_spots({"small", "far"})
    app.processEvents()
    assert "2 spots selected" in card.body.text()
    review.set_selected_spots(set())
    app.processEvents()
    assert not card.isVisible()


def test_studio_tree_and_concordia_selection_follow_each_other(app, monkeypatch, finalized):
    from pathlib import Path
    from GUI import main_window
    from GUI.main_window import MainWindow

    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    rich = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice/Zircon_04Nov26_slice_Rich.csv"
    window = MainWindow(theme_mode="light")
    try:
        window.resize(1280, 800)
        window.show()
        window.load_experiment_directory(str(rich), background=False)
        names = [str(name) for name in window.session.spot_names[:3]]
        renamed = {old: new for old, new in zip(["big", "small", "far"], names)}
        results = SimpleNamespace(
            parallel_results=finalized.parallel_results.replace({"spot_name": renamed}),
            selected_results=finalized.selected_results.replace({"spot_name": renamed}),
            decision=finalized.decision)
        window.session.reduction_cache["finalized_results"] = results
        window.set_plot_mode("final_results", refresh=True)
        window.results_table.tabs.setCurrentWidget(window.results_table.spot_concordia_page)
        app.processEvents()
        calls = []
        original = window.results_table.show_results
        monkeypatch.setattr(window.results_table, "show_results",
                            lambda *a, **k: (calls.append(1), original(*a, **k)))
        # Plot -> tree, without rebuilding the results view.
        window.results_table.set_selected_spots({names[2]})
        assert window.current_spot_name() == names[2]
        app.processEvents()  # the card follows on the next event-loop turn
        assert window.results_table.spot_card.spot_name() == names[2]
        # Tree -> plot.
        window.spot_list.setCurrentItem(window._spot_tree_item(names[1]))
        assert window.results_table.selected_spots == {names[1]}
        assert window.results_table.spot_concordia.selected_spots == {names[1]}
        assert calls == []
        # Show signal jumps to the Browse view of that spot.
        app.processEvents()
        window.results_table.spot_card.show_signal_button.click()
        assert window.session.plot_mode == "raw" and window.current_spot_name() == names[1]
    finally:
        window.has_unsaved_changes = lambda: False
        window.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
