"""Spawned stages preserve exact products, identity, errors and window lifetime."""

from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
from pathlib import Path
import threading
import time

import pandas as pd
import pytest

from GUI import services, stage_process
from GUI.workflows import get_workflow_preset
from UPbRPy.export_table import ExportSettings
from UPbRPy.notices import ReductionError
from UPbRPy.pipeline import PipelineStage
from scripts.snapshot_restore import _convert, compare_snapshots


RICH = Path(__file__).parent / "fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"
PRESET = get_workflow_preset("zircon")


@pytest.fixture
def session():
    session = services.load_session(str(RICH), preset=PRESET)
    # The fixture omits 202Hg. Carry a synthetic background in the request,
    # as in the existing restore identity tests; never edit generated CSVs.
    session.total_data = session.total_data.assign(**{"202Hg": 1.0})
    session.channels = [*session.channels, "202Hg"]
    session.pipeline = None
    session.session_fit_method = "mean"
    session.primary_68_standard = session.primary_76_standard = session.primary_82_standard = "S513"
    return session


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setenv("UPBRPY_STAGE_PROCESS", "1")
    pool = stage_process.StagePool()
    monkeypatch.setattr(stage_process, "stage_pool", pool)
    monkeypatch.setattr(stage_process, "_warned", False)
    yield pool
    pool.shutdown()


def prepare(session, stage, stages=(), settings=None):
    task = stage_process.StageTask()
    task.prepare(session, PRESET.key, settings or ExportSettings(), stage, stages)
    assert task._packet is not None
    return task


def no_fallback():
    pytest.fail("Unexpected in-thread fallback")


def assert_exact(left, right):
    # The snapshot comparator uses pandas.testing with check_exact=True,
    # including attrs, and compares all array/model parameters and notices.
    assert compare_snapshots(_convert(left), _convert(right)) == []


@pytest.mark.parametrize("stage", stage_process.STAGES)
def test_every_stage_exact_and_parent_identity(session, pool, stage):
    stages = tuple(PipelineStage)
    settings = ExportSettings(a_tio2=.75)
    # Overrides exercise the interpreter-local archive memo as well.
    session.reference_overrides = {"S513": {"29Si": 310000.0}}
    task = prepare(session, stage, stages, settings)
    inputs = (session.total_data, session.experiment, session.qtegra, session.channels)
    archive = services.current_standard_archive()
    expected = stage_process.run_stage(session, PRESET.key, settings, stage, stages)
    expected_cache = session.reduction_cache
    expected_notices = session.pipeline.notices
    expected_pipeline_cache = session.pipeline.cache
    with ThreadPoolExecutor(1) as executor:
        reply = executor.submit(task.run, no_fallback).result(timeout=40)
    assert isinstance(reply, stage_process.StageReply)
    actual = reply.merge(session, PRESET)
    assert_exact(expected, actual)
    assert_exact(expected_cache, session.reduction_cache)
    assert_exact(expected_pipeline_cache, session.pipeline.cache)
    assert session.pipeline.notices == expected_notices
    assert all(a is b for a, b in zip(inputs, (
        session.total_data, session.experiment, session.qtegra, session.channels)))
    assert session.pipeline._source_experiment is session.experiment
    assert session.pipeline.base_standard_archive is archive
    stages_after = session.pipeline.cache.stages()
    services.ensure_shared_pipeline(session, PRESET)
    assert session.pipeline.cache.stages() == stages_after
    if stage in {"final_review", "selection"}:
        finalized = session.reduction_cache["finalized_results"]
        assert session.reduction_cache["results_pb204_source"] is finalized
        assert session.pipeline.cache.get(PipelineStage.FINALIZE) is finalized
    assert len(pool._idle) == 1


def test_warm_worker_reused_and_qt_free(session, pool):
    pool.warm()
    deadline = time.monotonic() + 20
    while not pool._idle and time.monotonic() < deadline:
        time.sleep(.01)
    assert len(pool._idle) == 1
    worker = pool._idle[0]
    assert worker.qt_loaded is False
    for stage in ("baseline", "normalization", "concentration"):
        prepare(session, stage).run(no_fallback).merge(session, PRESET)
        assert pool._idle == [worker]


def test_child_uses_parent_reference_library(session, pool, monkeypatch, tmp_path):
    archive = services.current_standard_archive().copy(deep=True)
    archive.loc["NIST612", "29Si"] *= 1.01
    monkeypatch.setattr(services, "_active_standard_archive", archive)
    monkeypatch.setattr(services, "_active_standard_archive_path", str(tmp_path / "references"))
    task = prepare(session, "normalization")
    expected = services.ensure_normalized_plateau(session, PRESET)
    reply = task.run(no_fallback)
    pd.testing.assert_frame_equal(reply.result, expected, check_exact=True)
    reply.merge(session, PRESET)
    assert session.pipeline.base_standard_archive is archive
    assert session.pipeline.reference_library_path == tmp_path / "references"


