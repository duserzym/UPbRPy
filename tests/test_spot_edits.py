"""Synthetic 1B-style spot edits replay through calculations and projects."""

from datetime import datetime, timedelta
import os
from copy import deepcopy
import json

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from GUI import main_window, services
from GUI.main_window import MainWindow
from GUI.theme import active_theme, build_application
from UPbRPy.io import sample_from_label
from UPbRPy.notices import ReductionError
from UPbRPy.pipeline import PipelineStage
from UPbRPy.spot_edits import original_spot_label


LABELS = [f"NIST610_{n}" for n in range(795, 798)] + [f"NIST612_{n}" for n in range(849, 852)]
LABELS += [f"91500_{n}" for n in range(1, 4)] + ["unknown_1", "unknown_2"]
CHANNELS = ("29Si", "91Zr", "202Hg", "204Hg", "206Pb", "207Pb", "208Pb", "232Th", "238U")


def write_session(folder):
    folder.mkdir()
    for number, label in enumerate(LABELS):
        start = datetime(2026, 11, 1) + timedelta(minutes=number)
        lines = [f"{label}:{start.strftime('%m/%d/%Y %I:%M:%S %p')};\n"]
        lines += ["Meta:x=1;\n"] * 12
        lines += ["Time," + ",".join(CHANNELS) + ",\n", "," + ",".join(["dwell time=0.075"] * len(CHANNELS)) + ",\n"]
        scale = 4 if label.startswith("NIST610") or label == "NIST612_849" else 1
        for sweep in range(44):
            values = [300000, 400000, 1, 0.2, 10000, 800, 3000, 50000, 100000]
            values = [10 + value * scale * (1 + sweep / 10000) if sweep >= 12 else 10
                      for value in values]
            lines.append(f"{sweep * .5}," + ",".join(map(str, values)) + ",\n")
        (folder / f"spot{number}.csv").write_text("".join(lines))
    return folder


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: pytest.fail(str(args[1:])))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *args: pytest.fail(str(args[1:])))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *args: QtWidgets.QMessageBox.StandardButton.Ok)
    monkeypatch.setenv("UPBRPY_AUTOSAVE_DIR", str(tmp_path / "autosave"))
    window = MainWindow()
    window.load_experiment_directory(str(write_session(tmp_path / "raw")), background=False, offer_restore=False)
    window.session.primary_68_standard = window.session.primary_76_standard = window.session.primary_82_standard = "91500"
    window.session.session_fit_method = "mean"
    window._populate_selectors()
    # Known synthetic windows, independent of auto-pick heuristics.
    for prefix, a, b in (("baseline", 1, 5), ("plateau", 8, 17), ("signal", 6, 20)):
        window.session.experiment[f"{prefix}_start_times"] = window.session.experiment.spot_starting_times + a
        window.session.experiment[f"{prefix}_end_times"] = window.session.experiment.spot_starting_times + b
    window.selection_autosave_debounce_ms = 60000
    yield window
    for timer in (window._selection_edit_timer, window._selection_recompute_timer, window._selection_autosave_timer):
        timer.stop()
    window.has_unsaved_changes = lambda: False
    window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def pipeline(window):
    return services.ensure_shared_pipeline(window.session, window.workflow_preset)


def test_label_rule():
    assert sample_from_label("nIsT612_849", "_", {" NIST612 ": "NIST610"}) == "NIST610"
    assert sample_from_label("MAD-UCSB 4", " ", {"mad-ucsb": "MADUCSB"}) == "MADUCSB"
    from UPbRPy.spot_edits import restore_spot_edits
    imported = pd.DataFrame({"spot_names": ["KD27_4"], "sample": ["SMP2"]})
    imported.attrs["sample_overrides"] = {"KD27_4": {"sample": "SMP2", "label_sample": "KD27", "source": "session_manifest"}}
    total = pd.DataFrame({"spot_name": ["KD27_4"], "sample": ["SMP2"]})
    restored, sweeps = restore_spot_edits(imported, total, {"sample_overrides": {}})
    assert restored["sample"].tolist() == ["KD27"]
    assert sweeps["sample"].tolist() == ["KD27"]


