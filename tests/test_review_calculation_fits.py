"""Calculation decisions, acquisition selection and asynchronous review fits."""
from types import SimpleNamespace
import json
import threading
import time

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from GUI.theme import THEMES, build_application
from GUI.widgets import ResultsTableWidget, _SPOT_NAMES_ROLE
from UPbRPy.regression import fit_discordia, fit_record


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


def frame(n=12):
    x = np.linspace(2, 10, n)
    y = .85 - .07*x + .0002*np.sin(np.arange(n))
    f = pd.DataFrame(dict(spot_name=[f"{('A' if i < n//2 else 'B')}_{i}" for i in range(n)],
                          sample=["A"]*(n//2)+["B"]*(n-n//2)))
    f["238U/206Pb"], f["207Pb/206Pb"] = x, y
    f["206Pb/238U"], f["207Pb/235U"] = 1/x, y/x*137.818
    for ratio in ("238U/206Pb", "207Pb/206Pb", "206Pb/238U", "207Pb/235U"):
        f[ratio + "_2e_with_calibration"] = f[ratio]*.02
        f[ratio + "_2e_no_calibration"] = f[ratio]*.01
    f["err_corr"], f["tw_rho"] = .2, -.15
    return f


def depths(f):
    d = f.loc[f.index.repeat(4)].reset_index(drop=True)
    d["signal_relative_time"] = np.tile(np.arange(4), len(f))
    d = d.rename(columns={c: c.replace("_2e", "_fractionation_corrected_2e")
                          if "_2e" in c else c + "_fractionation_corrected"
                          for c in d if "/" in c})
    return d


@pytest.fixture
def review(app):
    w = ResultsTableWidget()
    w.apply_theme(THEMES["light"])
    f = frame()
    w.show_results(SimpleNamespace(parallel_results=f, selected_results=f.copy()))
    w.show_depth_profile(depths(f), "A_0")
    w.show_all()
    w.resize(1000, 700)
    w.tabs.setCurrentWidget(w.spot_concordia_page)
    w.show()
    app.processEvents()
    yield w
    for p in w.fit_panels.values():
        p.fit_button.setChecked(False)
    QtCore.QThreadPool.globalInstance().waitForDone(10000)
    w.close()
    w.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def wait_for(app, predicate, timeout=10):
    stop = time.monotonic()+timeout
    while time.monotonic() < stop:
        app.processEvents()
        if predicate():
            return
        QtTest.QTest.qWait(10)
    pytest.fail("Timed out waiting for review fit")


def fit(app, review, kind="spot", resamples=250):
    p = review.fit_panels[kind]
    p.resamples.setValue(resamples)
    p.fit_button.setChecked(True)
    wait_for(app, lambda: bool(p.records) and all(r.get("bootstrap_resamples") == resamples for r in p.records))
    return p


def children(view):
    return [index for index in ResultsTableWidget._tree_rows(view) if index.parent().isValid()]


def test_visibility_and_calculation_exclusions_are_independent(review, app):
    review.set_spot_included(["A_0"], False)
    assert review.excluded_spots == set()
    assert "A_0" not in {p[0] for p in review.spot_concordia._points}
    review.set_spot_excluded(["A_1"], True)
    assert review.include_flags["A_1"]
    assert "A_1" in {p[0] for p in review.spot_concordia._points}
    for canvas in (review.spot_concordia, review.depth_concordia):
        handle = next(e for name, e, _ in canvas._ellipses if name == "A_1")
        assert handle.isVisible()
        assert handle.opts["pen"].style() == QtCore.Qt.PenStyle.DashLine
        index = next(i for i, p in enumerate(canvas._points) if p[0] == "A_1")
        assert canvas._scatter.points()[index].brush().style() == QtCore.Qt.BrushStyle.NoBrush
        assert "Excluded from calculations" in canvas.tooltip_text(index)
    app.processEvents()
    assert review.spot_age_label.text().startswith("Included 10")
    item = review.spot_choice_list.findItems("A_1", QtCore.Qt.MatchFlag.MatchExactly)[0]
    assert item.font().italic() and "Excluded from calculations" in item.toolTip()
    row = next(i for i in range(review.results_model.rowCount()) if review.results_model.spot_name(i) == "A_1")
    assert review.results_model.index(row, 1).data(QtCore.Qt.ItemDataRole.FontRole).italic()
    p = fit(app, review)
    a = next(r for r in p.records if r["label"] == "A")
    assert a["n"] == 4 and a["n_total"] == 5 and a["excluded"] == ["A_1"]
    assert "A_0" not in a["items"] and "A_1" not in a["items"]
    review.set_spot_excluded(["A_1"], False)
    assert next(e for n, e, _ in review.depth_concordia._ellipses if n == "A_1").opts["pen"].style() == QtCore.Qt.PenStyle.SolidLine


@pytest.mark.parametrize("kind", ["spot", "depth"])
def test_tree_multiselection_mixed_state_and_actions(review, app, kind):
    view = review.spot_choice_tree if kind == "spot" else review.depth_choice_tree
    review.tabs.setCurrentWidget(review.spot_concordia_page if kind == "spot" else review.depth_concordia_page)
    review.spot_group_toggle.setChecked(True)
    app.processEvents()
    menu = (review.spot_spots_button if kind == "spot" else review.depth_spots_button).menu()
    menu.popup(review.mapToGlobal(QtCore.QPoint(20, 20)))
    for _ in range(8): app.processEvents()
    view.expandAll()
    app.processEvents()
    indices = children(view)
    QtTest.QTest.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton,
                           pos=view.visualRect(indices[0]).center())
    QtTest.QTest.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton,
                           QtCore.Qt.KeyboardModifier.ShiftModifier, view.visualRect(indices[2]).center())
    assert {indices[i].data(_SPOT_NAMES_ROLE)[0] for i in range(3)} <= review.selected_spots
    selected = set(review.selected_spots)
    QtTest.QTest.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton,
                           QtCore.Qt.KeyboardModifier.ControlModifier, view.visualRect(indices[1]).center())
    assert review.selected_spots == selected - set(indices[1].data(_SPOT_NAMES_ROLE))
    review.show_only_selected()
    assert {n for n, shown in review.include_flags.items() if shown} == review.selected_spots
    parent = view.model().index(0, 0)
    assert parent.data(QtCore.Qt.ItemDataRole.CheckStateRole) == QtCore.Qt.CheckState.PartiallyChecked.value
    model = review.depth_choice_group_model if kind == "depth" else review.spot_choice_group_model
    if kind == "depth":
        review._depth_sample_choices = False
    model.item(0).child(4).setCheckState(QtCore.Qt.CheckState.Checked)
    if kind == "depth":
        assert review._depth_sample_choices
    QtTest.QTest.keyClick(view, QtCore.Qt.Key.Key_A, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert review.selected_spots == set(review.include_flags)
    review.hide_selected()
    assert not any(review.include_flags.values())
    review.show_all()
    assert all(review.include_flags.values())


@pytest.mark.parametrize("kind", ["spot", "depth"])
def test_canvas_gestures_selection_shortcuts_and_batched_highlight(review, app, kind):
    canvas = getattr(review, kind + "_concordia")
    review.tabs.setCurrentWidget(getattr(review, kind + "_concordia_page"))
    app.processEvents()
    canvas.restore_default_view()
    app.processEvents()
    n, x, y, _ = canvas._points[0]
    scene = canvas._plot_item.vb.mapViewToScene(QtCore.QPointF(x, y))
    canvas._select_at(scene, QtCore.Qt.KeyboardModifier.NoModifier)
    assert review.selected_spots == {n}
    other = next(p for p in canvas._points if p[0] != n)
    scene2 = canvas._plot_item.vb.mapViewToScene(QtCore.QPointF(other[1], other[2]))
    canvas._select_at(scene2, QtCore.Qt.KeyboardModifier.ShiftModifier)
    assert review.selected_spots == {n, other[0]}
    canvas._select_at(scene2, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert review.selected_spots == {n}
    assert next(e for name, e, _ in canvas._ellipses if name == n).opts["pen"].widthF() > 1
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_E)
    assert review.excluded_spots == {n}
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_H)
    assert review.include_flags[n] and not review.excluded_spots
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_H, QtCore.Qt.KeyboardModifier.ShiftModifier)
    assert all(review.include_flags.values())
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_A, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert review.selected_spots == set(review.include_flags)
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_Escape)
    assert not review.selected_spots
    menu = review._action_menu(canvas, kind)
    assert any(a.text() == "Fit selected" for a in menu.actions())
    menu.deleteLater()


