"""Final results: key spot-mean columns, Jump-to chips, frozen spot columns."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI.theme import THEMES, build_application
from GUI.widgets import (
    RESULTS_ADDED_COLUMNS,
    RESULTS_KEY_GROUPS,
    ResultsTableWidget,
    results_display_frame,
)

# Column sets of the 11 Nov zircon and 10 Nov apatite sessions (both presets
# produce the same final-results columns).
SELECTED = ["sample", "spot_name", "preferred_date", "preferred_date_2e", "preferred_date_method",
            "preferred_date_pass", "automatic_flags", "206Pb/238U", "206Pb/238U_2e_no_calibration",
            "206Pb/238U_2e_with_calibration", "207Pb/206Pb", "207Pb/206Pb_2e_no_calibration",
            "207Pb/206Pb_2e_with_calibration", "err_corr_no_calibration", "err_corr_with_calibration",
            "tw_rho_no_calibration", "tw_rho_with_calibration", "208Pb/232Th",
            "208Pb/232Th_2e_no_calibration", "208Pb/232Th_2e_with_calibration"]
SPOT_MEAN_EXTRA = list(dict.fromkeys(RESULTS_ADDED_COLUMNS.values())) + ["P", "Ti", "qc_sweep_count"]


def finalized(n=60, drop=(), seed=4):
    rng = np.random.default_rng(seed)
    names = [f"S{i % 3}_{i:03d}" for i in range(n)]
    selected = pd.DataFrame({column: rng.random(n) for column in SELECTED})
    selected["sample"] = [name.split("_")[0] for name in names]
    selected["spot_name"] = names
    selected["preferred_date_method"] = np.where(selected.preferred_date > 0.5, "207Pb/206Pb", "206Pb/238U")
    selected["automatic_flags"] = ""
    spots = pd.DataFrame({column: rng.random(n) for column in SPOT_MEAN_EXTRA if column not in drop})
    spots.insert(0, "spot_name", names[::-1])  # a different row order than the final results
    spots.insert(1, "sample", selected["sample"][::-1].to_numpy())
    return SimpleNamespace(selected_results=selected, parallel_results=spots)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.fixture
def review(app):
    widget = ResultsTableWidget(settings=QtCore.QSettings("UPbRPy", "StudioTest"))
    widget.apply_theme(THEMES["light"])
    widget.resize(1200, 600)
    widget.show()
    yield widget
    widget._settings.remove("review_key_columns_only_v1")
    widget.close()
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def test_display_frame_keeps_results_and_adds_spot_mean_columns():
    result = finalized()
    selected, spots = result.selected_results.copy(), result.parallel_results.copy()
    display = results_display_frame(result.selected_results, result.parallel_results)
    # The final-results columns are all there, in their order, with their values.
    assert [c for c in display.columns if c in SELECTED] == SELECTED
    pd.testing.assert_frame_equal(display[SELECTED], selected)
    # Each added column equals its spot-mean source for the same spot.
    source = spots.set_index("spot_name")
    for display_name, source_name in RESULTS_ADDED_COLUMNS.items():
        expected = source[source_name].reindex(display.spot_name).to_numpy()
        np.testing.assert_array_equal(display[display_name].to_numpy(), expected)
    # Dates follow the preferred date; concentrations follow the flags.
    columns = list(display.columns)
    assert columns.index("206Pb/238U_age") == columns.index("preferred_date_pass") + 1
    assert columns.index("U (µg/g)") == columns.index("automatic_flags") + 1
    assert "204Pb mean (cps)" in columns and "204Pb common fraction (f206)" in columns
    assert "P" not in columns  # only the key spot-mean columns are added
    # Inputs, which the exports are built from, are untouched.
    pd.testing.assert_frame_equal(result.selected_results, selected)
    pd.testing.assert_frame_equal(result.parallel_results, spots)


@pytest.mark.parametrize("preset", ["zircon", "apatite"])
def test_every_chip_scrolls_to_an_existing_column(review, app, preset):
    review.show_results(finalized(seed=1 if preset == "zircon" else 2))
    review.tabs.setCurrentWidget(review.results_page)
    app.processEvents()
    table, frame = review.results_table, review.results_model.frame
    bar = review.results_jump_bar
    labels = [label for label, _c, _s in RESULTS_KEY_GROUPS]
    assert set(bar.visible_chips()) | set(bar.overflow()) == set(labels)
    assert not (set(bar.visible_chips()) & set(bar.overflow()))
    header = table.horizontalHeader()
    for label, columns, _strength in RESULTS_KEY_GROUPS:
        assert review.jump_to_column_group(label)
        target = next(c for c in columns if c in frame)
        column = frame.columns.get_loc(target) + 1
        assert not table.isColumnHidden(column)
        left = header.sectionViewportPosition(column)
        assert review.results_frozen.frozen_width() - 1 <= left <= table.viewport().width()
        assert review.results_model.flash_columns >= {target}
    # The spot columns stay in view while the table is scrolled.
    assert review.results_frozen.isVisible()
    assert [review.results_model.headerData(c, QtCore.Qt.Orientation.Horizontal)
            for c in review.results_frozen.frozen_columns()] == ["Include", "sample", "spot_name"]


def test_missing_columns_hide_their_chips(review, app):
    review.show_results(finalized(drop=("207Pb_corrected_age", "207Pb_corrected_age_2e", "208Pb_corrected_age",
                                        "208Pb_corrected_age_2e", "U", "qc_204Pb_mean",
                                        "204Pb_common_fraction_206", "207Pb_common_fraction_206")))
    app.processEvents()
    bar = review.results_jump_bar
    shown = set(bar.visible_chips()) | set(bar.overflow())
    assert not shown & {"207-corrected age", "208-corrected age", "U (µg/g)", "204Pb"}
    assert {"Preferred age", "206/238 date", "Th/U", "Ratios", "Flags"} <= shown
    assert not review.jump_to_column_group("208-corrected age")


def test_key_columns_only_round_trips_and_is_remembered(review, app):
    review.show_results(finalized())
    frame = review.results_model.frame
    table = review.results_table
    hidden = lambda name: table.isColumnHidden(frame.columns.get_loc(name) + 1)
    review.set_key_columns_only(True)
    assert hidden("err_corr_no_calibration")
    assert not hidden("preferred_date") and not hidden("U (µg/g)") and not hidden("spot_name")
    assert review._settings.value("review_key_columns_only_v1", type=bool) is True
    # A new widget with the same settings opens in the same view.
    again = ResultsTableWidget(settings=review._settings)
    try:
        again.show_results(finalized())
        assert again.key_columns_only()
        assert again.results_table.isColumnHidden(
            again.results_model.frame.columns.get_loc("err_corr_no_calibration") + 1)
    finally:
        again.deleteLater()
    review.set_key_columns_only(False)
    assert not any(table.isColumnHidden(c) for c in range(1, review.results_model.columnCount()))


def test_key_columns_are_marked_and_the_preferred_header_explains_the_method(review):
    review.show_results(finalized())
    model, frame = review.results_model, review.results_model.frame
    column = lambda name: frame.columns.get_loc(name) + 1
    background = lambda name: model.data(model.index(0, column(name)), QtCore.Qt.ItemDataRole.BackgroundRole)
    assert background("preferred_date") is not None and background("206Pb/238U_age") is not None
    assert background("preferred_date").color().alpha() > background("206Pb/238U_age").color().alpha()
    assert background("err_corr_no_calibration") is None
    tip = model.headerData(column("preferred_date"), QtCore.Qt.Orientation.Horizontal,
                           QtCore.Qt.ItemDataRole.ToolTipRole)
    assert "varies by row" in tip and "preferred_date_method" in tip
