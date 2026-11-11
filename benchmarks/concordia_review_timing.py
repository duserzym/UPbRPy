"""Time the Studio concordia review canvases on a saved project.

Usage::

    QT_QPA_PLATFORM=offscreen .venv/bin/python benchmarks/concordia_review_timing.py \
        PROJECT.upbr [--profile OUT.prof] [--json OUT.json]

The project is restored in a separate offscreen Studio window with Qt
settings redirected to a temporary folder, so the user's preferences and any
open Studio window are not touched. Every interaction is timed until the
canvas has been painted into an offscreen image (``QWidget.grab``), which
includes pyqtgraph's scene update and the Qt paint of every item.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("UPBRPY_UPDATE_PREVIEW", "none")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtCore, QtWidgets  # noqa: E402

_settings = tempfile.mkdtemp(prefix="upbrpy-bench-settings-")
QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
for _scope in (QtCore.QSettings.Scope.UserScope, QtCore.QSettings.Scope.SystemScope):
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, _scope, _settings)


def rss_mib() -> float:
    """Return the peak resident set size of this process in MiB (macOS bytes)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def settle(app, canvas=None):
    for _ in range(3):
        app.processEvents()
    if canvas is not None:
        canvas.grab()  # force a full paint of the scene
    app.processEvents()


def timed(app, canvas, action, repeat=1):
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        action()
        settle(app, canvas)
        samples.append((time.perf_counter() - start) * 1000.0)
    return samples


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("project")
    parser.add_argument("--profile")
    parser.add_argument("--json")
    parser.add_argument("--repeat", type=int, default=10)
    args = parser.parse_args(argv)

    from GUI.theme import build_application
    from GUI.main_window import MainWindow
    from GUI.services import ensure_depth_profile

    app = QtWidgets.QApplication.instance() or build_application([], "light")
    window = MainWindow()
    window.resize(1440, 860)
    window.show()
    window.load_project_file(args.project, background=False)
    settle(app)
    window.session.plot_mode = "final_results"
    depth = ensure_depth_profile(window.session, window.workflow_preset)
    window.set_plot_mode("depth_profile", refresh=False)
    window.refresh_view()
    settle(app)
    table = window.results_table
    results = {"sweeps": int(len(depth)),
               "acquisitions": int(depth["spot_name"].nunique()),
               "rss_after_load_mib": rss_mib()}

    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()

    def show_depth_all():
        table.tabs.setCurrentWidget(table.depth_concordia_page)
        table.set_all_spot_choices(True)
        table._depth_sample_choices = True
        table.refresh_depth_concordia()

    canvas = table.depth_concordia
    for diagram in ("wetherill", "tera_wasserburg"):
        key = diagram
        table.depth_diagram_buttons[diagram].setChecked(True)
        out = results.setdefault(key, {})
        out["first_draw_ms"] = timed(app, canvas, show_depth_all)[0]
        out["points_drawn"] = int(len(canvas._point_xy))
        vb = canvas._plot_item.vb

        def pan():
            (x0, x1), (y0, y1) = vb.viewRange()
            dx = (x1 - x0) * 0.02
            vb.setRange(xRange=(x0 + dx, x1 + dx), yRange=(y0, y1), padding=0)

        def zoom():
            vb.scaleBy((0.97, 0.97))

        out["pan_ms"] = timed(app, canvas, pan, args.repeat)
        out["zoom_ms"] = timed(app, canvas, zoom, args.repeat)
        centre = vb.mapViewToScene(QtCore.QPointF(*canvas._point_xy[len(canvas._point_xy) // 2]))
        far = vb.mapViewToScene(QtCore.QPointF(*canvas._point_xy[0]))
        positions = [centre, far] * (args.repeat // 2)
        out["hover_ms"] = [timed(app, canvas, lambda p=p: canvas._on_mouse_moved(p))[0]
                           for p in positions]
        names = sorted(n for n, inc in table.include_flags.items() if inc)
        target = names[len(names) // 2]

        def toggle():
            table.set_spot_included({target}, not table.include_flags.get(target, True))
        out["toggle_ms"] = timed(app, canvas, toggle, args.repeat)

        def change_selection():
            table.set_selected_spots({names[len(names) // 3]})
        out["selection_ms"] = timed(app, canvas, change_selection, 2)

    spot_canvas = table.spot_concordia

    def to_spot():
        table.tabs.setCurrentWidget(table.spot_concordia_page)
        table.refresh_spot_concordia()

    def to_depth():
        table.tabs.setCurrentWidget(table.depth_concordia_page)
        table.refresh_depth_concordia(reset_view=False)

    results["switch_to_spot_ms"] = timed(app, spot_canvas, to_spot, 3)
    results["switch_to_depth_ms"] = timed(app, canvas, to_depth, 3)
    results["rss_peak_mib"] = rss_mib()
    if profiler:
        profiler.disable()
        profiler.dump_stats(args.profile)

    def summary(values):
        return f"median {statistics.median(values):7.1f} ms  max {max(values):7.1f} ms"

    print(f"{results['acquisitions']} acquisitions, {results['sweeps']} sweeps")
    for diagram in ("wetherill", "tera_wasserburg"):
        out = results[diagram]
        print(f"[{diagram}] first draw {out['first_draw_ms']:.0f} ms, points {out['points_drawn']}")
        for name in ("pan_ms", "zoom_ms", "hover_ms", "toggle_ms", "selection_ms"):
            print(f"  {name[:-3]:10s} {summary(out[name])}")
    print(f"switch to spot  {summary(results['switch_to_spot_ms'])}")
    print(f"switch to depth {summary(results['switch_to_depth_ms'])}")
    print(f"peak RSS {results['rss_peak_mib']:.0f} MiB (after load {results['rss_after_load_mib']:.0f})")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))
    window.close()


if __name__ == "__main__":
    main()