@pytest.mark.parametrize("kind", ["spot", "depth"])
@pytest.mark.parametrize("space", ["wetherill", "tera_wasserburg"])
def test_gui_matches_engine_points_and_groups_with_fixed_overlay_count(review, app, kind, space):
    getattr(review, kind + "_diagram_buttons")[space].setChecked(True)
    getattr(review, "refresh_" + kind + "_concordia")()
    review.set_spot_excluded(["A_0"], True)
    panel = fit(app, review, kind)
    source = review._spot_frame if kind == "spot" else review.depth_concordia._frame
    prefix = "" if kind == "spot" else "_fractionation_corrected"
    xcol, ycol = (("207Pb/235U", "206Pb/238U") if space == "wetherill" else ("238U/206Pb", "207Pb/206Pb"))
    xcol, ycol = xcol + prefix, ycol + prefix
    for r in panel.records:
        data = source.loc[source["sample"].eq(r["label"]) & ~source.spot_name.isin(review.excluded_spots)]
        options = dict(space=space)
        if kind == "depth":
            options.update(groups=data.spot_name, order=data.signal_relative_time)
        expected = fit_discordia(data[xcol], data[ycol], data[xcol + "_2e_with_calibration"]/2,
                                 data[ycol + "_2e_with_calibration"]/2,
                                 data["err_corr" if space == "wetherill" else "tw_rho"], **options)
        assert r["slope"] == pytest.approx(expected.line.slope, rel=1e-12)
        assert r["intercept"] == pytest.approx(expected.line.intercept, rel=1e-12)
        assert r["n_total"] == (24 if kind == "depth" else 6)
        assert r["bootstrap_method"] == ("resample" if kind == "depth" else "parametric")
        if kind == "depth":
            assert r["autocorrelation"] == pytest.approx(expected.line.autocorrelation)
    assert len(panel._overlays) == 2
    assert all(len(items) == 4 for _, items in panel._overlays)
    if space == "tera_wasserburg":
        assert panel._overlays[0][1][0].getData()[0][0] == 0
        assert panel.canvas._plot_item.vb.viewRange()[0][0] <= 0
    panel.together.setChecked(True)
    wait_for(app, lambda: len(panel.records) == 1 and panel.records[0].get("bootstrap_resamples") == 250)
    assert panel.records[0]["label"] == "selection"
    assert len(panel._overlays) == 1
    before = panel.records[0]["bootstrap_lower_ma_low"]
    panel.invalidate()
    wait_for(app, lambda: bool(panel.records) and panel.records[0].get("bootstrap_resamples") == 250)
    assert panel.records[0]["bootstrap_lower_ma_low"] == before
    assert len(panel._overlays) == 1


