"""Quick sample inclusion, shared chip state and bounded wrapping in reviews."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from GUI.theme import THEMES, build_application
from GUI.widgets import ResultsTableWidget
from UPbRPy.isochron import concordia_age_readout, tera_wasserburg_point, wetherill_point


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


def results(samples=("Zulu", "91500", "Zulu", "Beta", "91500", "NIST610", "Beta")):
    ages = np.array([500. + 100. * list(dict.fromkeys(samples)).index(sample)
                     for sample in samples])
    x, y = wetherill_point(ages)
    tx, ty = tera_wasserburg_point(ages)
    frame = pd.DataFrame({"spot_name": [f"spot_{i}" for i in range(len(samples))],
                          "sample": samples, "207Pb/235U": x, "206Pb/238U": y,
                          "238U/206Pb": tx, "207Pb/206Pb": ty})
    for ratio in ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb"):
        frame[f"{ratio}_2e"] = frame[ratio] * .02
    return SimpleNamespace(parallel_results=frame, selected_results=frame.copy())


@pytest.fixture
def review(app):
    widget = ResultsTableWidget()
    widget.apply_theme(THEMES["light"])
    widget.show_results(results())
    widget.tabs.setCurrentWidget(widget.spot_concordia_page)
    widget.resize(600, 550)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def included_samples(widget):
    return {widget._sample_spots()[name] for name, included in widget.include_flags.items() if included}


def click(widget, sample, app, modifier=QtCore.Qt.KeyboardModifier.NoModifier, *, depth=False):
    area = widget.depth_sample_chips if depth else widget.spot_sample_chips
    area.ensureWidgetVisible(area.chips[sample])
    QtTest.QTest.mouseClick(area.chips[sample], QtCore.Qt.MouseButton.LeftButton, modifier)
    app.processEvents()


def depth_frame():
    frame = results().parallel_results
    # Multiple sweeps per spot must count as one spot per chip.
    frame = pd.concat([frame, frame], ignore_index=True)
    frame["signal_relative_time"] = np.arange(len(frame), dtype=float)
    return frame.rename(columns={column: column + "_fractionation_corrected"
                                for column in frame if "/" in column})


def test_chip_order_counts_colors_and_glass_visibility(review, app):
    assert review.sample_chip_names() == ["Zulu", "91500", "Beta", "NIST610"]
    chips = review.spot_sample_chips.chips
    assert [chip.text() for chip in chips.values()] == ["Zulu 2", "91500 2", "Beta 2", "NIST610 1"]
    for sample, chip in chips.items():
        assert chip.sample_color == QtGui.QColor(review.spot_concordia._sample_colors[sample])
    assert chips["NIST610"].isHidden()
    review.set_show_glasses(True)
    app.processEvents()
    assert not chips["NIST610"].isHidden()
    assert chips["NIST610"].property("inclusionState") == "active"
    review.set_show_glasses(False)
    assert chips["NIST610"].isHidden()


@pytest.mark.parametrize("sorted_selected", [False, True])
def test_chip_order_retains_input_acquisition_order_after_display_sort(review, sorted_selected):
    samples = ("NIST612", "S513", "NIST612", "K26", "S513", "91500", "K26")
    data = results(samples)
    if sorted_selected:
        data.selected_results = data.selected_results.sort_values("sample")
    original = data.parallel_results.copy(deep=True)
    review.show_results(data)
    expected = ["NIST612", "S513", "K26", "91500"]
    assert review.sample_chip_names() == expected
    assert list(review.spot_sample_chips.chips) == expected
    review.results_table.sortByColumn(2, QtCore.Qt.SortOrder.DescendingOrder)
    review._rebuild_sample_chips()
    assert review.sample_chip_names() == expected
    pd.testing.assert_frame_equal(data.parallel_results, original)


def test_depth_chips_follow_depth_frame_order_independently(review):
    samples = ("NIST612", "S513", "NIST612", "K26", "91500", "S513")
    data = results(samples)
    review.show_results(data)
    frame = data.parallel_results.iloc[[4, 3, 1, 0, 2, 5]]
    review.show_depth_profile(frame, "spot_0")
    assert list(review.depth_sample_chips.chips) == ["91500", "K26", "S513", "NIST612"]


def test_chip_replaces_all_inclusion_and_pools_whole_sample(review, app):
    review.set_selected_spots({"spot_1"})
    review.spot_filter.setText("spot_1")
    click(review, "Zulu", app)
    assert included_samples(review) == {"Zulu"}
    assert not review.selected_spots
    assert {name for name, *_ in review.spot_concordia._points} == {"spot_0", "spot_2"}
    assert review.concordia_readout.n == 2
    expected = concordia_age_readout(results().parallel_results.query("sample == 'Zulu'"))
    assert review.concordia_readout.age_ma == pytest.approx(expected.age_ma)
    assert review.spot_sample_chips.chips["Zulu"].property("inclusionState") == "active"
    assert review.spot_sample_chips.chips["91500"].property("inclusionState") == "inactive"


def test_control_click_adds_then_removes_whole_sample(review, app):
    click(review, "Zulu", app)
    modifier = QtCore.Qt.KeyboardModifier.ControlModifier  # Command on macOS in Qt.
    click(review, "91500", app, modifier)
    assert included_samples(review) == {"Zulu", "91500"}
    assert review.concordia_readout.n == 4
    click(review, "91500", app, modifier)
    assert included_samples(review) == {"Zulu"}
    assert review.concordia_readout.n == 2


def test_shift_click_adds_run_order_range_in_both_directions(review, app):
    click(review, "Zulu", app)
    click(review, "Beta", app, QtCore.Qt.KeyboardModifier.ShiftModifier)
    assert included_samples(review) == {"Zulu", "91500", "Beta"}
    review.clear_spot_choices()
    click(review, "91500", app, QtCore.Qt.KeyboardModifier.ShiftModifier)
    assert included_samples(review) == {"91500", "Beta"}


def test_public_modes_toggle_partial_groups_and_reject_bad_mode(review):
    review.show_samples(["Zulu"])
    review.set_spot_included(["spot_0"], False)
    review.show_samples(["Zulu"], mode="toggle")
    assert review.include_flags["spot_0"] and review.include_flags["spot_2"]
    review.show_samples(["91500"], mode="add")
    assert included_samples(review) == {"Zulu", "91500"}
    review.show_samples(["Zulu", "91500"], mode="toggle")
    assert not any(review.include_flags.values())
    with pytest.raises(ValueError, match="mode"):
        review.show_samples(["Zulu"], mode="bad")


@pytest.mark.parametrize("page_name", ["spot_concordia_page", "depth_concordia_page"])
def test_one_clear_and_all_per_page_clear_everything_and_obey_glasses(review, app, page_name):
    review.show_depth_profile(depth_frame(), "spot_0")
    page = getattr(review, page_name)
    review.tabs.setCurrentWidget(page)
    review.spot_filter.setText("spot_1")
    review.set_show_glasses(True)
    app.processEvents()
    buttons = page.findChildren(QtWidgets.QPushButton)
    clear = [button for button in buttons if button.text() == "Clear"]
    all_buttons = [button for button in buttons if button.text() == "All"]
    assert len(clear) == len(all_buttons) == 1
    assert not any(button.text() == "None" for button in buttons)
    clear[0].click()
    app.processEvents()
    assert not any(review.include_flags.values())
    assert not review.spot_concordia._points
    assert review.depth_concordia._frame.empty
    assert not review.selected_spots
    assert review.concordia_readout is None
    assert review.concordia_age_label.text() == ""
    all_buttons[0].click()
    app.processEvents()
    assert all(review.include_flags.values())
    assert review.show_glasses()
    review.set_show_glasses(False)
    # Manual glass ticks remain possible; All restores the toggle's rule.
    review.set_spot_included(["spot_5"], True)
    review.clear_spot_choices()
    review.set_spot_included(["spot_5"], True)
    all_buttons[0].click()
    app.processEvents()
    assert included_samples(review) == {"Zulu", "91500", "Beta"}
    assert review.concordia_readout.n == 6


def test_table_chooser_and_group_checks_update_chip_states(review, app):
    chip = review.spot_sample_chips.chips["Zulu"]
    role = QtCore.Qt.ItemDataRole.CheckStateRole
    assert chip.property("inclusionState") == "active"
    proxy = review.results_table.model()
    index = next(proxy.index(row, 0) for row in range(proxy.rowCount())
                 if review.results_model.spot_name(proxy.mapToSource(proxy.index(row, 0)).row()) == "spot_0")
    assert proxy.setData(index, QtCore.Qt.CheckState.Unchecked, role)
    app.processEvents()
    assert chip.property("inclusionState") == "partial"
    item = review.spot_choice_list.findItems("spot_2", QtCore.Qt.MatchFlag.MatchExactly)[0]
    item.setCheckState(QtCore.Qt.CheckState.Unchecked)
    assert chip.property("inclusionState") == "inactive"
    model = review.spot_choice_group_model
    parent = next(model.item(row) for row in range(model.rowCount())
                  if model.item(row).text().startswith("Zulu"))
    parent.setCheckState(QtCore.Qt.CheckState.Checked)
    assert chip.property("inclusionState") == "active"


def test_sample_changes_refresh_each_plot_and_fit_once(review, app, monkeypatch):
    import GUI.widgets as widgets

    calls = []
    for method in ("refresh_spot_concordia", "refresh_depth_concordia"):
        original = getattr(review, method)

        def refresh(*args, original=original, method=method, **kwargs):
            calls.append(method)
            return original(*args, **kwargs)

        monkeypatch.setattr(review, method, refresh)
    original_fit = widgets.concordia_age_readout

    def fit(*args, **kwargs):
        calls.append("fit")
        return original_fit(*args, **kwargs)

    monkeypatch.setattr(widgets, "concordia_age_readout", fit)
    for action in (lambda: review.show_samples(["Zulu", "Beta"]),
                   review.clear_spot_choices, lambda: review.set_all_spot_choices(True),
                   lambda: review.set_show_glasses(True),
                   lambda: review.set_glass_materials(["Zulu"])):
        calls.clear()
        action()
        app.processEvents()
        assert calls == ["refresh_spot_concordia", "refresh_depth_concordia", "fit"]


def test_depth_chips_count_spots_and_show_whole_sample(review, app):
    review.show_depth_profile(depth_frame(), "spot_1")
    review.tabs.setCurrentWidget(review.depth_concordia_page)
    app.processEvents()
    assert review.depth_sample_chips.chips["Zulu"].text() == "Zulu 2"
    click(review, "Zulu", app, depth=True)
    assert set(review.depth_concordia._frame.spot_name) == {"spot_0", "spot_2"}
    assert len(review.depth_concordia._frame) == 4
    assert review.concordia_readout.n == 2
    assert review.depth_sample_chips.chips["91500"].property("inclusionState") == "inactive"
    review.depth_spot_combo.setCurrentText("spot_2")
    assert set(review.depth_concordia._frame.spot_name) == {"spot_2"}


def test_depth_only_samples_and_missing_sample_data(review, app):
    frame = depth_frame()
    frame["sample"] = "depth only"
    frame["spot_name"] = "depth_1"
    review.show_depth_profile(frame, "depth_1")
    assert review.sample_chip_names()[-1] == "depth only"
    assert list(review.depth_sample_chips.chips) == ["depth only"]
    review.tabs.setCurrentWidget(review.depth_concordia_page)
    app.processEvents()
    click(review, "depth only", app, depth=True)
    assert set(review.depth_concordia._frame.spot_name) == {"depth_1"}
    review.clear_spot_choices()
    assert review.depth_concordia._frame.empty
    review.show_depth_profile(frame.drop(columns="sample"), "depth_1")
    assert not review.depth_sample_chips.chips
    assert review.depth_sample_chips.isHidden()


@pytest.mark.parametrize("page_name,area_name", [("spot_concordia_page", "spot_sample_chips"),
                                                ("depth_concordia_page", "depth_sample_chips")])
def test_twenty_chips_wrap_scroll_and_fit_narrow_pages(review, app, page_name, area_name):
    samples = [f"Sample {i:02d}" for i in range(19)] + ["A very long sample name " * 5]
    data = results(samples)
    review.show_results(data)
    review.show_depth_profile(data.parallel_results, "spot_0")
    page = getattr(review, page_name)
    area = getattr(review, area_name)
    review.tabs.setCurrentWidget(page)
    review.resize(600, 550)
    app.processEvents()
    assert len(area.chips) == 20
    assert len({chip.y() for chip in area.chips.values()}) > 1
    assert area.verticalScrollBar().maximum() > 0
    assert area.height() <= 3 * max(26, area.fontMetrics().height() + 10) + 8
    for chip in area.chips.values():
        assert 0 < chip.width() <= area.viewport().width()
        assert chip.geometry().right() < area.widget().width()
        assert chip.height() >= chip.height_for_width(chip.width())
    assert page.horizontalScrollBar().maximum() == 0
    if page_name == "spot_concordia_page":
        assert review._chooser_stack.height() >= 80


@pytest.mark.parametrize("size", [(1280, 800), (1440, 900)])
def test_nine_samples_fit_full_width_strips_in_two_rows_in_studio(app, tmp_path, monkeypatch, size):
    from GUI.studio_window import StudioWindow

    settings = QtCore.QSettings(str(tmp_path / "studio.ini"), QtCore.QSettings.Format.IniFormat)
    monkeypatch.setattr(QtCore, "QSettings", lambda *args, **kwargs: settings)
    shell = StudioWindow()
    try:
        window = shell.active_reduction
        widget = window.results_table
        samples = ("SAMPLE-B", "91500", "SAMPLE-A", "LTSE", "NIST610",
                   "NIST612", "PL", "S513", "Seiland")
        counts = (30, 25, 32, 22, 9, 9, 12, 22, 84)
        data = results(tuple(sample for sample, count in zip(samples, counts)
                             for _ in range(count)))
        widget.show_results(data)
        widget.show_depth_profile(data.parallel_results, "spot_0")
        widget.set_show_glasses(True)
        shell.show()
        shell.resize(*size)
        window.apply_default_layout()
        window.plots_stack.setCurrentWidget(widget)
        for page, area, plot in ((widget.spot_concordia_page, widget.spot_sample_chips,
                                  widget.spot_concordia),
                                 (widget.depth_concordia_page, widget.depth_sample_chips,
                                  widget.depth_concordia)):
            widget.tabs.setCurrentWidget(page)
            for _ in range(8):
                app.processEvents()
            assert (shell.width(), shell.height()) == size
            assert len(page.findChildren(type(area))) == 1
            assert area.parentWidget() is page.widget()
            assert page.widget().layout().itemAt(0).widget() is area
            assert area.width() == page.widget().width()
            assert len(area.chips) == 9
            rows = {chip.y() for chip in area.chips.values()}
            assert 1 <= len(rows) <= 2
            assert all(22 <= chip.height() <= 24 for chip in area.chips.values()), [
                (chip.text(), chip.size(), chip.sizeHint(), chip.font().toString())
                for chip in area.chips.values()]
            assert area.height() == area.flow.heightForWidth(area.viewport().width())
            assert area.verticalScrollBar().maximum() == 0
            assert page.horizontalScrollBar().maximum() == 0
            assert plot.height() >= 240
            if page is widget.spot_concordia_page:
                assert widget.spot_filter.parentWidget() is widget._chooser_stack.parentWidget()
                assert widget.spot_filter.parentWidget() is not area.parentWidget()
    finally:
        shell.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_explicit_run_order_overrides_grouped_frame_order(review):
    # Reduction frames arrive grouped alphabetically by sample; the main window
    # passes the acquisition order from the session's start times.
    review.show_results(results(("91500", "91500", "NIST610", "S513", "S513", "Seiland")))
    review.set_sample_run_order(["NIST610", "S513", "Seiland", "91500"])
    expected = ["NIST610", "S513", "Seiland", "91500"]
    assert review.sample_chip_names() == expected
    assert list(review.spot_sample_chips.chips) == expected
    review.set_sample_run_order(["Seiland"])
    assert review.sample_chip_names() == ["Seiland", "91500", "NIST610", "S513"]