def test_rename_calibration_history_and_raw_identity(window, monkeypatch):
    name = "NIST612_849"
    window.session.selected_spot = name
    window._select_spot_in_tree(name)
    regions = services.get_selection_regions(window.session, name)
    window._review_flags[name] = "counts resemble 610"
    window.results_table.include_flags[name] = False
    window.results_table.selected_spots = {name}
    window._selection_undo.append(({name: regions}, {name: regions}))
    captured = []
    from UPbRPy import concentration
    original = concentration.calc_concentration_calibration_factor
    def calibration(data, *args, **kwargs):
        captured.append(data.copy())
        return original(data, *args, **kwargs)
    monkeypatch.setattr(concentration, "calc_concentration_calibration_factor", calibration)
    assert window.request_rename_spot(name, "NIST610_849")
    new = "NIST610_849"
    assert window._spot_tree_item(new).parent().text(0) == "NIST610"
    assert services.get_selection_regions(window.session, new) == regions
    assert window.session.selected_spot == new
    assert window._review_flags[new] == "counts resemble 610"
    assert window.results_table.include_flags[new] is False
    assert window.results_table.selected_spots == {new}
    assert new in window._selection_undo[0][0]
    assert window.session.total_data.loc[window.session.total_data.spot_name.eq(new), "sample"].eq("NIST610").all()
    assert window.session.experiment.attrs["spot_renames"] == {new: name}
    assert window.session.experiment.attrs["sample_overrides"][new] == {
        "label_sample": "NIST612", "sample": "NIST610", "source": "studio"}
    services.ensure_concentration_results(window.session, window.workflow_preset)
    assert captured[-1].loc[captured[-1].spot_name.eq(new), "sample"].eq("NIST610").all()
    assert new in set(captured[-1].spot_name)
    assert pipeline(window).manifest()["spot_renames"] == {new: name}
    from UPbRPy.reproducibility import build_reproducibility_notebook
    from UPbRPy.spot_edits import restore_spot_edits
    from UPbRPy.pipeline import pipeline_config_from_dict
    from UPbRPy.io import read_experiment_data
    from pathlib import Path
    import json
    notebook = build_reproducibility_notebook(pipeline(window))
    namespace = dict(Path=Path, json=json, pd=pd, read_experiment_data=read_experiment_data,
                     restore_spot_edits=restore_spot_edits, pipeline_config_from_dict=pipeline_config_from_dict)
    for index in (2, 4, 5):
        exec("".join(notebook["cells"][index]["source"]), namespace)
    assert new in set(namespace["experiment"].spot_names)
    assert namespace["total_data"].loc[namespace["total_data"].spot_name.eq(new), "sample"].eq("NIST610").all()
    restored_row = namespace["experiment"].set_index("spot_names").loc[new]
    for region, limits in regions.items():
        if limits is not None:
            assert tuple(restored_row[f"{region}_{edge}_times"] for edge in ("start", "end")) == limits
    submitted = services.pipeline_for_database_submission(window.session, window.workflow_preset)
    assert submitted.experiment.attrs["spot_renames"] == {}
    assert name in set(submitted.experiment.spot_names) and new not in set(submitted.experiment.spot_names)
    assert submitted.experiment.loc[submitted.experiment.spot_names.eq(name), "sample"].iloc[0] == "NIST610"
    window.undo_selection_edit()
    assert name in window.session.spot_names and new not in window.session.spot_names
    assert services.get_selection_regions(window.session, name) == regions
    assert window._review_flags[name]
    window.redo_selection_edit()
    assert new in window.session.spot_names
    assert original_spot_label(window.session.experiment, new) == name
    assert window.request_rename_spot(new, "NIST610_999")
    assert original_spot_label(window.session.experiment, "NIST610_999") == name
    assert window.request_rename_spot("NIST610_999", name)
    assert window.session.experiment.attrs["spot_renames"] == {}
    assert name not in window.session.experiment.attrs["sample_overrides"]