def test_bootstrap_background_progress_and_cancel_stale_selection(review, app, monkeypatch):
    import GUI.review_fits as module
    original = module.bootstrap_discordia
    threads, calls = [], []
    entered, release = threading.Event(), threading.Event()
    def bootstrap(*args, **kwargs):
        threads.append(QtCore.QThread.currentThread())
        calls.append(kwargs["seed"])
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "bootstrap_discordia", bootstrap)
    p = review.fit_panels["spot"]
    p.together.setChecked(True)
    p.resamples.setValue(500)
    p.fit_button.setChecked(True)
    wait_for(app, lambda: entered.is_set() and bool(p.records))
    assert p.records and "bootstrap_resamples" not in p.records[0]
    old = p._generation
    review.set_selected_spots({f"A_{i}" for i in range(4)})
    assert p._generation > old and not p.records
    release.set()
    wait_for(app, lambda: p.records and p.records[0].get("bootstrap_resamples") == 500)
    assert set(p.records[0]["items"]) == review.selected_spots
    assert all(t != app.thread() for t in threads)
    # The cancelled job stops after its blocked first chunk.
    assert calls.count(1) == 2 and len(calls) == 5
    current = list(p.records)
    p._accept((old, [], ["stale error"]))
    assert p.records == current and "stale error" not in p.readout.toPlainText()


