"""Studio displays cached export mass-204 quantities without changing exports."""

from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import replace
from threading import current_thread
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI import main_window, services
from GUI.state import ReductionSession
from GUI.theme import build_application
from GUI.widgets import ResultsTableWidget, results_column_group
from UPbRPy.export_table import ExportSettings, build_export_table, build_footnotes
from UPbRPy.pipeline import PipelineStage
from test_export_table import synthetic_pipeline


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.fixture
def review(app):
    widget = ResultsTableWidget()
    widget.resize(1200, 600)
    widget.show()
    yield widget
    widget.close()
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def session_for(pipeline, monkeypatch):
    session = ReductionSession(pipeline=pipeline)
    session.reduction_cache["finalized_results"] = pipeline.run_finalize()
    monkeypatch.setattr(services, "ensure_shared_pipeline", lambda *_: pipeline)
    return session


@pytest.mark.parametrize("case", ["reported", "omitted", "no202", "no204", "uncalibrated"])
def test_export_values_status_and_unchanged_export(review, app, monkeypatch, case):
    pipeline = synthetic_pipeline()  # Includes NIST612, 202Hg and 204Hg.
    final = pipeline.run_finalize()
    if case == "omitted":
        final.parallel_results.loc[2, "206Pb/204Pb"] = np.nan
    elif case == "no202":
        pipeline.channels = tuple(c for c in pipeline.channels if c != "202Hg")
        del pipeline.run_normalization()["202Hg"]
    elif case == "no204":
        pipeline.channels = tuple(c for c in pipeline.channels if c != "204Hg")
    elif case == "uncalibrated":
        pipeline.config = replace(pipeline.config, concentration_standards=())
    # A legacy selected ratio must not override the export's Hg-corrected one.
    final.selected_results["206Pb/204Pb"] = -123.
    selected = final.selected_results.copy(deep=True)
    parallel = final.parallel_results.copy(deep=True)
    session = session_for(pipeline, monkeypatch)
    settings = ExportSettings(a_tio2=.8, optional_columns=("204Pb",))
    before = build_export_table(pipeline, settings)
    footnotes_before = build_footnotes(before, pipeline)
    pb204 = services.ensure_results_pb204(session, None, settings)
    # Rendering, repeated spot selection and chip use cannot build an export.
    monkeypatch.setattr(services, "build_export_table", lambda *_: pytest.fail("Recomputed on click"))
    for _ in range(2):
        review.show_results(final, pb204_frame=pb204)
        review.set_selected_spots(["A_2"])
        assert review.jump_to_column_group("204Pb")
    app.processEvents()
    display = review.results_model.frame.set_index("spot_name")
    exported = before.rows.set_index("spot_name").reindex(display.index)
    reported = display["204Pb status"].eq("reported")
    np.testing.assert_array_equal(display.loc[reported, "204Pb (µg/g)"], exported.loc[reported, "204Pb"])
    assert display.loc[~reported, "204Pb (µg/g)"].isna().all()
    if case in {"reported", "omitted"}:
        omitted = display.index.isin(before.context["total_pb"]["omitted_204_spots"])
        assert display.loc[omitted, "204Pb status"].eq("not detected").all()
        assert display.loc[~omitted, "204Pb status"].eq("reported").all()
        assert reported.any()
        if case == "omitted":
            assert display.loc["A_2", "204Pb status"] == "not detected"
            assert exported.loc["A_2", "204Pb"] == 0
    else:
        expected = {"no202": "unavailable: no 202Hg channel", "no204": "not measured",
                    "uncalibrated": "not calibrated"}[case]
        assert display["204Pb status"].eq(expected).all()
    for label, key in (("206Pb/204Pb", "206Pb/204Pb"), ("206Pb/204Pb ±1s", "206Pb/204Pb_1s")):
        np.testing.assert_array_equal(display[label], exported[key])
        assert results_column_group(label) == "Concentrations"
    assert results_column_group("204Pb (µg/g)") == "Concentrations"
    assert results_column_group("204Pb status") == "Concentrations"
    columns = list(review.results_model.frame.columns)
    assert columns.index("206Pb/204Pb") == columns.index("204Pb status") + 1
    assert columns.index("204Pb (µg/g)") + 1 == columns.index("204Pb status")
    # The chip's first target is on screen, including in Key columns only.
    review.set_key_columns_only(True)
    assert review.jump_to_column_group("204Pb")
    column = columns.index("204Pb (µg/g)") + 1
    assert not review.results_table.isColumnHidden(column)
    left = review.results_table.horizontalHeader().sectionViewportPosition(column)
    assert review.results_frozen.frozen_width() - 1 <= left <= review.results_table.viewport().width()
    after = build_export_table(pipeline, settings)
    for attribute in ("rows", "sample_summaries", "secondary_summary"):
        pd.testing.assert_frame_equal(getattr(before, attribute), getattr(after, attribute), check_exact=True)
    assert before.columns == after.columns and before.settings == after.settings
    assert footnotes_before == build_footnotes(after, pipeline)
    pd.testing.assert_frame_equal(final.selected_results, selected)
    pd.testing.assert_frame_equal(final.parallel_results, parallel)


