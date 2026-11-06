"""Compact Studio per-system reference-material summary, tested offscreen."""

import pytest


def test_reference_summary_shows_two_accepted_systems(monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets
    from GUI import services
    from GUI.theme import build_application
    from GUI.widgets import ResultsTableWidget
    from GUI.workflows import get_workflow_preset
    from test_export_table import synthetic_pipeline

    app = QtWidgets.QApplication.instance() or build_application([], theme_mode="light")
    pipeline = synthetic_pipeline()
    from UPbRPy.export_table import summarize_reference_materials_by_system
    finalized = pipeline.run_finalize()
    finalized.reference_material_summary_by_system = summarize_reference_materials_by_system(
        finalized.parallel_results, pipeline.standard_archive, ("S513", "91500"))
    # Exercise the service adapter without importing a second data session.
    monkeypatch.setattr(services, "ensure_shared_pipeline", lambda *args: pipeline)
    monkeypatch.setattr(services, "ensure_spot_means", lambda *args: None)
    session = type("Session", (), {"reduction_cache": {}})()
    assert services.ensure_finalized_results(session, get_workflow_preset("zircon")) is finalized
    widget = ResultsTableWidget()
    try:
        widget.resize(1000, 650)
        widget.show_results(finalized)
        widget.tabs.setCurrentWidget(widget.secondary_page)
        widget.show()
        app.processEvents()
        frame = widget.secondary_model.frame
        assert list(frame) == ["sample", "n", "6/38 WM ±2s", "6/38 accepted", "6/38 offset %",
                               "7/6 WM ±2s", "7/6 accepted", "7/6 offset %"]
        row = frame.set_index("sample").loc["91500"]
        assert row["6/38 accepted"] == "1062.4"
        assert row["7/6 accepted"] == "1065.7"
        assert "±" in row["6/38 WM ±2s"] and "±" in row["7/6 WM ±2s"]
        assert widget.secondary_table.horizontalHeader().length() < 1000
        assert widget.minimumSizeHint().width() <= 1280
        text = widget._secondary_summary(frame.iloc[0])
        assert "6/38" in text and "7/6" in text and "accepted" in text
        # The legacy result table remains available with its original schema.
        assert finalized.secondary_standard_summary.empty
    finally:
        widget.close()
        widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
