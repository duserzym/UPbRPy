"""Isolated Studio latency and offscreen paint benchmark.

Run with .venv/bin/python -B benchmarks/studio_responsiveness.py --project FILE
--code-root CHECKOUT --profile /tmp/profiles --json /tmp/result.json.
Each invocation uses a fresh copy and cold reduction cache. Repeat invocations
and compare medians on loaded machines. Profiles cover only the main thread;
worker contention appears in heartbeat gaps, not in cProfile worker stacks.
"""
from __future__ import annotations

import argparse
import cProfile
import importlib.abc
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time
import threading

sys.dont_write_bytecode = True


class MainThreadProfile(cProfile.Profile):
    """Exclude Qt worker callbacks from Python 3.14's monitoring profiler."""

    def __init__(self):
        super().__init__()
        self._profile_thread = threading.get_ident()


def _main_thread_callback(name):
    original = getattr(cProfile.Profile, name)
    def callback(self, *args):
        if threading.get_ident() == self._profile_thread:
            return original(self, *args)
    return callback


# Earlier Python versions use thread-local profiling already. Python 3.14
# exposes monitoring callbacks and otherwise records native Qt worker entries.
for _callback_name in ("_pystart_callback", "_pythrow_callback", "_pyreturn_callback",
                       "_ccall_callback", "_creturn_callback"):
    if hasattr(cProfile.Profile, _callback_name):
        setattr(MainThreadProfile, _callback_name, _main_thread_callback(_callback_name))