def test_reduction_error_keeps_repairs_and_worker(session, pool):
    session.primary_68_standard = "not_measured"
    with pytest.raises(ReductionError) as direct:
        stage_process.run_stage(session, PRESET.key, ExportSettings(), "downhole")
    with pytest.raises(ReductionError) as remote:
        prepare(session, "downhole").run(no_fallback)
    assert remote.value.stage == direct.value.stage
    assert remote.value.fixes == direct.value.fixes
    assert remote.value.next_step == direct.value.next_step
    assert str(remote.value) == str(direct.value)
    assert "Traceback" in remote.value.__notes__[0]
    assert len(pool._idle) == 1


def test_killed_child_falls_back_once(session, pool, caplog):
    prepare(session, "baseline").run(no_fallback)
    worker = pool._idle[0]
    worker.process.kill()
    worker.process.join(timeout=5)
    calls = []

    def fallback():
        calls.append(threading.get_ident())
        return services.ensure_baseline_subtracted(session)

    result = prepare(session, "baseline").run(fallback)
    assert calls == [threading.get_ident()]
    pd.testing.assert_frame_equal(result, services.ensure_baseline_subtracted(session), check_exact=True)
    assert caplog.text.count("Process stages unavailable") == 1
    assert not pool._workers


def test_cancel_while_claiming_idle_child_reaps_it(session, pool, monkeypatch):
    prepare(session, "baseline").run(no_fallback)
    process = pool._idle[0].process
    task = prepare(session, "normalization")
    attach = task.attach

    def cancel_then_attach(worker):
        task.cancel()
        attach(worker)

    monkeypatch.setattr(task, "attach", cancel_then_attach)
    with pytest.raises(stage_process.StageCancelled):
        task.run(no_fallback)
    assert not process.is_alive()
    assert not pool._workers


@pytest.mark.parametrize("failure", ["start", "pickle"])
def test_infrastructure_failure_falls_back(session, pool, monkeypatch, caplog, failure):
    if failure == "start":
        def fail(*args):
            raise OSError("forced start failure")
        monkeypatch.setattr(multiprocessing.get_context("spawn").Process, "start", fail)
    else:
        session.import_settings["unpicklable"] = lambda: None
    task = stage_process.StageTask()
    task.prepare(session, PRESET.key, ExportSettings(), "baseline")
    marker = object()
    assert task.run(lambda: marker) is marker
    assert caplog.text.count("Process stages unavailable") == 1


@pytest.mark.parametrize("change", ["settings", "selection", "archive"])
def test_live_changes_reconciled(session, pool, monkeypatch, change):
    task = prepare(session, "final_review")
    if change == "settings":
        session.ratio_aggregation = "geometric"
    elif change == "selection":
        session.experiment.loc[0, "baseline_start_times"] += .1
    else:
        monkeypatch.setattr(services, "_active_standard_archive", services.current_standard_archive().copy())
    reply = task.run(no_fallback)
    assert reply.pipeline.cache.has(PipelineStage.FINALIZE)
    reply.merge(session, PRESET)
    assert not session.pipeline.cache.has(PipelineStage.FINALIZE)
    assert "finalized_results" not in session.reduction_cache
    assert session.pipeline.matches_source_experiment(session.experiment)
    assert session.pipeline.base_standard_archive is services.current_standard_archive()


def test_snapshot_buffers_do_not_read_later_gui_edits(session, pool):
    task = prepare(session, "baseline")
    expected = services.ensure_baseline_subtracted(session).copy(deep=True)
    session.total_data.loc[:, "238U"] = -1234.0
    reply = task.run(no_fallback)
    pd.testing.assert_frame_equal(reply.result, expected, check_exact=True)


def test_returned_frames_remain_writable(session, pool):
    reply = prepare(session, "baseline").run(no_fallback)
    reply.result.loc[0, "238U"] = -1234.0
    assert reply.result.loc[0, "238U"] == -1234.0


def test_two_concurrent_windows_share_small_pool(session, pool):
    tasks = [prepare(session, "concentration") for _ in range(3)]
    with ThreadPoolExecutor(3) as executor:
        replies = [executor.submit(task.run, no_fallback) for task in tasks]
        for reply in replies:
            assert isinstance(reply.result(timeout=40), stage_process.StageReply)
    assert len(pool._workers) == len(pool._idle) == 1


def wait_for(app, condition):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(.005)
    pytest.fail("Timed out waiting for Qt worker")


