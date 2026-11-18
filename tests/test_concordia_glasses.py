"""Glass defaults and bulk inclusion in final concordia review."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtTest, QtWidgets

from GUI.theme import THEMES, build_application
from GUI.widgets import ResultsTableWidget
from UPbRPy.isochron import tera_wasserburg_point, wetherill_point


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


def results(names=("grain_1", "grain_2", "glass_3", "glass_4"),
            samples=("91500", "91500", "NIST610", "NIST612")):
    # Labels deliberately differ from samples: classification must use groups.
    ages = np.array([500. if sample == "91500" else 900. for sample in samples])
    x, y = wetherill_point(ages)
    tx, ty = tera_wasserburg_point(ages)
    frame = pd.DataFrame({"spot_name": names, "sample": samples,
                          "207Pb/235U": x, "206Pb/238U": y,
                          "238U/206Pb": tx, "207Pb/206Pb": ty})
    for ratio in ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb"):
        frame[f"{ratio}_2e"] = frame[ratio] * .02
    for system in ("206Pb/238U", "207Pb/235U", "207Pb/206Pb"):
        frame[system + "_age"] = ages
        frame[system + "_age_2e"] = 10.
    return SimpleNamespace(parallel_results=frame, selected_results=frame.copy())


@pytest.fixture
def review(app):
    widget = ResultsTableWidget()
    try:
        widget.apply_theme(THEMES["light"])
        widget.show_results(results())
        widget.tabs.setCurrentWidget(widget.spot_concordia_page)
        widget.resize(800, 550)
        widget.show()
        app.processEvents()
        yield widget
    finally:
        widget.close()
        widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def assert_inclusion(widget, expected):
    assert widget.include_flags == expected
    checked = QtCore.Qt.CheckState.Checked
    unchecked = QtCore.Qt.CheckState.Unchecked
    for row in range(widget.results_model.rowCount()):
        name = widget.results_model.spot_name(row)
        assert widget.results_model.data(widget.results_model.index(row, 0),
                                         QtCore.Qt.ItemDataRole.CheckStateRole) == (
                                             checked if expected[name] else unchecked)
    for row in range(widget.spot_choice_list.count()):
        item = widget.spot_choice_list.item(row)
        assert item.checkState() == (checked if expected[item.text()] else unchecked)
    model = widget.spot_choice_group_model
    for row in range(model.rowCount()):
        parent = model.item(row)
        for child in range(parent.rowCount()):
            item = parent.child(child)
            assert item.checkState() == (checked if expected[item.text()] else unchecked)
    assert {name for name, *_ in widget.spot_concordia._points} == {
        name for name, included in expected.items() if included}


def test_glasses_default_excluded_from_all_spot_views_and_readout(review):
    assert not review.show_glasses()
    assert not review.spot_glasses_toggle.isHidden()
    assert not review.depth_glasses_toggle.isHidden()
    assert_inclusion(review, {"grain_1": True, "grain_2": True,
                              "glass_3": False, "glass_4": False})
    assert review.weighted_mean_dates["206Pb/238U"]["n"] == 2
    assert review.weighted_mean_dates["206Pb/238U"]["weighted_mean"] == pytest.approx(500., abs=.001)


def test_default_materials_come_from_current_archive(app, monkeypatch):
    monkeypatch.setattr("GUI.widgets.current_standard_archive", lambda: pd.DataFrame(
        {"material": ["glass", "zircon"]}, index=["custom", "NIST610"]))
    widget = ResultsTableWidget()
    try:
        widget.show_results(results(("a", "b"), ("custom", "NIST610")))
        assert widget.include_flags == {"a": False, "b": True}
    finally:
        widget.close()
        widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_user_toggles_sync_pages_and_readout_and_allow_manual_ticks(review, app):
    spy = QtTest.QSignalSpy(review.showGlassesChanged)
    original = review.weighted_mean_dates["206Pb/238U"]
    review.spot_glasses_toggle.click()
    app.processEvents()
    assert spy.count() == 1 and spy.at(0) == [True]
    assert review.show_glasses() and review.depth_glasses_toggle.isChecked()
    assert_inclusion(review, dict.fromkeys(review.include_flags, True))
    assert review.weighted_mean_dates["206Pb/238U"]["n"] == 4
    assert review.weighted_mean_dates["206Pb/238U"]["weighted_mean"] == pytest.approx(700.)
    assert review.weighted_mean_dates["206Pb/238U"]["weighted_mean"] != pytest.approx(original["weighted_mean"])
    assert review.weighted_mean_dates["206Pb/238U"]["mswd"] > original["mswd"]
    review.depth_glasses_toggle.click()
    app.processEvents()
    assert spy.count() == 2 and spy.at(1) == [False]
    assert not review.spot_glasses_toggle.isChecked()
    assert review.weighted_mean_dates["206Pb/238U"]["n"] == 2
    item = review.spot_choice_list.findItems("glass_3", QtCore.Qt.MatchFlag.MatchExactly)[0]
    item.setCheckState(QtCore.Qt.CheckState.Checked)
    app.processEvents()
    assert review.include_flags["glass_3"] and not review.include_flags["glass_4"]
    assert not review.show_glasses()
    assert review.weighted_mean_dates["206Pb/238U"]["n"] == 3
    assert spy.count() == 2


def test_set_show_glasses_is_silent_and_round_trips(review, app):
    spy = QtTest.QSignalSpy(review.showGlassesChanged)
    review.set_spot_included({"grain_2"}, False)
    for enabled in (True, False, True):
        review.set_show_glasses(enabled)
        app.processEvents()
        assert review.show_glasses() is enabled
        assert review.spot_glasses_toggle.isChecked() is enabled
        assert review.depth_glasses_toggle.isChecked() is enabled
        assert review.include_flags["glass_3"] is enabled
        assert review.include_flags["glass_4"] is enabled
        assert not review.include_flags["grain_2"]
    assert spy.count() == 0


def test_new_results_preserve_manual_flags_and_default_only_new_spots(review):
    review.set_spot_included({"grain_1"}, False)
    review.set_spot_included({"glass_3"}, True)
    names = ("grain_1", "glass_3", "new_glass", "new_zircon")
    samples = ("91500", "NIST610", "NIST612", "91500")
    review.show_results(results(names, samples))
    assert_inclusion(review, {"grain_1": False, "glass_3": True,
                              "new_glass": False, "new_zircon": True})
    review.set_show_glasses(True)
    review.set_spot_included({"glass_3"}, False)
    review.show_results(results((*names, "another_glass"), (*samples, "NIST610")))
    assert_inclusion(review, {"grain_1": False, "glass_3": False,
                              "new_glass": True, "new_zircon": True, "another_glass": True})


def test_material_override_replaces_archive_classification(review):
    spy = QtTest.QSignalSpy(review.showGlassesChanged)
    review.set_glass_materials({"91500"})
    assert_inclusion(review, {"grain_1": False, "grain_2": False,
                              "glass_3": True, "glass_4": True})
    review.set_show_glasses(True)
    assert all(review.include_flags.values())
    review.set_glass_materials([])
    assert review.spot_glasses_toggle.isHidden()
    assert review.depth_glasses_toggle.isHidden()
    assert spy.count() == 0


def test_no_glass_results_hide_toggles(review):
    review.show_results(results(("grain_1", "grain_2"), ("91500", "91500")))
    assert review.spot_glasses_toggle.isHidden()
    assert review.depth_glasses_toggle.isHidden()
    assert_inclusion(review, {"grain_1": True, "grain_2": True})


def test_override_and_toggle_can_be_restored_before_results(review, app):
    review.show_results(results((), ()))
    review.set_glass_materials({"custom"})
    review.set_show_glasses(True)
    review.show_results(results(("custom_1", "glass_1"), ("custom", "NIST610")))
    assert_inclusion(review, {"custom_1": True, "glass_1": True})
    review.set_show_glasses(False)
    app.processEvents()
    assert_inclusion(review, {"custom_1": False, "glass_1": True})


def test_depth_glass_plot_obeys_inclusion_and_page_toggle(review, app):
    frame = results().parallel_results.rename(columns={
        name: name + "_fractionation_corrected" for name in
        ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb")})
    frame["signal_relative_time"] = np.arange(len(frame), dtype=float)
    review.show_depth_profile(frame, "glass_3")
    review.tabs.setCurrentWidget(review.depth_concordia_page)
    app.processEvents()
    assert review.depth_concordia._frame.empty
    review.depth_glasses_toggle.click()
    app.processEvents()
    assert list(review.depth_concordia._frame.spot_name) == ["glass_3"]
    review.set_spot_included({"glass_3"}, False)
    assert review.depth_concordia._frame.empty
    review.depth_spot_combo.setCurrentText("grain_1")
    assert list(review.depth_concordia._frame.spot_name) == ["grain_1"]


def test_glass_controls_fit_chooser_at_laptop_size(review, app):
    review.resize(600, 500)  # Plot column within a 1280 by 800 Studio window.
    app.processEvents()
    page = review.spot_concordia_page
    assert page.horizontalScrollBar().maximum() == 0
    menu = review.spot_spots_button.menu()
    menu.popup(review.mapToGlobal(QtCore.QPoint(20, 20)))
    app.processEvents()
    for control in (review.spot_group_toggle, review.spot_glasses_toggle):
        assert menu.rect().contains(QtCore.QRect(control.mapTo(menu, QtCore.QPoint()), control.size()))
    assert not review.spot_group_toggle.geometry().intersects(review.spot_glasses_toggle.geometry())
    assert review._chooser_stack.height() >= 80
    menu.hide()