def test_fit_settings_and_json_workspace_records_roundtrip(review, app, tmp_path):
    panel = fit(app, review)
    review.set_spot_excluded(["B_8"], True)
    wait_for(app, lambda: panel.records and all(r.get("bootstrap_resamples") == 250 for r in panel.records))
    from UPbRPy.project import json_safe_value
    workspace = json.loads(json.dumps(json_safe_value(dict(
        review_excluded_spots=["B_8", "missing"], review_fit_settings=review.fit_settings(), review_fits=review.fit_records())), allow_nan=False))
    restored = ResultsTableWidget()
    try:
        restored.apply_theme(THEMES["light"])
        f = frame()
        restored.show_results(SimpleNamespace(parallel_results=f, selected_results=f))
        restored.restore_review_state(workspace, f.spot_name)
        assert restored.excluded_spots == {"B_8"}
        assert restored.fit_settings() == review.fit_settings()
        assert restored.fit_records()[0]["items"] == review.fit_records()[0]["items"]
        assert all(restored.include_flags.values())
    finally:
        restored.fit_panels["spot"].fit_button.setChecked(False)
        restored.deleteLater()
        app.processEvents()


def test_concordia_fit_panels_fit_laptop(review, app):
    review.resize(1280, 800)
    from test_small_screens import clipped_widgets
    for kind in ("spot", "depth"):
        review.tabs.setCurrentWidget(getattr(review, kind + "_concordia_page"))
        panel = review.fit_panels[kind]
        panel.fit_button.setChecked(True)
        app.processEvents()
        page = getattr(review, kind + "_concordia_page")
        assert page.horizontalScrollBar().maximum() == 0
        menu = getattr(review, kind + "_fit_button").menu()
        menu.popup(review.mapToGlobal(QtCore.QPoint(10, 10)))
        for _ in range(5): app.processEvents()
        assert menu.isAncestorOf(panel.model_combo)
        assert menu.width() <= 600 and menu.height() <= 700
        menu.hide()
        assert not clipped_widgets(review)


def test_bundle_contains_isochron_table_and_exclusions(tmp_path):
    from test_export_table import synthetic_pipeline
    from UPbRPy.reproducibility import export_reduction_bundle
    f = frame()
    fit = fit_discordia(f["238U/206Pb"], f["207Pb/206Pb"], f["238U/206Pb"]*.01,
                       f["207Pb/206Pb"]*.01, -.15)
    record = fit_record(fit)
    record.update(label="A", view="spot means", items=list(f.spot_name), excluded=["B_3"])
    bundle = export_reduction_bundle(synthetic_pipeline(), tmp_path, formats=("csv",),
        include_audit_files=False, file_stem="session", excluded_spots=["B_3"], isochron_fits=[record])
    assert {"session_isochron_fits.csv", "session_isochron_fits.md", "session_isochron_fits_footnotes.txt"} <= {p.name for p in bundle.files}


def test_studio_hiding_does_not_exclude_export_or_send_page_fits(app, tmp_path, monkeypatch):
    import GUI.main_window as module
    from GUI.studio_window import StudioWindow
    from test_export_table import synthetic_pipeline
    shell = StudioWindow()
    shell.add_tab()
    window = shell.active_reduction
    pipeline = synthetic_pipeline()
    window.session.pipeline = pipeline
    window.session.directory = str(tmp_path / "session")
    window.results_table.set_spot_included(["B_3"], False)
    monkeypatch.setattr(window, "_require_loaded", lambda: True)
    monkeypatch.setattr(module, "ensure_shared_pipeline", lambda *args: pipeline)
    monkeypatch.setattr(window, "run_background", lambda label, fn, done: done(fn()))
    monkeypatch.setattr(window, "refresh_view", lambda: None)
    calls = []
    monkeypatch.setattr(module, "export_reduction_bundle", lambda *a, **kw: calls.append(kw) or SimpleNamespace(files=[], warnings=[], directory=tmp_path))
    for kind, panel in window.results_table.fit_panels.items():
        panel.records = [dict(label=kind, view=panel.view_name)]
    try:
        window.choose_reduction_bundle_export()
        app.processEvents()
        dialog = window._export_dialog
        assert dialog.excluded_spots == ()
        assert not dialog.table.rows.set_index("spot_name").loc["B_3", "excluded"]
        # A bootstrap finishing during the preview still belongs only to the project.
        window.results_table.fit_panels["spot"].records[0]["bootstrap_resamples"] = 2000
        dialog.folder_edit.setText(str(tmp_path))
        dialog.accept()
        app.processEvents()
        assert calls[0]["excluded_spots"] == ()
        assert "isochron_fits" not in calls[0]
        state = window.project_workspace_state()
        assert state["review_excluded_spots"] == []
        assert len(state["review_fits"]) == 2
        assert "include_flags" not in state
    finally:
        window.has_unsaved_changes = lambda: False
        shell.close()
        shell.deleteLater()
        app.processEvents()