@pytest.fixture
def window(session, pool, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from GUI import main_window
    from GUI.theme import build_application

    app = QtWidgets.QApplication.instance() or build_application([], theme_mode="light")
    app.setQuitOnLastWindowClosed(False)
    monkeypatch.setattr(main_window, "stage_pool", pool)
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    window = main_window.MainWindow(theme_mode="light")
    window.session = session
    yield app, window
    window.has_unsaved_changes = lambda: False
    window.close()
    wait_for(app, lambda: window._active_worker is None)


def test_window_merges_before_success_and_completes_guard(window):
    app, window = window
    seen = []
    window.run_background("Subtracting baselines", stage_process.StageCall("baseline", no_fallback),
                          lambda result: seen.append(result is window.session.reduction_cache[
                              "baseline_subtracted_total_data"]))
    wait_for(app, lambda: window._active_worker is None)
    assert seen == [True]
    assert window.step_actions_block.isEnabled()
    assert window._busy_label == ""


def test_child_reduction_error_reaches_issue_banner(window, monkeypatch):
    app, window = window
    window.session.primary_68_standard = "not_measured"
    errors = []
    monkeypatch.setattr(window, "_show_reduction_issue", lambda label, error, details: errors.append(error))
    window.run_background("Fitting downhole fractionation", stage_process.StageCall("downhole", no_fallback),
                          lambda result: pytest.fail("Unexpected success"))
    wait_for(app, lambda: window._active_worker is None)
    assert len(errors) == 1 and isinstance(errors[0], ReductionError)
    assert errors[0].stage == "downhole" and errors[0].fixes


def test_close_probe_keeps_work_usable_if_another_tab_cancels(window):
    app, window = window
    seen = []
    window.has_unsaved_changes = lambda: False
    window.run_background("Subtracting baselines", stage_process.StageCall("baseline", no_fallback),
                          lambda result: seen.append(result))
    assert window.request_close()
    assert not window._stage_task.cancelled
    assert not window._stage_closing
    wait_for(app, lambda: window._active_worker is None)
    assert len(seen) == 1


def test_close_terminates_in_flight_child(window, pool):
    app, window = window
    window.run_background("Finalizing reduction", stage_process.StageCall("final_review", no_fallback),
                          lambda result: pytest.fail("Closed window received results"))
    wait_for(app, lambda: window._stage_task._worker is not None)
    worker = window._stage_task._worker
    window.has_unsaved_changes = lambda: False
    window.close()
    wait_for(app, lambda: window._active_worker is None)
    assert not worker.process.is_alive()
    assert worker not in pool._workers
    assert not pool._workers


def test_last_window_close_reaps_idle_child(window, pool):
    app, window = window
    window.run_background("Subtracting baselines", stage_process.StageCall("baseline", no_fallback),
                          lambda result: None)
    wait_for(app, lambda: window._active_worker is None)
    process = pool._idle[0].process
    window.has_unsaved_changes = lambda: False
    window.close()
    wait_for(app, lambda: not process.is_alive())
    assert not pool._workers


def test_detector_preview_async_cached_and_discards_stale(window):
    app, window = window
    panel = window.detector_calibration_panel
    panel.set_session(window.session)
    assert panel._preview_pending
    assert "Computing" in panel.reason_label.text()
    assert not panel.apply_button.isEnabled()
    # Replace the basis while the first request runs; only this key may win.
    panel.basis_combo.setCurrentIndex(1)
    wait_for(app, lambda: not panel._preview_pending)
    assert panel._constant.on == "primary_standard"
    assert panel._constant.channels
    assert panel.apply_button.isEnabled()
    constant = panel._constant
    panel._refresh_preview()
    assert panel._constant is constant
    expected = stage_process.calibration_preview(window.session.qtegra, {
        "on": "primary_standard", "primary_standard": "S513", "window": (1e5, 2.4e6),
        "smoothing_s": 1800., "sep": "_"})
    assert_exact((panel._constant, panel._drift), expected)
    channel = panel.channel_combo.currentText()
    window.session.import_settings["detector_calibration"] = {"on": "primary_standard"}
    panel.set_session(window.session)
    assert not panel._preview_pending
    assert panel._constant is constant
    assert panel.channel_combo.currentText() == channel


def test_shutdown_terminates_workers(session, pool):
    prepare(session, "baseline").run(no_fallback)
    process = pool._idle[0].process
    pool.shutdown()
    assert not process.is_alive()
    assert not pool._workers


def test_snapshot_arena_reused_only_after_send(session, pool, monkeypatch):
    first = prepare(session, "baseline")
    second = prepare(session, "baseline")
    assert first._arena is not second._arena
    arena = first._arena
    original_send = stage_process._send
    concurrent = []

    def send(connection, packet):
        # A request prepared while transport holds the buffers must not share
        # them, even when another warm child could be available.
        other = prepare(session, "baseline")
        assert other._arena is not arena
        concurrent.append(other)
        return original_send(connection, packet)

    monkeypatch.setattr(stage_process, "_send", send)
    first.run(no_fallback)
    assert first._packet is None and first._arena is None
    third = prepare(session, "baseline")
    assert third._arena is arena
    # Reuse does not overwrite a concurrently prepared snapshot.
    expected = stage_process._load(second._packet)[1][0].total_data.copy()
    session.total_data.loc[:, "238U"] = -999.
    pd.testing.assert_frame_equal(stage_process._load(second._packet)[1][0].total_data,
                                  expected, check_exact=True)


def test_cancelled_unsent_snapshot_returns_arena(session, pool):
    task = prepare(session, "baseline")
    arena = task._arena
    task.cancel()
    with pytest.raises(stage_process.StageCancelled):
        task.run(no_fallback)
    assert task._packet is None
    assert prepare(session, "baseline")._arena is arena


def test_snapshot_arena_preserves_empty_and_readonly_buffers():
    import numpy as np
    empty = np.array([])
    readonly = np.arange(4.)
    readonly.flags.writeable = False
    arena = stage_process._SnapshotArena()
    packet = stage_process._dump((empty, readonly), copy_buffers=True, arena=arena)
    actual_empty, actual_readonly = stage_process._load(packet)
    assert actual_empty.flags.writeable
    assert not actual_readonly.flags.writeable
    np.testing.assert_array_equal(actual_readonly, readonly)


def test_last_window_does_not_retain_unsent_snapshot(session, pool):
    token = pool.register_window()
    task = prepare(session, "baseline")
    pool.release_window(token)
    task.cancel()
    with pytest.raises(stage_process.StageCancelled):
        task.run(no_fallback)
    assert pool._arenas == []


def test_numpy_scalar_reducer_is_byte_identical():
    import pickle
    import numpy as np
    values = [np.float64(-0.), np.float64(np.nan), np.float32(1.5),
              np.int64(-7), np.uint64(2**63), np.bool_(True),
              np.complex128(1 + 2j), np.str_("zircon"), np.bytes_(b"sample"),
              np.datetime64("2026-11-18"), np.timedelta64(3, "s"),
              np.array((1, 2.), dtype=[("a", "i4"), ("b", "f8")])[()]]
    payload, buffers = stage_process._dump(values)
    assert not buffers
    assert payload == pickle.dumps(values, protocol=5)


def test_numpy_array_reducer_is_byte_identical():
    import pickle
    import numpy as np
    readonly = np.arange(8.)
    readonly.flags.writeable = False
    arrays = [np.arange(8.), readonly, np.array(2.), np.array([]),
              np.asfortranarray(np.arange(12.).reshape(3, 4)),
              np.arange(8.)[::2], np.array([np.float64(2.), "spot"], dtype=object),
              np.empty(3, dtype="V0"),
              np.array([(1, 2.)], dtype=[("a", "i4"), ("b", "f8")])]
    for array in arrays:
        original = []
        expected = pickle.dumps(array, protocol=5, buffer_callback=original.append)
        payload, buffers = stage_process._dump(array)
        assert payload == expected
        assert [buffer.raw().tobytes() for buffer in buffers] == [
            buffer.raw().tobytes() for buffer in original]


@pytest.mark.parametrize("stage", ["downhole", "session"])
def test_precomputed_diagnostic_uses_exact_view_and_result_objects(session, pool, stage):
    task = stage_process.StageTask()
    task.prepare(session, PRESET.key, ExportSettings(), stage, plot_ratio="206Pb/238U")
    reply = task.run(no_fallback)
    assert reply._plot_data is not None
    data = reply._plot_data[-1]
    reply.merge(session, PRESET)
    getter = (services.get_downhole_plot_data if stage == "downhole"
              else services.get_session_plot_data)
    assert getter(session, PRESET, "206Pb/238U") is data
    # A view changed while transport ran may not consume the prepared ratio.
    assert getter(session, PRESET, "207Pb/206Pb") is not data
    # Compare the child product with the synchronous fallback, exactly.
    cache = services._downhole_plot_cache if stage == "downhole" else services._session_plot_cache
    cache.pop(id(session), None)
    assert_exact(getter(session, PRESET, "206Pb/238U"), data)


@pytest.mark.parametrize("stage", ["downhole", "session"])
def test_precomputed_diagnostic_rejects_edits_during_request(session, pool, stage):
    task = stage_process.StageTask()
    task.prepare(session, PRESET.key, ExportSettings(), stage, plot_ratio="206Pb/238U")
    session.experiment.loc[0, "baseline_start_times"] += .1
    reply = task.run(no_fallback)
    assert reply._plot_data is not None
    reply.merge(session, PRESET)
    cache = services._downhole_plot_cache if stage == "downhole" else services._session_plot_cache
    assert id(session) not in cache