def test_invalid_and_cancelled_rename(window, monkeypatch):
    warnings = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *args: warnings.append(args))
    for text in ("", "   ", "nist610_795"):
        assert not window.request_rename_spot("NIST612_849", text)
    assert len(warnings) == 3
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *args: QtWidgets.QMessageBox.StandardButton.Cancel)
    assert not window.request_rename_spot("NIST612_849", "NIST610_849")
    assert "NIST612_849" in window.session.spot_names


def test_inline_editor_geometry_selection_and_escape(window, app):
    name = "NIST612_849"
    window.resize(1440, 900)
    window.show()
    window._select_spot_in_tree(name)
    tree = window.spot_list
    tree.setFocus()
    QtTest.QTest.keyClick(tree, QtCore.Qt.Key.Key_F2)
    app.processEvents()
    editor = tree.findChild(QtWidgets.QLineEdit)
    assert editor is not None
    assert editor.selectedText() == name
    assert editor.height() >= editor.fontMetrics().height() + 8
    assert editor.width() >= editor.fontMetrics().horizontalAdvance(name) + 8
    editor.setText("NIST610_849")
    QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Escape)
    app.processEvents()
    assert name in window.session.spot_names
    assert "NIST610_849" not in window.session.spot_names
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    window._select_spot_in_tree(name)
    tree.setFocus()
    QtTest.QTest.qWait(30)
    QtTest.QTest.keyClick(tree, QtCore.Qt.Key.Key_F2)
    app.processEvents()
    edits = tree.findChildren(QtWidgets.QLineEdit)
    assert any(edit.isVisible() for edit in edits)
    editor = next(edit for edit in edits if edit.isVisible())
    editor.setText("NIST612_999")
    QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Return)
    app.processEvents()
    app.processEvents()
    assert "NIST612_999" in window.session.spot_names


@pytest.mark.parametrize("target", ["nist610", "NIST 610", "NIST-610"])
def test_material_move_matches_existing_group_and_undo(window, target, monkeypatch):
    baseline = services.ensure_baseline_subtracted(window.session)
    assert window.request_move_spot("NIST612_849", target)
    assert window._spot_tree_item("NIST612_849").parent().text(0) == "NIST610"
    assert target == "NIST610" or target not in window._get_all_folder_names()
    assert "NIST612_849" not in window._spot_folder_overrides
    assert pipeline(window).cache.has(PipelineStage.BASELINE)
    assert not pipeline(window).cache.has(PipelineStage.NORMALIZATION)
    moved = pipeline(window).run_baseline()
    assert moved.loc[moved.spot_name.eq("NIST612_849"), "sample"].eq("NIST610").all()
    window.undo_selection_edit()
    assert window._spot_tree_item("NIST612_849").parent().text(0) == "NIST612"
    window.redo_selection_edit()
    assert window._spot_tree_item("NIST612_849").parent().text(0) == "NIST610"


def test_remove_rerun_undo_rerun_and_exclusions(window):
    pipeline(window).run_finalize()
    window.request_remove_spot("NIST612_849")
    assert "NIST612_849" not in set(pipeline(window).total_data.spot_name)
    pipeline(window).run_finalize()
    window.undo_selection_edit()
    assert "NIST612_849" in set(pipeline(window).total_data.spot_name)
    pipeline(window).run_finalize()
    assert window.request_calculation_exclusion("NIST612_849", True)
    assert "NIST612_849" not in set(pipeline(window).run_baseline().spot_name)
    pipeline(window).run_finalize()
    window.undo_selection_edit()
    assert "NIST612_849" not in window.session.experiment.attrs.get("calculation_excluded_spots", ())
    assert "NIST612_849" in set(pipeline(window).run_baseline().spot_name)
    window.redo_selection_edit()
    assert "NIST612_849" in window.session.experiment.attrs["calculation_excluded_spots"]
    assert window.request_calculation_exclusion("NIST612_849", False)
    assert "NIST612_849" in set(pipeline(window).run_baseline().spot_name)