def test_project_save_and_install_restores_exclusions_and_last_fits(app, tmp_path, monkeypatch):
    from GUI.services import RestoredProject
    from GUI.studio_window import StudioWindow
    from UPbRPy.pipeline import PipelineStage
    from UPbRPy.project import write_reduction_project, read_reduction_project
    from synthetic_bundles import write_raw_directory
    monkeypatch.setattr("UPbRPy.io.auto_select_baseline_peak", lambda experiment, *a, **k:
                        experiment.assign(baseline_start_times=0., baseline_end_times=4.,
                            plateau_start_times=8., plateau_end_times=16., signal_start_times=6., signal_end_times=18.))
    raw = tmp_path / "Apatite_synthetic"
    write_raw_directory(raw)
    shell = StudioWindow()
    shell.add_tab()
    window = shell.active_reduction
    try:
        window.load_experiment_directory(str(raw), background=False)
        names = list(window.session.spot_names)
        record = fit_record(fit_discordia([2, 3, 4, 5], [.71, .64, .57, .50], [.02]*4, [.01]*4))
        record.update(label="saved", view="spot means", items=names[:2], excluded=names[2:3])
        window.results_table.set_spot_excluded([names[0]], True)
        window.results_table.include_flags[names[1]] = False
        window.results_table.fit_panels["spot"].records = [record]
        target = write_reduction_project(window.session.pipeline, tmp_path / "review.upbr", workspace=window.project_workspace_state())
        workspace = read_reduction_project(target)["workspace"]
        workspace["review_excluded_spots"].append("absent")
        restored = RestoredProject(window.session, window.workflow_preset, target, None,
            PipelineStage.IMPORT, (PipelineStage.IMPORT,), workspace)
        window.results_table.set_spot_excluded([names[0]], False)
        window.install_restored_project(restored)
        assert window.results_table.excluded_spots == {names[0]}
        assert window.results_table.fit_records()[0]["label"] == "saved"
        assert names[1] not in window.results_table.include_flags or window.results_table.include_flags[names[1]]
        assert "include_flags" not in workspace
        workspace.clear()
        window.install_restored_project(restored)
        assert not window.results_table.excluded_spots
        assert not window.results_table.fit_records()
    finally:
        window.has_unsaved_changes = lambda: False
        shell.close()
        shell.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("model,anchor", [(1, "common_pb"), (2, "none"), (3, "none")])
def test_optimised_fits_run_in_worker_and_match_engine(review, app, monkeypatch, model, anchor):
    import GUI.review_fits as module
    original = module.fit_discordia
    threads = []
    def compute(*args, **kwargs):
        threads.append(QtCore.QThread.currentThread())
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "fit_discordia", compute)
    review.spot_diagram_buttons["tera_wasserburg"].setChecked(True)
    review.refresh_spot_concordia()
    panel = review.fit_panels["spot"]
    panel.together.setChecked(True)
    panel.model_combo.setCurrentIndex(model-1)
    panel.anchor_combo.setCurrentIndex(panel.anchor_combo.findData(anchor))
    panel.anchor_value.setValue(.85)
    panel = fit(app, review, resamples=4)
    f = review._spot_frame
    from UPbRPy.regression import Anchor
    expected = original(f["238U/206Pb"], f["207Pb/206Pb"],
        f["238U/206Pb_2e_with_calibration"]/2, f["207Pb/206Pb_2e_with_calibration"]/2,
        f.tw_rho, space="tera_wasserburg", model=model, anchor=Anchor(anchor, .85))
    assert panel.records[0]["intercept"] == pytest.approx(expected.line.intercept)
    assert threads and all(t != app.thread() for t in threads)


