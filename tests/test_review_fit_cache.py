"""Scope caching, bootstrap chunk reproducibility and GUI update throttling."""
import threading

import numpy as np
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore

from GUI import review_fits as module
from UPbRPy.regression import bootstrap_discordia, fit_discordia, isochron_fit_table, model_name
from benchmarks.fit_timing import mixtures
from test_review_calculation_fits import app, review, fit, wait_for


def test_unchanged_scope_keeps_fit_bootstrap_and_overlay(review, app, monkeypatch):
    calls = []
    original = module.fit_discordia
    def compute(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "fit_discordia", compute)
    panel = fit(app, review, resamples=1000)
    initial_calls = len(calls)
    initial_generation = panel._generation
    initial_records = panel.records
    overlays = [tuple(items) for _, items in panel._overlays]
    from GUI.theme import THEMES
    review.apply_theme(THEMES["dark"])
    review.set_spot_colour_mode("single")
    review.set_selected_spots(set(review.include_flags))
    for _ in range(3):
        panel.invalidate()
    assert panel.records is initial_records
    assert panel._generation == initial_generation
    assert not panel._timer.isActive()
    assert [tuple(items) for _, items in panel._overlays] == overlays
    assert len(calls) == initial_calls
    review.set_spot_excluded(["A_0"], True)
    assert panel._generation > initial_generation
    wait_for(app, lambda: panel.records and all(r.get("bootstrap_resamples") == 1000 for r in panel.records))
    assert len(calls) == initial_calls+1  # the other sample reuses its cache
    review.set_spot_excluded(["A_0"], False)
    wait_for(app, lambda: panel.records and all(r.get("bootstrap_resamples") == 1000 for r in panel.records))
    assert len(calls) == initial_calls+1


def test_changed_resamples_reuses_analytical_fit(review, app, monkeypatch):
    panel = fit(app, review, resamples=250)
    monkeypatch.setattr(module, "fit_discordia", lambda *a, **kw: pytest.fail("analytical cache missed"))
    panel.resamples.setValue(1000)
    wait_for(app, lambda: panel.records and all(r.get("bootstrap_resamples") == 1000 for r in panel.records))
    assert all(r["bootstrap_chunk_size"] == 500 for r in panel.records)


def scope():
    return dict(label="test", points=mixtures(30), kwargs={}, depth=False,
                n_valid=30, n_total=30, excluded_count=0)


def test_hash_covers_arrays_groups_order_and_fit_options():
    original = scope()
    key = module._scope_key(original)
    copied = dict(original, points=tuple(a.copy() for a in original["points"]))
    assert module._scope_key(copied) == key
    for i in range(5):
        points = [a.copy() for a in original["points"]]
        points[i][0] += .001
        assert module._scope_key(dict(original, points=points)) != key
    for kwargs in (dict(model=2), dict(groups=np.array(["a"]*30, dtype=object)),
                   dict(order=np.arange(30)), dict(order=np.arange(30)[::-1])):
        assert module._scope_key(dict(original, kwargs=kwargs)) != key
    a = dict(original, kwargs=dict(groups=np.array(["a", "b"], dtype=object)))
    b = dict(original, kwargs=dict(groups=np.array(["a", "b"], dtype=object)))
    assert module._scope_key(a) == module._scope_key(b)


def test_cache_is_bounded_lru():
    cache = module._FitCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1
    cache.put("c", 3)
    assert cache.get("b") is None
    assert len(cache.entries) == 2


def test_worker_throttles_and_flushes_final_result(app, monkeypatch):
    clock = [0.]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    original = module.bootstrap_discordia
    calls, emitted = [], []
    def compute(*args, **kwargs):
        calls.append((kwargs["n_resamples"], kwargs["seed"]))
        clock[0] += .11
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "bootstrap_discordia", compute)
    worker = module._FitWorker(1, [scope()], dict(resamples=2000, seed=7), threading.Event())
    worker.signals.partial.connect(lambda result: emitted.append((clock[0], result)))
    worker.run()
    assert calls == [(500, 7), (500, 8), (500, 9), (500, 10)]
    assert all(t2-t1 >= .2 for (t1, _), (t2, _) in zip(emitted[:-2], emitted[1:-1]))
    boot = emitted[-1][1][1][0][2]
    assert boot.n_resamples == 2000
    assert "500 resamples" in boot.notes[-1]
    expected = [bootstrap_discordia(*scope()["points"], fit=emitted[-1][1][1][0][1],
                 n_resamples=500, seed=7+k, chunk=500) for k in range(4)]
    np.testing.assert_array_equal(boot.lower_ma, np.concatenate([c.lower_ma for c in expected]))