def test_empty_primary_error(window):
    for name in LABELS:
        if name.startswith("91500"):
            assert window.request_calculation_exclusion(name, True)
    with pytest.raises(ReductionError, match="91500") as error:
        pipeline(window).run_downhole()
    assert "move spots back" in str(error.value) or "move spots back" in error.value.next_step
    assert window._active_worker is None


def test_project_replays_renames_material_folders_exclusions_and_removal(window, tmp_path, monkeypatch):
    assert window.request_rename_spot("NIST612_849", "NIST610_849")
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *args, **kwargs: ("Custom folder", True))
    assert window._on_spot_add_folder() == "Custom folder"
    assert "Custom folder" in window._get_all_folder_names()
    window.results_table.set_spot_excluded({"unknown_1"}, True)
    assert window.request_calculation_exclusion("NIST612_850", True)
    window.request_remove_spot("unknown_2")
    project = tmp_path / "edited.upbr"
    window.save_project(project)
    window.load_project_file(project, background=False, use_cache=False)
    assert "NIST610_849" in window.session.spot_names
    assert "NIST612_849" not in window.session.spot_names
    assert "unknown_2" not in window.session.spot_names
    assert window.results_table.excluded_spots == {"unknown_1"}
    assert "unknown_1" in set(pipeline(window).run_baseline().spot_name)
    assert "NIST612_850" not in set(pipeline(window).run_baseline().spot_name)
    assert "Custom folder" in window._get_all_folder_names()
    assert original_spot_label(window.session.experiment, "NIST610_849") == "NIST612_849"
    assert window._spot_tree_item("NIST610_849").parent().text(0) == "NIST610"
    reimported = services.reimport_detector_calibration(window.session, {"mode": "qtegra"})
    assert "NIST610_849" in reimported.spot_names and "unknown_2" not in reimported.spot_names
    assert reimported.experiment.attrs["calculation_excluded_spots"] == ["NIST612_850"]


def test_folder_changes_preserve_calculations_and_history(window, monkeypatch, app):
    baseline = pipeline(window).run_baseline()
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *args, **kwargs: ("Review 2", True))
    window._on_spot_add_folder()
    selected = window.session.selected_spot
    folder = window.spot_list.findItems("Review 2", QtCore.Qt.MatchFlag.MatchRecursive | QtCore.Qt.MatchFlag.MatchExactly)[0]
    window._on_spot_tree_item_changed(folder, None)
    assert window.session.selected_spot == selected
    assert pipeline(window).run_baseline() is baseline
    window.undo_selection_edit()
    assert "Review 2" not in window._get_all_folder_names()
    assert pipeline(window).run_baseline() is baseline
    window.redo_selection_edit()
    assert "Review 2" in window._get_all_folder_names()
    assert window.request_move_spot("unknown_1", "review-2")
    assert window._spot_folder_overrides["unknown_1"] == "Review 2"
    assert pipeline(window).run_baseline() is baseline

    # New folder + a confirmed RM reassignment is one menu operation and
    # undo must refresh sample labels in the retained baseline cache too.
    window.resize(1440, 900)
    window.show()
    window._select_spot_in_tree("NIST612_849")
    app.processEvents()
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *args, **kwargs: ("Review 3", True))
    count = len(window._selection_undo)
    window._request_new_folder_move("NIST612_849")
    assert len(window._selection_undo) == count + 1
    assert window._spot_tree_item("NIST612_849").parent().text(0) == "Review 3"
    window.undo_selection_edit()
    assert "Review 3" not in window._get_all_folder_names()
    restored = pipeline(window).run_baseline()
    assert restored.loc[restored.spot_name.eq("NIST612_849"), "sample"].eq("NIST612").all()
    window.redo_selection_edit()
    assert window._spot_tree_item("NIST612_849").parent().text(0) == "Review 3"
    # Display-folder matching must not alter the reader's sample rule on rename.
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *args, **kwargs: ("Other sample", True))
    window._on_spot_add_folder()
    assert window.request_rename_spot("unknown_2", "Other-sample_2")
    assert window._spot_sample_map()["Other-sample_2"] == "Other-sample"