def test_cache_rebuilds_only_for_new_finalized_object(monkeypatch):
    pipeline = synthetic_pipeline()
    session = session_for(pipeline, monkeypatch)
    settings = ExportSettings()
    calls = []

    def build(pipeline, received_settings):
        calls.append(received_settings)
        return build_export_table(pipeline, received_settings)

    monkeypatch.setattr(services, "build_export_table", build)
    first = services.ensure_results_pb204(session, None, settings)
    entries = tuple(session.reduction_cache.values())
    assert services.ensure_results_pb204(session, None, replace(settings, a_tio2=.9)) is first
    assert all(a is b for a, b in zip(entries, session.reduction_cache.values()))
    final = copy(pipeline.run_finalize())
    pipeline.cache.set(PipelineStage.FINALIZE, final)
    session.reduction_cache["finalized_results"] = final
    second = services.ensure_results_pb204(session, None, settings)
    assert second is not first
    assert session.reduction_cache["results_pb204_source"] is final
    assert calls == [settings, settings]


def test_final_review_prepares_export_in_worker(monkeypatch):
    pipeline = synthetic_pipeline()
    session = session_for(pipeline, monkeypatch)
    final = pipeline.run_finalize()
    monkeypatch.setattr(main_window, "ensure_spot_means", lambda *_: final.parallel_results)
    monkeypatch.setattr(main_window, "ensure_finalized_results", lambda *_: final)
    work = []
    settings = ExportSettings(a_tio2=.75)
    window = SimpleNamespace(session=session, workflow_preset=None, export_settings=settings,
        _require_loaded=lambda: True, run_background=lambda title, calculate, show: work.append(calculate))
    gui_thread = current_thread()
    threads = []

    def build(pipeline, received_settings):
        assert received_settings is settings
        threads.append(current_thread())
        return build_export_table(pipeline, received_settings)

    monkeypatch.setattr(services, "build_export_table", build)
    main_window.MainWindow.run_final_review(window)
    assert not threads and "results_pb204" not in session.reduction_cache
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(work[0]).result()[1] is final
    assert len(threads) == 1 and threads[0] is not gui_thread
    assert "results_pb204" in session.reduction_cache


def test_recalculated_final_stages_prepare_display_once(monkeypatch):
    pipeline = synthetic_pipeline()
    session = session_for(pipeline, monkeypatch)
    monkeypatch.setattr(services, "ensure_finalized_results", lambda *_: pipeline.run_finalize())
    settings = ExportSettings(a_tio2=.85)
    calls = []

    def build(pipeline, received_settings):
        calls.append(received_settings)
        return build_export_table(pipeline, received_settings)

    monkeypatch.setattr(services, "build_export_table", build)
    services.populate_recalculated_cache(session, None,
        (PipelineStage.FINALIZE, PipelineStage.EXPORT), export_settings=settings)
    assert calls == [settings]
    assert session.reduction_cache["results_pb204_source"] is pipeline.run_finalize()


def test_failed_export_build_leaves_results_usable(monkeypatch):
    pipeline = synthetic_pipeline()
    session = session_for(pipeline, monkeypatch)

    def fail(*_args):
        raise KeyError("202Hg")

    monkeypatch.setattr(services, "build_export_table", fail)
    frame = services.ensure_results_pb204(session, None, ExportSettings())
    assert frame["204Pb"].isna().all()
    assert frame["204Pb_status"].str.startswith("unavailable:").all()
    assert set(frame.spot_name) == set(pipeline.run_finalize().selected_results.spot_name)