def isolate(folder):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["UPBRPY_BLOCK_IMPORTS"] = "requests,keyring"
    os.environ["UPBRPY_RESULTS_CACHE"] = "0"
    os.environ["UPBRPY_REDUCE_MOTION"] = "0"
    os.environ["XDG_CONFIG_HOME"] = str(folder / "config")
    os.environ["UPB_STANDARDS_DIR"] = str(folder / "absent-standards")
    for key in ("RESULTS_CACHE", "AUTOSAVE", "LAB_INBOX"):
        os.environ[f"UPBRPY_{key}_DIR"] = str(folder / key.lower())

    class Blocked(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in {"requests", "keyring"}:
                raise ModuleNotFoundError("Benchmark blocks network dependencies", name=fullname)

    sys.meta_path.insert(0, Blocked())

    def no_network(*args, **kwargs):
        raise RuntimeError("Network disabled by responsiveness benchmark")

    socket.create_connection = socket.getaddrinfo = no_network
    socket.socket.connect = socket.socket.connect_ex = no_network
    bootstrap = folder / "python-startup"
    bootstrap.mkdir(exist_ok=True)
    (bootstrap / "sitecustomize.py").write_text(
        "import importlib.abc, socket, sys\n"
        "sys.dont_write_bytecode = True\n"
        "class Blocked(importlib.abc.MetaPathFinder):\n"
        " def find_spec(self, fullname, path=None, target=None):\n"
        "  if fullname.split('.')[0] in {'requests', 'keyring'}:\n"
        "   raise ModuleNotFoundError('Benchmark blocks network dependencies', name=fullname)\n"
        "sys.meta_path.insert(0, Blocked())\n"
        "def blocked(*args, **kwargs):\n"
        " raise RuntimeError('Network disabled by responsiveness benchmark')\n"
        "socket.create_connection = socket.getaddrinfo = blocked\n"
        "socket.socket.connect = socket.socket.connect_ex = blocked\n")
    os.environ["PYTHONPATH"] = str(bootstrap) + os.pathsep + os.environ.get("PYTHONPATH", "")
    from PySide6 import QtCore, QtGui
    original = QtCore.QSettings
    original.setDefaultFormat(original.Format.IniFormat)
    for scope in (original.Scope.UserScope, original.Scope.SystemScope):
        original.setPath(original.Format.IniFormat, scope, str(folder / "settings"))

    class Settings(original):
        def __init__(self, *args, **kwargs):
            if args and isinstance(args[0], str) and (len(args) < 2 or isinstance(args[1], str)):
                super().__init__(self.Format.IniFormat, self.Scope.UserScope, *args, **kwargs)
            else:
                super().__init__(*args, **kwargs)

    QtCore.QSettings = Settings
    QtGui.QDesktopServices.openUrl = no_network


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--code-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--profile", type=Path, help="Directory for per-phase .prof files")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--paint", action="store_true", help="Also paint 500 and 5000 synthetic ellipses")
    parser.add_argument("--frames", type=int, default=20)
    parser.add_argument("--fit-resamples", type=int, default=100, help="Bootstrap draws for each fit (default: 100, same on both versions)")
    parser.add_argument("--paint-only", action="store_true", help="Skip the reduction session")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.code_root.resolve()))
    with tempfile.TemporaryDirectory(prefix="upbrpy-responsiveness-") as temporary:
        folder = Path(temporary)
        isolate(folder)
        import numpy as np
        import pandas as pd
        from PySide6 import QtCore, QtGui, QtTest, QtWidgets
        from GUI.theme import build_application, THEMES
        from GUI.main_window import MainWindow
        from GUI.updater import Updater
        from GUI.widgets import ConcordiaReviewWidget
        Updater.start_scheduler = lambda self: None
        app = build_application([], "light")
        app.setQuitOnLastWindowClosed(False)
        errors = []
        QtWidgets.QMessageBox.critical = lambda *a, **k: errors.append(str(a[2:]))
        window = MainWindow(theme_mode="light")
        window.resize(1440, 900)
        window.show()
        # Fail instead of leaving a recoverable stage error silently unmeasured.
        window._handle_action_error = lambda title, exc: errors.append(f"{title}: {exc}")
        if hasattr(window, "_show_reduction_issue"):
            window._show_reduction_issue = lambda title, exc, *a: errors.append(f"{title}: {exc}")
        rows, paint_rows = [], []
        heartbeat, frames = [], []
        last = [time.perf_counter(), time.perf_counter()]

        def tick(target, index):
            now = time.perf_counter()
            target.append((now-last[index])*1000)
            last[index] = now

        pulse, frame_timer = QtCore.QTimer(), QtCore.QTimer()
        for timer, interval, target, index in ((pulse, 1, heartbeat, 0), (frame_timer, 16, frames, 1)):
            timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
            timer.timeout.connect(lambda t=target, i=index: tick(t, i))
            timer.start(interval)

        def busy():
            timers = [getattr(window, name, None) for name in
                      ("_selection_edit_timer", "_selection_recompute_timer", "_selection_autosave_timer")]
            timers.append(getattr(window.results_table, "_column_resize_timer", None))
            for panel in getattr(window.results_table, "fit_panels", {}).values():
                if panel._workers or panel._timer.isActive():
                    return True
            return (window._active_worker is not None or window.thread_pool.activeThreadCount()
                    or any(t is not None and t.isActive() for t in timers))

        def settle():
            deadline, stable = time.monotonic()+600, [0]
            loop = QtCore.QEventLoop()
            check = QtCore.QTimer()
            check.setInterval(20)
            failed = []
            def poll():
                stable[0] = 0 if busy() else stable[0]+1
                if errors or time.monotonic() > deadline:
                    failed.extend(errors or ["Studio did not become idle"])
                    loop.quit()
                elif stable[0] >= 8:
                    loop.quit()
            check.timeout.connect(poll)
            check.start()
            loop.exec()
            check.stop()
            if failed:
                raise RuntimeError("; ".join(failed))

        def measure(name, action, supported=True):
            if not supported:
                rows.append(dict(phase=name, skipped="API unavailable"))
                print(f"{name:28} skipped: API unavailable", flush=True)
                return
            heartbeat.clear(); frames.clear()
            last[:] = [time.perf_counter()]*2
            profile = MainThreadProfile() if args.profile else None
            start = time.perf_counter()
            if profile: profile.enable()
            action()
            settle()
            # A final heartbeat captures synchronous work at the end as well.
            app.processEvents()
            if profile:
                profile.disable()
                args.profile.mkdir(parents=True, exist_ok=True)
                profile.dump_stats(str(args.profile / (name+".prof")))
            row = dict(phase=name, wall_ms=(time.perf_counter()-start)*1000,
                       long_50=sum(g > 50 for g in heartbeat), long_100=sum(g > 100 for g in heartbeat),
                       max_gap_ms=max(heartbeat, default=0), p95_frame_ms=float(np.percentile(frames, 95)) if frames else None)
            rows.append(row)
            print(f"{name:28} {row['wall_ms']:10.1f} {row['long_50']:5} {row['long_100']:5} {row['max_gap_ms']:9.1f} {row['p95_frame_ms'] or 0:9.1f}", flush=True)

        print("phase                           wall ms   >50  >100   max gap frame p95", flush=True)
        try:
            if not args.paint_only:
                if args.project:
                    copy = (folder / "project").resolve()
                    shutil.copytree(args.project.resolve().parent, copy)
                    project = copy / args.project.name
                    # Older readers know the original spelling only. This alias is
                    # metadata, not measurement data; normalize it in BOTH copies.
                    manifest = copy / "session_manifest.toml"
                    if manifest.exists():
                        manifest.write_text(manifest.read_text().replace(
                            'format = "laicpmsdb-session-bundle"', 'format = "upbdb-session-bundle"'))
                    # Only the copy is rewritten: force absolute fallbacks into the copy.
                    data = json.loads(project.read_text())
                    for path in data.get("paths", {}).values():
                        if isinstance(path, dict) and path.get("relative"):
                            resolved = (copy / path["relative"]).resolve()
                            if not resolved.is_relative_to(copy):
                                raise ValueError("Project references data outside its folder; copy it into the project first")
                            path["absolute"] = str(resolved)
                        elif isinstance(path, dict) and path.get("absolute"):
                            original = Path(path["absolute"]).resolve()
                            try:
                                relative = original.relative_to(args.project.resolve().parent)
                            except ValueError:
                                raise ValueError("Project references a file outside its folder; benchmark requires a self-contained copy") from None
                            path["absolute"] = str(copy / relative)
                    project.write_text(json.dumps(data))
                    measure("open", lambda: window.load_project_file(project, background=True))
                else:
                    source = args.code_root / "tests/fixtures/qtegra/Zircon_demo"
                    copy = folder / "fixture"
                    shutil.copytree(source, copy)
                    project = folder / "fixture.upbr"
                    measure("open", lambda: window.load_experiment_directory(str(copy), background=True, offer_restore=False))
                for name in ("baseline", "internal_normalization", "concentration", "raw_ratio", "downhole", "session_correction", "final"):
                    method = "run_concentration_stage" if name == "concentration" else "run_final_review" if name == "final" else f"run_{name}_preview"
                    measure(name, getattr(window, method))
                for index in range(window.workflow_step_list.count()):
                    measure(f"stage_{index}", lambda i=index: window.workflow_step_list.setCurrentRow(i))
                window.set_plot_mode("final_results", refresh=True)
                table = window.results_table
                measure("spot_page", lambda: table.tabs.setCurrentWidget(table.spot_concordia_page))
                for fit_panel in getattr(table, "fit_panels", {}).values():
                    fit_panel.fit_button.setChecked(False)
                settle()
                canvas = table.spot_concordia
                names = list(dict.fromkeys(p[0] for p in canvas._points))
                if not names:
                    raise RuntimeError("No plotted spots to exercise")
                def select(count, modifier):
                    for name in names[:count]:
                        p = next((p for p in canvas._points if p[0] == name), None)
                        if p:
                            canvas._select_at(canvas._plot_item.vb.mapViewToScene(QtCore.QPointF(p[1], p[2])), modifier)
                measure("select_one", lambda: select(1, QtCore.Qt.KeyboardModifier.NoModifier))
                measure("shift_select_10", lambda: select(10, QtCore.Qt.KeyboardModifier.ShiftModifier))
                measure("box_select", lambda: canvas.select_rectangle(canvas._plot_item.vb.viewRect()))
                measure("hide", lambda: QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_H), hasattr(table, "_hide_action_text"))
                measure("unhide", lambda: QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_H), hasattr(table, "_hide_action_text"))
                chips = getattr(table, "spot_sample_chips", None)
                buttons = [chip for chip in getattr(chips, "chips", {}).values()
                           if not chip.isHidden()]
                for state in ("off", "on"):
                    measure("sample_"+state, lambda: QtTest.QTest.mouseClick(
                        buttons[0], QtCore.Qt.MouseButton.LeftButton,
                        QtCore.Qt.KeyboardModifier.ControlModifier), bool(buttons))
                for diagram in ("tera_wasserburg", "wetherill"):
                    measure(diagram, lambda d=diagram: table.spot_diagram_buttons[d].click())
                measure("colour", lambda: table.set_spot_colour_mode("spot"), hasattr(table, "set_spot_colour_mode"))
                for visible in (False, True):
                    measure("grid_"+str(visible), lambda v=visible: table.set_concordia_grid(v), hasattr(table, "set_concordia_grid"))
                item = window._spot_tree_item(names[0])
                for checked in (False, True):
                    measure("tree_"+str(checked), lambda c=checked: item.setCheckState(0, QtCore.Qt.CheckState.Checked if c else QtCore.Qt.CheckState.Unchecked),
                            bool(item.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable))
                table.set_selected_spots(set(names[:10]))
                panel = getattr(table, "fit_panels", {}).get("spot")
                for model in (1, 3):
                    def fit(m=model):
                        panel.together.setChecked(True)
                        index = panel.model_combo.findData(m)
                        panel.model_combo.setCurrentIndex(index if index >= 0 else m-1)
                        panel.resamples.setValue(args.fit_resamples)
                        panel.fit_button.setChecked(True)
                    measure(f"fit_{model}", fit, panel is not None)
                    if panel is not None:
                        rows[-1].update(records=len(panel.records), fit_errors=list(getattr(panel, "_errors", ())), resamples=args.fit_resamples)
                        if not panel.records:
                            raise RuntimeError(f"{model=}: {panel.readout.toPlainText()}")
                if panel: panel.fit_button.setChecked(False)
                def table_check():
                    model = table.results_model
                    model.setData(model.index(0, 0), QtCore.Qt.CheckState.Unchecked, QtCore.Qt.ItemDataRole.CheckStateRole)
                measure("table_inclusion", table_check)
                measure("depth_reduce", window.run_depth_profile_review)
                measure("depth_page", lambda: table.tabs.setCurrentWidget(table.depth_concordia_page))
                measure("depth_focus", lambda: table.depth_spot_combo.setCurrentIndex(min(1, table.depth_spot_combo.count()-1)))
                measure("save", lambda: window.save_project(project))
                if args.json:
                    args.json.write_text(json.dumps(dict(code_root=str(args.code_root), project=str(args.project), phases=rows, paint=[]), indent=2))
            if args.paint or args.paint_only:
                from UPbRPy.isochron import wetherill_point, tera_wasserburg_point
                for count in (500, 5000):
                    rng = np.random.default_rng(419)
                    ages = rng.uniform(100, 1600, count)
                    frame = pd.DataFrame({"spot_name": [f"s{i}" for i in range(count)], "sample": "synthetic"})
                    for ratio, values in zip(("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb"), (*wetherill_point(ages), *tera_wasserburg_point(ages))):
                        frame[ratio] = values
                        frame[ratio+"_2e"] = values*.08
                    view = ConcordiaReviewWidget(); view.resize(1440, 900); view.show()
                    view.show_data(frame, diagram="wetherill", kind="spot", theme=THEMES["light"], include_calibration_uncertainty=True)
                    settle()
                    image = QtGui.QImage(2880, 1800, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
                    image.setDevicePixelRatio(2)
                    samples = []
                    for i in range(args.frames+3):
                        start = time.perf_counter()
                        view._plot_item.vb.scaleBy((.99, .99))
                        view._plot_item.vb.translateBy(x=.001)
                        app.processEvents()
                        painter = QtGui.QPainter(image); view.render(painter, QtCore.QRectF(0, 0, 1440, 900)); painter.end()
                        if i >= 3: samples.append((time.perf_counter()-start)*1000)
                    paint_rows.append(dict(ellipses=count, median_ms=float(np.median(samples)), p95_ms=float(np.percentile(samples, 95))))
                    print("paint", paint_rows[-1], flush=True)
                    view.close(); view.deleteLater()
            output = dict(code_root=str(args.code_root), project=str(args.project), phases=rows, paint=paint_rows,
                          scenario_version=2,
                          quiet_tail_ms=160, note="Wall times include a 160 ms idle confirmation tail")
            if args.json: args.json.write_text(json.dumps(output, indent=2))
        finally:
            pulse.stop(); frame_timer.stop()
            window.has_unsaved_changes = lambda: False
            window.close()
            window.thread_pool.waitForDone()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