def test_worker_after_removal_and_undo(window, monkeypatch):
    monkeypatch.setenv("UPBRPY_STAGE_PROCESS", "1")
    from GUI import stage_process
    from UPbRPy.export_table import ExportSettings
    pool = stage_process.StagePool()
    try:
        pipeline(window).run_finalize()
        window.request_remove_spot("NIST612_849")
        for removed in (True, False):
            task = stage_process.StageTask(pool=pool)
            task.prepare(window.session, window.workflow_preset.key, ExportSettings(), "final_review")
            reply = task.run(lambda: stage_process.run_stage(window.session, window.workflow_preset.key,
                                                            ExportSettings(), "final_review"))
            reply.merge(window.session, window.workflow_preset)
            assert ("NIST612_849" not in set(pipeline(window).total_data.spot_name)) == removed
            assert pipeline(window).cache.has(PipelineStage.FINALIZE)
            if removed:
                window.undo_selection_edit()
    finally:
        pool.shutdown()


def test_database_matching_5000_labels_needs_no_row_requests(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from time import perf_counter
    from GUI.database import _verified_session_origin
    from UPbRPy import qtegra, project
    from UPbRPy.session_manifest import SESSION_MANIFEST_FILENAME
    labels = [f"unknown_{i}" for i in range(5000)]
    (tmp_path / SESSION_MANIFEST_FILENAME).write_text("\n".join(
        f'[[acquisition]]\nlabel = "{name}"' for name in labels))
    raw = SimpleNamespace(labels=labels, sources=[], format="rich")
    calls = []
    monkeypatch.setattr(qtegra, "open_qtegra", lambda path: calls.append(path) or raw)
    monkeypatch.setattr(services, "raw_input_files", lambda source: ())
    origin = {"server": "https://lab.example.org", "session_id": "session-1"}
    monkeypatch.setattr(project, "read_reduction_project", lambda path: {"origin": origin})
    bundle = SimpleNamespace(path=tmp_path, raw_directory=tmp_path / "raw",
                             project_file=tmp_path / "test.upbr", session_id="session-1")
    session = SimpleNamespace(qtegra=raw, experiment=pd.DataFrame({"spot_names": labels}), spot_names=labels)
    before = perf_counter()
    _verified_session_origin(session, bundle)
    initial_time = perf_counter() - before
    assert len(calls) == 1  # One local bundle parse, zero server calls.
    names = [f"reviewed_{i}" for i in range(5000)]
    session.experiment = pd.DataFrame({"spot_names": names})
    session.experiment.attrs["spot_renames"] = dict(zip(names, labels))
    session.spot_names = names
    before = perf_counter()
    _verified_session_origin(session, bundle)
    renamed_time = perf_counter() - before
    assert len(calls) == 2  # No per-acquisition parsing or network work added.
    print(f"5000 labels: original={initial_time:.4f}s renamed={renamed_time:.4f}s; server calls=0/0")


@pytest.mark.parametrize("source", ["session_manifest", "upbdb"])
def test_rename_preserves_registered_sample_for_same_prefix(window, monkeypatch, source):
    old_name = "unknown_1"
    override = {"label_sample": "unknown", "sample": "DS-01", "source": source}
    window.session.experiment.attrs["sample_overrides"] = {old_name: override}
    window.session.experiment.loc[window.session.experiment.spot_names.eq(old_name), "sample"] = "DS-01"
    window.session.total_data.loc[window.session.total_data.spot_name.eq(old_name), "sample"] = "DS-01"
    previews = []
    set_material = window._set_spot_material
    calls = []
    monkeypatch.setattr(window, "_set_spot_material",
                        lambda *args: calls.append(args) or set_material(*args))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *args: previews.append(args[2]) or QtWidgets.QMessageBox.StandardButton.Ok)
    assert window.request_rename_spot(old_name, "unknown_1b")
    assert not previews and not calls
    assert window._spot_sample_map()["unknown_1b"] == "DS-01"
    assert window.session.total_data.loc[window.session.total_data.spot_name.eq("unknown_1b"), "sample"].eq("DS-01").all()
    assert window.session.experiment.attrs["sample_overrides"] == {"unknown_1b": override}
    assert not pipeline(window).cache.has(PipelineStage.BASELINE)
    assert window.request_rename_spot("unknown_1b", "NIST610_1")
    assert len(previews) == 1 and "DS-01" in previews[0] and "NIST610" in previews[0]
    assert calls == [("NIST610_1", "NIST610")]
    assert window._spot_sample_map()["NIST610_1"] == "NIST610"
    assert window.session.experiment.attrs["sample_overrides"]["NIST610_1"]["source"] == "studio"
    window.undo_selection_edit()
    assert window.session.experiment.attrs["sample_overrides"] == {"unknown_1b": override}


