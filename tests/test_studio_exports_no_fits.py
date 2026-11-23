"""Studio exports data while retaining synthetic concordia fits in projects."""

import json
import zipfile
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from UPbRPy.pipeline import PipelineStage
from UPbRPy.project import json_safe_value, read_reduction_project
from test_export_table import synthetic_pipeline
from test_review_calculation_fits import app, depths, fit, frame, review, wait_for
from test_spot_edits import window


def assert_no_fit_output(folder):
    """Search all written text, including workbook XML, for fit records."""
    # The manifest keeps an empty "isochron_fits" list (unchanged engine format).
    forbidden = (b"review_fits", b"bootstrap_resamples",
                 b"cov_intercept_slope", b"lower_age_ma")
    files = [path for path in folder.rglob("*") if path.is_file()]
    assert files
    for path in files:
        assert "isochron" not in path.name.lower()
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                contents = [archive.read(name) for name in archive.namelist()]
        else:
            contents = [path.read_bytes()]
        for content in contents:
            assert not any(token in content.lower() for token in forbidden), path


@pytest.mark.parametrize("audit", [False, True])
def test_results_export_omits_live_fits_and_project_save_retains_them(
        window, app, tmp_path, monkeypatch, audit):
    import GUI.main_window as module
    from UPbRPy.reproducibility import export_reduction_bundle

    pipeline = synthetic_pipeline()
    pipeline.source_directory = window.session.directory
    finalized = pipeline.run_finalize()
    pipeline.cache.set(PipelineStage.SPOT_MEANS, finalized.parallel_results)
    pipeline.cache.set(PipelineStage.DEPTH_PROFILE, depths(frame()))
    window.session.pipeline = pipeline
    review = window.results_table
    f = frame()
    review.show_results(SimpleNamespace(parallel_results=f, selected_results=f.copy()))
    review.show_depth_profile(depths(f), "A_0")
    review.show_all()
    review.set_selected_spots({"A_0", "A_1", "A_2", "A_3"})
    for kind in ("spot", "depth"):
        fit(app, review, kind, resamples=4)
    wait_for(app, lambda: all(not panel._workers for panel in review.fit_panels.values()))
    records = json_safe_value(review.fit_records())
    assert len(records) == 2
    assert all(panel.fit_button.isChecked() for panel in review.fit_panels.values())
    monkeypatch.setattr(module, "ensure_shared_pipeline", lambda *args: pipeline)
    monkeypatch.setattr(window, "run_background", lambda label, fn, done: done(fn()))
    monkeypatch.setattr(window, "refresh_view", lambda: None)
    calls = []

    def export(*args, **kwargs):
        calls.append(kwargs)
        assert "isochron_fits" not in kwargs
        return export_reduction_bundle(*args, **kwargs)

    monkeypatch.setattr(module, "export_reduction_bundle", export)
    target = tmp_path / "export"
    try:
        window.choose_reduction_bundle_export()
        app.processEvents()
        dialog = window._export_dialog
        dialog.folder_edit.setText(str(target))
        dialog.audit_check.setChecked(audit)
        dialog.accept()
        app.processEvents()
        assert len(calls) == 1
        assert calls[0]["include_audit_files"] == audit
        assert_no_fit_output(target)
        if audit:
            manifest = json.loads((target / "reduction_manifest.json").read_text())
            assert not manifest["metadata"].get("isochron_fits")
        else:
            assert not (target / "reduction_manifest.json").exists()
        assert json_safe_value(review.fit_records()) == records
        # Exercise the main-window save path, using the same prepared pipeline.
        monkeypatch.setattr(window, "apply_pending_project_edits", lambda: None)
        monkeypatch.setattr(module.results_cache_warmer, "schedule", lambda *args: None)
        monkeypatch.setattr(window, "_handle_action_error", lambda title, error: pytest.fail(f"{title}: {error}"))
        project = tmp_path / "with_fits.upbr"
        window.save_project(project)
        workspace = read_reduction_project(project)["workspace"]
        assert workspace["review_fits"] == records
        assert all(workspace["review_fit_settings"][kind]["enabled"] for kind in ("spot", "depth"))
    finally:
        for panel in review.fit_panels.values():
            panel.fit_button.setChecked(False)
        QtCore.QThreadPool.globalInstance().waitForDone(10000)


@pytest.mark.parametrize("kind", ["spot", "depth"])
def test_fit_controls_have_no_export(review, app, kind):
    panel = fit(app, review, kind, resamples=4)
    popover = getattr(review, kind + "_fit_button").menu()
    assert not any("Export" in button.text() for button in popover.findChildren(QtWidgets.QPushButton))
    assert not any("Export" in action.text() for action in panel.options_button.menu().actions())
    assert "export" not in panel.options_button.toolTip().lower()
    assert not hasattr(panel, "export_fits")
