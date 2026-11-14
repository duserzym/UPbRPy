"""Exact restore regression checks, with an optional external real-data baseline."""

from dataclasses import dataclass
import os
from pathlib import Path
import pickle
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts.snapshot_restore import compare_snapshots, snapshot_project, snapshot_restored


RICH = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice/Zircon_04Nov26_slice_Rich.csv"


@pytest.mark.skipif(
    not (os.environ.get("UPBRPY_IDENTITY_PROJECT") and os.environ.get("UPBRPY_IDENTITY_BASELINE")),
    reason="Set UPBRPY_IDENTITY_PROJECT and UPBRPY_IDENTITY_BASELINE for a cross-version check",
)
def test_saved_project_matches_earlier_version():
    with Path(os.environ["UPBRPY_IDENTITY_BASELINE"]).expanduser().open("rb") as stream:
        baseline = pickle.load(stream)
    current = snapshot_project(Path(os.environ["UPBRPY_IDENTITY_PROJECT"]).expanduser())
    assert compare_snapshots(baseline, current) == []


def test_fixture_restore_is_deterministic_through_finalization(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtCore = pytest.importorskip("PySide6.QtCore")
    from PySide6 import QtWidgets
    from GUI import main_window, services
    from GUI.main_window import MainWindow
    from GUI.theme import build_application
    from UPbRPy.pipeline import PipelineStage

    app = QtWidgets.QApplication.instance() or build_application([], theme_mode="light")
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: pytest.fail(str(args[1:])))
    original_load = services.load_session

    def load_with_test_hg(*args, **kwargs):
        session = original_load(*args, **kwargs)
        # As in test_bias.test_demo_session_shared_pipeline, the slice
        # needs a synthetic 202Hg background to run ratios. Apply it on load
        # AND restore. This checks determinism, not mass-204 accuracy.
        session.total_data = session.total_data.assign(**{"202Hg": 1.0})
        session.channels = [*session.channels, "202Hg"]
        session.pipeline = None
        return session

    monkeypatch.setattr(services, "load_session", load_with_test_hg)
    # MainWindow imports the function into its own module namespace.
    monkeypatch.setattr(main_window, "load_session", load_with_test_hg)
    window = MainWindow(theme_mode="light")
    try:
        window.resize(1440, 900)
        window.show()
        window.load_experiment_directory(str(RICH), background=False)
        app.processEvents()
        assert window.session.loaded
        # Save commits the standards editors; settle their ordering before
        # reduction so that saving does not invalidate the completed stages.
        window.apply_pending_project_edits()
        # The fixture has only two S513 spots: use the mean session fit, as
        # the existing full-pipeline fixture tests do, rather than a polynomial.
        window.session.session_fit_method = "mean"
        pipeline = services.ensure_shared_pipeline(window.session, window.workflow_preset)
        pipeline.run_finalize()  # Runs all prerequisite reduction stages.
        services.populate_recalculated_cache(
            window.session, window.workflow_preset, pipeline.cache.stages(),
        )
        assert PipelineStage.FINALIZE in pipeline.cache.stages()
        assert not window.session.reduction_cache["finalized_results"].selected_results.empty
        project = tmp_path / "fixture.upbr"
        window.save_project(project)
        assert project.is_file()

        first = snapshot_project(project)
        second = snapshot_project(project)
        assert "finalized_results" in first["reduction_cache"]
        assert PipelineStage.FINALIZE in first["completed_stages"]
        assert compare_snapshots(first, second) == []
        # Actual fit models include local lambda predictors; the complete
        # snapshot must survive pickling as well as an in-memory comparison.
        assert compare_snapshots(first, pickle.loads(pickle.dumps(first))) == []
    finally:
        window.has_unsaved_changes = lambda: False
        window.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_snapshot_converts_objects_without_losing_model_parameters():
    @dataclass(slots=True)
    class Model:
        parameters: np.ndarray
        predictor: object

        def __call__(self, x):
            return self.predictor(x)

    array = np.array([1.0, np.nan])
    frame = pd.DataFrame({"x": array})
    series = frame["x"]
    plain = SimpleNamespace(path=Path("some/file"), _retained=1, __ignored=lambda: None)
    restored = SimpleNamespace(
        session=SimpleNamespace(
            reduction_cache={"model": Model(array, lambda x: x), "plain": plain, "series": series},
            total_data=frame, channels=["x"], experiment=frame,
            qtegra=SimpleNamespace(metadata=frame),
        ),
        completed_stages=("import",), resume_stage="import", workspace={},
    )
    result = snapshot_restored(restored)
    model = result["reduction_cache"]["model"]
    assert model["__type__"].endswith(".<locals>.Model")
    assert model["parameters"] is array
    assert model["predictor"]["__callable__"].endswith(".<lambda>")
    assert result["total_data"] is frame
    assert result["experiment"] is frame
    assert result["qtegra_tables"]["metadata"] is frame
    assert result["reduction_cache"]["series"] is series
    assert result["reduction_cache"]["plain"] == {
        "__type__": "types.SimpleNamespace", "path": "some/file", "_retained": 1,
    }
    assert compare_snapshots(result, pickle.loads(pickle.dumps(result))) == []


@pytest.mark.parametrize("left,right", [
    (1.0, np.nextafter(1.0, 2.0)),
    (np.array([1], dtype="int32"), np.array([1], dtype="int64")),
    (np.array([1.0, np.nan]), np.array([2.0, np.nan])),
    (np.array([1.0]), np.array([[1.0]])),
    ({"a": 1, "b": 2}, {"b": 2, "a": 1}),
    ({"a": 1}, {"b": 1}),
    ([1, 2], [2, 1]),
    ([1], (1,)),
    (pd.DataFrame({"a": [1.0]}), pd.DataFrame({"a": [np.nextafter(1.0, 2.0)]})),
    (pd.DataFrame({"a": [1], "b": [2]}), pd.DataFrame({"b": [2], "a": [1]})),
    (pd.DataFrame({"a": [1, 2]}), pd.DataFrame({"a": [2, 1]})),
    (pd.Series([1.0], name="a"), pd.Series([1.0], name="b")),
])
def test_comparison_reports_exact_difference_paths(left, right):
    differences = compare_snapshots({"nested": [left]}, {"nested": [right]})
    assert differences
    assert all(item.startswith("snapshot['nested'][0]") for item in differences)


def test_comparison_accepts_nans_and_limits_diagnostics():
    value = {"float": float("nan"), "array": np.array([1.0, np.nan]),
             "objects": np.array(["a", float("nan")], dtype=object),
             "strings": np.array(["a", "b"])}
    assert compare_snapshots(value, pickle.loads(pickle.dumps(value))) == []
    assert len(compare_snapshots(list(range(100)), [-1] * 100)) == 50
    frame = pd.DataFrame({"x": [1]})
    other = frame.copy()
    other.attrs["changed"] = True
    assert compare_snapshots(frame, other)[0].startswith("snapshot.attrs")


def test_compare_cli_exit_status_and_output(tmp_path):
    baseline, current = tmp_path / "a.pkl", tmp_path / "b.pkl"
    baseline.write_bytes(pickle.dumps({"x": 1.0}))
    command = [sys.executable, str(Path(__file__).parents[1] / "scripts/snapshot_restore.py"),
               "--compare", str(baseline), str(current)]
    for value, status, message in [(1.0, 0, "identical"), (2.0, 1, "snapshot['x']")]:
        current.write_bytes(pickle.dumps({"x": value}))
        result = subprocess.run(command, capture_output=True, text=True, cwd=tmp_path, timeout=10)
        assert result.returncode == status, result.stderr
        assert message in result.stdout