def test_remove_preserves_other_fit_records_and_undo(window):
    records = [
        {"items": ["unknown_1", "unknown_2"], "excluded": ["unknown_1"], "label": "mixed fit"},
        {"items": ["NIST612_849", "NIST612_850"], "excluded": ["NIST612_850"], "label": "other fit"},
    ]
    for panel in window.results_table.fit_panels.values():
        panel.records = deepcopy(records)
    window.results_table.set_spot_excluded({"unknown_1", "unknown_2"}, True)
    assert window.request_calculation_exclusion("unknown_1", True)
    window.request_remove_spot("unknown_1")
    assert window.results_table.excluded_spots == {"unknown_2"}
    assert window.session.experiment.attrs["calculation_excluded_spots"] == []
    for panel in window.results_table.fit_panels.values():
        assert panel.records == [
            {"items": ["unknown_2"], "excluded": [], "label": "mixed fit"}, records[1]]
    window.undo_selection_edit()
    assert window.results_table.excluded_spots == {"unknown_1", "unknown_2"}
    assert window.session.experiment.attrs["calculation_excluded_spots"] == ["unknown_1"]
    for panel in window.results_table.fit_panels.values():
        assert panel.records == records


def test_review_exclusion_retains_reduction_and_export_rows(window):
    from UPbRPy.export_table import build_export_table
    pipe = pipeline(window)
    baseline = pipe.run_baseline()
    finalized = pipe.run_finalize()
    history_size = len(window._selection_undo)
    window.results_table.set_spot_excluded({"unknown_1"}, True)
    assert pipeline(window).run_baseline() is baseline
    assert pipeline(window).run_finalize() is finalized
    assert len(window._selection_undo) == history_size
    assert not window.session.experiment.attrs.get("calculation_excluded_spots")
    exported = build_export_table(pipe, excluded_spots=window.results_table.excluded_spots)
    row = exported.rows.set_index("spot_name").loc["unknown_1"]
    assert row["excluded"]
    assert "excluded" in row["notes"].lower()
    assert "unknown_2" in set(exported.rows.spot_name)


def test_legacy_review_exclusions_do_not_become_calculation_exclusions(window, tmp_path):
    window.results_table.set_spot_excluded({"unknown_1"}, True)
    project = tmp_path / "legacy.upbr"
    window.save_project(project)
    document = json.loads(project.read_text())
    document.pop("spot_edits", None)
    project.write_text(json.dumps(document))
    restored = services.restore_session_from_project(project)
    assert not restored.session.experiment.attrs.get("calculation_excluded_spots")
    window.install_restored_project(restored)
    assert window.results_table.excluded_spots == {"unknown_1"}
    assert "unknown_1" in set(pipeline(window).run_baseline().spot_name)
    directory = window.session.directory
    window.install_loaded_session(services.load_session(directory, preset=window.workflow_preset), str(directory))
    assert not window.results_table.excluded_spots