def test_fit_defaults_are_independent_per_page(app, tmp_path):
    settings = QtCore.QSettings(str(tmp_path / "review.ini"), QtCore.QSettings.Format.IniFormat)
    w = ResultsTableWidget(settings=settings)
    try:
        w.fit_panels["spot"].seed.setValue(37)
        w.fit_panels["spot"].resamples.setValue(600)
        w.fit_panels["depth"].model_combo.setCurrentIndex(1)
        other = ResultsTableWidget(settings=settings)
        try:
            assert other.fit_panels["spot"].seed.value() == 37
            assert other.fit_panels["spot"].resamples.value() == 600
            assert other.fit_panels["depth"].model_combo.currentIndex() == 1
            assert other.fit_panels["depth"].seed.value() == 1
            assert not other.fit_panels["depth"].decay_check.isChecked()
        finally:
            other.deleteLater()
    finally:
        w.deleteLater()
        app.processEvents()


def test_fit_pages_fit_studio_at_1280(app):
    from GUI.studio_window import StudioWindow
    from test_small_screens import clipped_widgets
    shell = StudioWindow()
    window = shell.active_reduction
    review = window.results_table
    try:
        shell.resize(1280, 800)
        shell.show()
        window.apply_default_layout()
        f = frame()
        review.show_results(SimpleNamespace(parallel_results=f, selected_results=f))
        review.show_depth_profile(depths(f), "A_0")
        window.session.plot_mode = "final_results"
        window.plots_stack.setCurrentWidget(review)
        for kind in ("spot", "depth"):
            page = getattr(review, kind + "_concordia_page")
            review.tabs.setCurrentWidget(page)
            review.fit_panels[kind].resamples.setValue(1)
            review.fit_panels[kind].fit_button.setChecked(True)
            for _ in range(8):
                app.processEvents()
            assert shell.size() == QtCore.QSize(1280, 800)
            assert page.horizontalScrollBar().maximum() == 0
            assert not clipped_widgets(shell)
    finally:
        for panel in review.fit_panels.values():
            panel.fit_button.setChecked(False)
        window.has_unsaved_changes = lambda: False
        shell.close()
        shell.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("kind", ["spot", "depth"])
def test_selection_action_menu_triggers_visibility_and_calculation_actions(review, kind):
    review.set_selected_spots({"A_0", "A_1"})
    canvas = getattr(review, kind + "_concordia")
    menu = review._action_menu(canvas, kind)
    try:
        def trigger(prefix):
            next(a for a in menu.actions() if a.text().startswith(prefix)).trigger()
        trigger("Show only selected")
        assert {n for n, shown in review.include_flags.items() if shown} == {"A_0", "A_1"}
        trigger("Hide 2 spots from calculations")
        assert review.excluded_spots == {"A_0", "A_1"}
        trigger("Unhide 2 spots")
        assert not review.excluded_spots
        trigger("Remove selected from plot")
        assert not any(review.include_flags.values())
        trigger("Show all")
        assert all(review.include_flags.values())
        trigger("Fit selected")
        assert review.fit_panels[kind].fit_button.isChecked()
    finally:
        menu.deleteLater()



def test_restored_live_fit_keeps_recorded_scope_until_inputs_change(review, app):
    review.set_selected_spots({"A_0", "A_1", "A_2", "A_3"})
    review.spot_diagram_buttons["tera_wasserburg"].setChecked(True)
    review.refresh_spot_concordia()
    panel = fit(app, review)
    saved = dict(review_fit_settings=review.fit_settings(), review_fits=review.fit_records())
    restored = ResultsTableWidget()
    try:
        restored.apply_theme(THEMES["light"])
        f = frame()
        restored.show_results(SimpleNamespace(parallel_results=f, selected_results=f))
        restored.restore_review_state(saved, f.spot_name)
        QtTest.QTest.qWait(150)
        app.processEvents()
        assert restored.fit_records()[0]["items"] == panel.records[0]["items"]
        assert restored.spot_concordia._diagram == "tera_wasserburg"
        assert restored.spot_concordia._plot_item.vb.viewRange()[0][0] <= 0
        assert not restored.fit_panels["spot"]._timer.isActive()
        restored.set_selected_spots({"B_6", "B_7", "B_8"})
        wait_for(app, lambda: restored.fit_records() and set(restored.fit_records()[0]["items"]) == {"B_6", "B_7", "B_8"})
    finally:
        restored.fit_panels["spot"].fit_button.setChecked(False)
        QtCore.QThreadPool.globalInstance().waitForDone(10000)
        restored.deleteLater()
        app.processEvents()