def test_model_names_tooltips_settings_and_old_chunk_record(review, app):
    panel = review.fit_panels["spot"]
    for model in (1, 2, 3):
        assert panel.model_combo.itemText(model-1) == model_name(model)
        tooltip = panel.model_combo.itemData(model-1, QtCore.Qt.ItemDataRole.ToolTipRole)
        assert f"model {model}" in tooltip and len(tooltip) > 100
    panel.restore_settings(dict(model=2))
    assert panel.model_combo.currentIndex() == 1
    assert panel.settings()["model"] == 2
    assert "orthogonal" in panel.model_combo.toolTip()
    panel = fit(app, review, resamples=250)
    panel._timer.stop()
    saved = dict(panel.records[0], bootstrap_chunk_size=125)
    panel.records = [saved]
    panel.show_records()
    assert panel.records[0] == saved
    assert "Equal weights, errors ignored (model 2)" in panel.readout.toPlainText()
    assert "Equal weights, errors ignored (model 2)" == isochron_fit_table([saved]).iloc[0]["Model"]


def test_restore_saved_record_cancels_matching_inflight_job(review, app, monkeypatch):
    panel = fit(app, review, resamples=1000)
    saved = dict(panel.records[0], bootstrap_chunk_size=125)
    entered, release = threading.Event(), threading.Event()
    original = module.bootstrap_discordia
    def compute(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "bootstrap_discordia", compute)
    panel.seed.setValue(panel.seed.value()+1)
    wait_for(app, lambda: entered.is_set())
    cancelled = panel._cancelled
    try:
        review.restore_review_state(dict(review_fit_settings=review.fit_settings(),
                                        review_fits=[saved]), review._spot_frame.spot_name)
        assert cancelled.is_set()
        assert not panel._timer.isActive()
        assert panel.records[0]["bootstrap_chunk_size"] == 125
    finally:
        release.set()
    wait_for(app, lambda: not panel._workers)
    assert panel.records[0]["bootstrap_chunk_size"] == 125


def test_saved_enabled_default_restores_before_canvas_exists(app, tmp_path):
    import json
    from GUI.widgets import ResultsTableWidget
    settings = QtCore.QSettings(str(tmp_path / "enabled.ini"), QtCore.QSettings.Format.IniFormat)
    settings.setValue("review_fit_spot_v1", json.dumps(dict(enabled=True, model=2)))
    widget = ResultsTableWidget(settings=settings)
    try:
        panel = widget.fit_panels["spot"]
        assert panel.fit_button.isChecked()
        assert panel.settings()["model"] == 2
        assert widget.spot_concordia is not None
    finally:
        for panel in widget.fit_panels.values():
            panel.fit_button.setChecked(False)
        widget.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("kind", ["spot", "depth"])
def test_selection_of_excluded_spot_only_updates_counts(review, app, monkeypatch, kind):
    review.set_spot_excluded(["A_0"], True)
    panel = fit(app, review, kind, resamples=250)
    before = dict(next(r for r in panel.records if r["label"] == "A"))
    generation = panel._generation
    overlays = [tuple(items) for _, items in panel._overlays]
    monkeypatch.setattr(module, "fit_discordia", lambda *a, **kw: pytest.fail("unchanged fit arrays"))
    review.set_selected_spots(set(review.include_flags)-{"A_0"})
    assert panel._generation == generation
    assert not panel._timer.isActive()
    assert [tuple(items) for _, items in panel._overlays] == overlays
    after = next(r for r in panel.records if r["label"] == "A")
    assert after["n_total"] == before["n_total"]-before["excluded_count"]
    assert after["excluded_count"] == 0 and after["excluded"] == []
    assert after["bootstrap_lower_ma_low"] == before["bootstrap_lower_ma_low"]


def test_appearance_keeps_restored_recorded_scope(review, app):
    from GUI.theme import THEMES
    selected = {f"A_{i}" for i in range(4)}
    review.set_selected_spots(selected)
    panel = fit(app, review, resamples=250)
    saved = dict(review_fit_settings=review.fit_settings(), review_fits=review.fit_records())
    review.set_selected_spots(set())
    review.restore_review_state(saved, review._spot_frame.spot_name)
    generation = panel._generation
    assert set(panel.records[0]["items"]) == selected
    review.apply_theme(THEMES["dark"])
    panel.invalidate()
    assert panel._generation == generation and not panel._timer.isActive()
    assert set(panel.records[0]["items"]) == selected and panel.records[0]["n_total"] == 4