def test_calculation_exclusion_tree_raw_trace_and_provenance(window):
    name = "unknown_1"
    pipe = pipeline(window)
    pipe.run_finalize()
    window._select_spot_in_tree(name)
    assert window.request_calculation_exclusion(name, True)
    assert window.results_table.excluded_spots == set()
    assert not pipe.cache.has(PipelineStage.BASELINE)
    assert window._spot_rerun_stage == PipelineStage.BASELINE
    assert "re-run baseline" in window.workflow_status_label.text()
    item = window._spot_tree_item(name)
    assert item.foreground(0).color() == QtGui.QColor(active_theme().muted)
    assert "Left out of every calculation (undo or 'Use in calculations' to restore)" in item.toolTip(0)
    assert item.flags() & QtCore.Qt.ItemFlag.ItemIsSelectable
    window.on_spot_changed(name)
    assert window.session.selected_spot == name and window.session.plot_mode == "raw"
    assert name in set(window.session.total_data.spot_name)
    assert name not in set(pipe.run_baseline().spot_name)
    assert pipe.manifest()["calculation_excluded_spots"] == [name]
    assert "excluded_spots" not in pipe.manifest()
    assert window.request_rename_spot(name, "unknown_1b")
    assert window.session.experiment.attrs["calculation_excluded_spots"] == ["unknown_1b"]
    submitted = services.pipeline_for_database_submission(window.session, window.workflow_preset)
    assert submitted.experiment.attrs["calculation_excluded_spots"] == [name]
    assert name not in set(submitted.run_baseline().spot_name)
    assert window.request_calculation_exclusion("unknown_1b", False)
    assert "unknown_1b" in set(pipeline(window).run_baseline().spot_name)


def test_calculation_exclusion_begin_guard(window, monkeypatch):
    monkeypatch.setattr(window, "_begin_spot_edit", lambda: None)
    assert not window.request_calculation_exclusion("unknown_1", True)
    assert not window.session.experiment.attrs.get("calculation_excluded_spots")


def test_calculation_exclusion_context_menu_toggle(window, monkeypatch):
    name = "unknown_1"
    monkeypatch.setattr(window.spot_list, "itemAt", lambda pos: window._spot_tree_item(name))
    class Menu(QtWidgets.QMenu):
        def exec(self, pos):
            actions = {action.text(): action for action in self.actions()}
            assert "Exclude spot" not in actions
            return actions[label]
    monkeypatch.setattr(main_window.QtWidgets, "QMenu", Menu)
    for label, expected in (("Leave out of calculations", [name]), ("Use in calculations", [])):
        window._on_spot_tree_context_menu(QtCore.QPoint())
        assert window.session.experiment.attrs["calculation_excluded_spots"] == expected
        assert not window.results_table.excluded_spots


def test_move_menu_names_groups_and_disables_current_folder(window, app):
    item = window._spot_tree_item("NIST612_849")
    pos = window.spot_list.visualItemRect(item).center()
    seen = {}

    def inspect():
        menu = QtWidgets.QApplication.activePopupWidget()
        if menu is None:
            QtCore.QTimer.singleShot(20, inspect)
            return
        move = next(action.menu() for action in menu.actions() if action.menu())
        seen["rows"] = [(action.text().strip(), action.isEnabled(), action.isChecked())
                        for action in move.actions() if not action.isSeparator()]
        menu.close()

    QtCore.QTimer.singleShot(0, inspect)
    window._on_spot_tree_context_menu(pos)
    rows = seen["rows"]
    # Group headings are visible, disabled rows (macOS hides addSection titles).
    assert ("Reference Materials", False, False) in rows
    assert ("NIST612", False, True) in rows  # the spot's own folder
    assert ("NIST610", True, False) in rows
    assert rows[-1][0] == "New folder…"
