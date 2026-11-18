"""Operation-count guards for retained review updates and smooth geometry."""
from types import SimpleNamespace
import re

import numpy as np
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtWidgets

from GUI.theme import THEMES, build_application
from GUI.widgets import ConcordiaReviewWidget, ResultsTableWidget
from GUI.review_fits import _export_svg
from test_concordia_render_parity import synthetic_frame, dispose


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.mark.parametrize("diagram", ["wetherill", "tera_wasserburg"])
def test_analytic_ellipse_picking_and_bezier_export(app, tmp_path, diagram):
    canvas = ConcordiaReviewWidget()
    frame = synthetic_frame(100, kind="spot")
    try:
        canvas.resize(1000, 650)
        canvas.show_data(frame, diagram=diagram, kind="spot", theme=THEMES["light"],
                         include_calibration_uncertainty=True)
        canvas.show()
        app.processEvents()
        names, boxes, areas, handles = canvas._ellipse_hit_data()
        assert len(handles) > 80
        assert all(handle.path.elementCount() == 13 for handle in handles)
        assert all(handle.path.elementAt(1).isCurveTo() for handle in handles)
        assert len(canvas._ellipse_batch.paths) <= 64
        rng = np.random.default_rng(42)
        for handle in handles[:12]:
            theta = np.linspace(0, 2*np.pi, 4097)
            xy = handle.center[:, None] + handle.matrix @ np.array([np.cos(theta), np.sin(theta)])
            polygon = QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in xy.T])
            for _ in range(10):
                # Stay off the boundary, where any polygon differs from a conic.
                direction = rng.normal(size=2)
                direction *= rng.choice([.3, .8, 1.2]) / np.linalg.norm(direction)
                point = handle.center + handle.matrix @ direction
                index = handles.index(handle)
                assert bool(canvas._ellipse_contains(point)[index]) == polygon.containsPoint(
                    QtCore.QPointF(*point), QtCore.Qt.FillRule.OddEvenFill)
            box = handle.path.boundingRect()
            assert canvas._ellipse_intersects_rectangle(box)[handles.index(handle)]
            far = QtCore.QRectF(box.right()+box.width(), box.bottom()+box.height(), box.width(), box.height())
            assert not canvas._ellipse_intersects_rectangle(far)[handles.index(handle)]
        batch, scatter = canvas._ellipse_batch, canvas._scatter
        canvas.select_spots({names[0]})
        assert canvas._ellipse_batch is batch and canvas._scatter is scatter
        assert canvas._highlight.path().elementAt(1).isCurveTo()
        path = tmp_path / "smooth.svg"
        _export_svg(canvas.scene(), path)
        assert re.search(r"[Cc]", " ".join(re.findall(r'\bd="([^"]+)"', path.read_text())))
    finally:
        dispose(canvas, app)


def test_single_review_edit_touches_only_its_rows(app, monkeypatch):
    widget = ResultsTableWidget()
    frame = synthetic_frame(5000, kind="spot")
    frame["spot_name"] = [f"s{i}" for i in range(len(frame))]
    try:
        widget.apply_theme(THEMES["light"])
        widget.show_results(SimpleNamespace(parallel_results=frame, selected_results=frame))
        # Building a hidden tree while its signals are blocked loses insert
        # notifications. It also turns a one-row edit into thousands of items.
        assert widget.spot_choice_tree in widget._pending_group_views
        assert widget.spot_choice_group_model.rowCount(QtCore.QModelIndex()) == 0
        changes = []
        widget.results_model.dataChanged.connect(lambda first, last, roles:
                                                 changes.append((first.row(), last.row())))
        monkeypatch.setattr(widget.results_model, "set_frame", lambda *a: pytest.fail("model reset"))
        widget.set_spot_excluded({"s2500"}, True)
        assert changes == [(2500, 2500)]
        assert widget.spot_choice_tree in widget._pending_group_views
        assert len(widget.spot_concordia._metadata_by_spot.rows) <= 1
        changes.clear()
        widget.set_spot_included({"s2500"}, False)
        assert changes == [(2500, 2500)]
        assert not widget._choice_items["s2500"].font().bold()
    finally:
        dispose(widget, app)


def test_hidden_concordia_requests_are_coalesced(app, monkeypatch):
    widget = ResultsTableWidget()
    frame = synthetic_frame(100, kind="spot")
    try:
        widget.apply_theme(THEMES["light"])
        widget.show_results(SimpleNamespace(parallel_results=frame, selected_results=frame))
        widget.show()
        app.processEvents()
        widget.tabs.setCurrentWidget(widget.results_page)
        calls = []
        original = widget.spot_concordia.show_data
        monkeypatch.setattr(widget.spot_concordia, "show_data", lambda *a, **k:
                            (calls.append(1), original(*a, **k)))
        for _ in range(10):
            widget.refresh_spot_concordia()
        app.processEvents()
        assert calls == []
        widget.tabs.setCurrentWidget(widget.spot_concordia_page)
        app.processEvents()
        assert calls == [1]
    finally:
        dispose(widget, app)


def test_selection_dirty_hash_tracks_in_place_edits_without_records(monkeypatch):
    from GUI.main_window import MainWindow
    frame = synthetic_frame(5000, kind="spot")[["spot_name"]].rename(columns={"spot_name": "spot_names"}).reset_index(drop=True)
    frame["baseline_start_times"] = np.arange(len(frame), dtype=float)
    frame["selection_method"] = "threshold"
    monkeypatch.setattr(frame, "to_dict", lambda *a, **k: pytest.fail("per-row records"))
    first = MainWindow._selection_fingerprint(frame)
    frame.loc[2500, "baseline_start_times"] += 1
    assert MainWindow._selection_fingerprint(frame) != first
    frame.loc[2500, "baseline_start_times"] -= 1
    assert MainWindow._selection_fingerprint(frame) == first


def test_adaptive_curve_bounds_screen_chords():
    from GUI.concordia_geometry import sample_curve
    view = SimpleNamespace(viewRange=lambda: [[0, 1], [0, 1]],
                           width=lambda: 1440, height=lambda: 900)
    x, y = sample_curve(lambda t: (t, t*t), 0, 1, view, min_points=1200)
    assert len(x) >= 1200
    assert np.max(np.hypot(np.diff(x)*1440, np.diff(y)*900)) <= 2
    x, y = sample_curve(lambda t: (t, t*t), 0, 1, view, max_points=65)
    assert len(x) == 65
