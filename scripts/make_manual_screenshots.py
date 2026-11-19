"""Capture Studio's manual using real measurements and neutral display labels.

Run with the normal Qt platform for display-resolution images, or set
QT_QPA_PLATFORM=offscreen. Only QWidget.grab() is used. The source is read
only; numeric cells and metadata are preserved in a temporary copy. Metadata
panels and paths are not displayed. No source names are logged or retained.
"""

from __future__ import annotations

import argparse
import csv
import importlib.abc
import io
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEFAULT_SOURCE = ROOT / "tests/fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"
DEFAULT_OUT = ROOT / "docs/assets/screenshots"


def alphabetic_number(index: int) -> str:
    """Return A through Z, then AA, AB, and so on, for a zero-based index."""
    result = ""
    while index >= 0:
        index, remainder = divmod(index, 26)
        result = chr(65 + remainder) + result
        index -= 1
    return result


def label_replacements(labels: list[str]) -> dict[str, str]:
    """Preserve library references and replace other prefixes in source order."""
    from UPbRPy.standards import load_standard_measurements

    library = load_standard_measurements()
    references = set(library["standard"].astype(str))
    for aliases in library["aliases"].dropna():
        references.update(str(aliases).split(";"))
    normalize = lambda value: "".join(c for c in value.casefold() if c.isalnum())
    references = {normalize(value) for value in references if value}
    prefixes: dict[str, str] = {}
    replacements = {}
    for label in labels:
        match = re.fullmatch(r"(.*?)([_ ](?:S[_ ])?\d+)", label)
        prefix, suffix = match.groups() if match else (label, "")
        if normalize(prefix) in references:
            replacements[label] = label
        else:
            if prefix not in prefixes:
                prefixes[prefix] = f"Sample {alphabetic_number(len(prefixes))}"
            replacements[label] = prefixes[prefix] + suffix
    if len(set(replacements.values())) != len(replacements):
        raise ValueError("Neutral labels would not be unique")
    return replacements


def copy_neutral_source(source: Path, destination: Path) -> Path:
    """Copy Rich and optional companion files, changing label cells only.

    Unchanged lines remain byte-identical, including the BOM and line endings.
    Companion file names are neutral, independently of their original names.
    Original labels exist only in memory during this function.
    """
    files = sorted(source.rglob("*.csv")) if source.is_dir() else [source]
    rich_files = []
    for candidate in files:
        with candidate.open("r", encoding="utf-8-sig", newline="") as stream:
            first = next(csv.reader(stream), [])
        if len(first) > 4 and first[:4] == ["", "", "", ""]:
            rich_files.append(candidate)
    if len(rich_files) != 1:
        raise ValueError("Provide exactly one Rich CSV, or a folder containing one")
    rich = rich_files[0]
    with rich.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = csv.reader(stream)
        next(rows)
        labels = next(rows)[4:]
    if labels and not labels[-1]:
        labels.pop()
    replacements = label_replacements(labels)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "Zircon_demo_Rich.csv"
    # Preserve all cell values outside labels, even on rewritten rows. The
    # csv writer is used only where a label changed (including quoted cells).
    with rich.open("r", encoding="utf-8", newline="") as src, target.open(
        "w", encoding="utf-8", newline=""
    ) as dst:
        for line in src:
            bom = "\ufeff" if line.startswith("\ufeff") else ""
            body = line.removeprefix(bom)
            row = next(csv.reader([body]))
            rewritten = [replacements.get(cell, cell) for cell in row]
            if rewritten == row:
                dst.write(line)
            else:
                ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                buffer = io.StringIO(newline="")
                csv.writer(buffer, lineterminator=ending).writerow(rewritten)
                dst.write(bom + buffer.getvalue())
    if source.is_dir():
        number = 0
        for candidate in files:
            if candidate == rich:
                continue
            with candidate.open("rb") as stream:
                first = stream.readline()
                match = re.match(rb"(?:\xef\xbb\xbf)?(.+):\d{2}/\d{2}/\d{4} ", first)
                if not match:
                    continue
                label = match[1].decode("utf-8")
                if label not in replacements:
                    raise ValueError("Companion acquisition is absent from the Rich CSV")
                folder = destination / "zircon_demo"
                folder.mkdir(exist_ok=True)
                number += 1
                with (folder / f"Zircon_demo_{number:04d}.csv").open("wb") as dst:
                    dst.write(first[:match.start(1)] + replacements[label].encode("utf-8") + first[match.end(1):])
                    # Per-spot files carry the label on their first line only.
                    import shutil
                    shutil.copyfileobj(stream, dst)
    return destination if source.is_dir() else target


class BlockedImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in {"requests", "keyring"}:
            raise ModuleNotFoundError("Optional network dependency disabled", name=fullname)


def isolate(folder: Path) -> None:
    """Install process-wide isolation before importing any Studio modules."""
    os.environ["UPBRPY_BLOCK_IMPORTS"] = "requests,keyring"
    os.environ["UPBRPY_RESULTS_CACHE"] = "0"
    os.environ.pop("UPBRPY_UPDATE_PREVIEW", None)
    for variable, child in (
        ("UPBRPY_RESULTS_CACHE_DIR", "cache"),
        ("UPBRPY_AUTOSAVE_DIR", "autosave"),
        ("UPBRPY_LAB_INBOX_DIR", "inbox"),
        ("UPB_STANDARDS_DIR", "absent-standards"),
    ):
        os.environ[variable] = str(folder / child)
    for name in list(sys.modules):
        if name.split(".", 1)[0] in {"requests", "keyring"}:
            del sys.modules[name]
    sys.meta_path.insert(0, BlockedImports())

    def no_network(*args, **kwargs):
        raise RuntimeError("Network disabled for manual screenshots")

    socket.create_connection = no_network
    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    socket.getaddrinfo = no_network
    from PySide6 import QtCore, QtGui

    original = QtCore.QSettings
    original.setDefaultFormat(original.Format.IniFormat)
    for scope in (original.Scope.UserScope, original.Scope.SystemScope):
        original.setPath(original.Format.IniFormat, scope, str(folder / "settings"))

    class IsolatedSettings(original):
        def __init__(self, *args, **kwargs):
            if args and isinstance(args[0], str) and (len(args) < 2 or isinstance(args[1], str)):
                super().__init__(self.Format.IniFormat, self.Scope.UserScope, *args, **kwargs)
            else:
                super().__init__(*args, **kwargs)

    QtCore.QSettings = IsolatedSettings
    QtGui.QDesktopServices.openUrl = no_network


def capture_manual(source: Path, out: Path, scale: float | None = None) -> list[Path]:
    """Drive Studio's zircon workflow and capture each completed view."""
    if scale is not None:
        os.environ["QT_SCALE_FACTOR"] = str(scale)
    with tempfile.TemporaryDirectory(prefix="upbrpy-manual-") as temporary:
        folder = Path(temporary)
        isolate(folder)
        # Give the offscreen plugin a desktop large enough for the two logical
        # window sizes. Do not change the default macOS platform.
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            config = folder / "screen.json"
            factor = float(os.environ.get("QT_SCALE_FACTOR", "1"))
            config.write_text(json.dumps({"screens": [{"width": round(1800 * factor), "height": round(1100 * factor)}]}))
            os.environ["QT_QPA_PLATFORM"] = f"offscreen:configfile={config}"
        neutral = copy_neutral_source(source, folder / "Zircon_demo")
        from PySide6 import QtCore, QtGui, QtTest, QtWidgets
        from GUI.theme import build_application
        from GUI.main_window import MainWindow
        from GUI.studio_window import StudioWindow
        from GUI.updater import Updater
        from GUI.update_widgets import UpdatesDialog
        from GUI.database import DatabaseLinks, SignInDialog
        from GUI.export_dialog import ExportResultsDialog
        from UPbRPy.export_table import build_export_table

        app = build_application([], "light")
        app.setQuitOnLastWindowClosed(False)
        updater = Updater()
        updater.start_scheduler = lambda: None
        updater._reason = updater._support_reason()
        updater._emit(("unsupported", updater._reason))
        shell = StudioWindow(theme_mode="light", reduction_factory=lambda: MainWindow(theme_mode="light", updater=updater))
        window = shell.active_reduction
        errors = []
        # Never enter a modal error loop offscreen; details may contain source
        # metadata, so fail with a neutral message instead of printing them.
        QtWidgets.QMessageBox.critical = lambda *args, **kwargs: errors.append(True)
        output = []
        unavailable = []
        out.mkdir(parents=True, exist_ok=True)

        def settle():
            deadline = time.monotonic() + 180
            stable = 0
            while stable < 5:
                QtTest.QTest.qWait(50)
                timers = (window._selection_edit_timer, window._selection_recompute_timer,
                          window._selection_autosave_timer, window.results_table._readout_timer)
                busy = window._active_worker is not None or window.thread_pool.activeThreadCount() or any(t.isActive() for t in timers)
                stable = 0 if busy else stable + 1
                if errors:
                    raise RuntimeError("A Studio stage failed; screenshot run stopped")
                if time.monotonic() > deadline:
                    raise RuntimeError("Studio did not finish its background work")

        def grab(name, dialog=None):
            settle()
            window.set_notes_log_visible(False)
            QtWidgets.QToolTip.hideText()
            if dialog:
                dialog.move(shell.mapToGlobal(shell.rect().center()) - dialog.rect().center())
                settle()
            image = shell.grab()
            if dialog:
                # Native dialogs are separate widgets. Compose their own grab
                # over the window grab; never read pixels from the desktop.
                painter = QtGui.QPainter(image)
                painter.drawPixmap(shell.mapFromGlobal(dialog.pos()), dialog.grab())
                painter.end()
            path = out / f"{name}.png"
            if not image.save(str(path), "PNG"):
                raise RuntimeError("Could not save screenshot")
            # Lossless first; quantize only when needed for high-DPI captures.
            from PIL import Image
            with Image.open(path) as png:
                png.save(path, optimize=True)
            if path.stat().st_size > 400_000:
                with Image.open(path) as png:
                    full_color = png.convert("RGB")
                for colors in (256, 128, 96, 64, 32):
                    full_color.quantize(colors=colors).save(path, optimize=True)
                    if path.stat().st_size <= 400_000:
                        break
            if path.stat().st_size > 410_000:
                raise RuntimeError("Screenshot exceeds the image size budget")
            output.append(path)
            print(path.name, flush=True)

        try:
            shell.resize(1440, 900)
            shell.show()
            grab("00-overview")
            shell.resize(1280, 800)
            grab("00-overview-1280x800")
            shell.resize(1440, 900)
            window.load_experiment_directory(str(neutral), background=False, offer_restore=False)
            settle()
            # Start on a zircon reference, with representative matrix/Pb/U traces.
            spot = next((s for s in window.session.spot_names if s.startswith("S513_")), window.session.spot_names[0])
            window.show_spot_signal(spot)
            grab("01-import-and-auto-pick-windows")
            window.workflow_step_list.setCurrentRow(1)
            window.set_active_edit_window("baseline")
            window.nudge_selection_edge("start", 1)
            window.set_active_edit_window("plateau")
            window.nudge_selection_edge("end", -1)
            regions = window._selection_regions(spot)
            times = window.session.total_data.loc[
                window.session.total_data["spot_name"].eq(spot), "Time"
            ]
            previous = times[times < regions["signal"][1]]
            if len(previous):
                window.on_plot_region_changed("signal", (regions["signal"][0], float(previous.iloc[-1])))
            grab("02-review-baseline-and-plateau-picks")
            actions = (
                (2, "03-subtract-baseline", window.run_baseline_preview),
                (3, "04-internal-normalization", window.run_internal_normalization_preview),
                (4, "05-calibrate-and-calculate-concentrations", window.run_concentration_stage),
                (5, "06-build-raw-isotope-ratios", window.run_raw_ratio_preview),
                (6, "07-downhole-correction", window.run_downhole_preview),
                (7, "08-session-fractionation-correction", window.run_session_correction_preview),
                (8, "09-dates-and-concordia-review", window.run_final_review),
            )
            # The small reader fixture intentionally retains only nine
            # channels. Studio's current raw-ratio implementation requires
            # both Hg channels. Do not pad the measurements or substitute
            # invented outputs to make a screenshot of an unavailable stage.
            required = {"202Hg", "204Hg", "206Pb", "207Pb", "208Pb", "232Th", "238U"}
            missing = sorted(required - set(window.session.channels))
            if missing:
                print("Later reduction views unavailable; missing channels: " + ", ".join(missing), flush=True)
            for row, name, action in actions:
                if missing and row >= 5:
                    unavailable.append(name)
                    continue
                window.workflow_step_list.setCurrentRow(row)
                action()
                grab(name)
            if not missing:
                window.results_table.tabs.setCurrentWidget(window.results_table.spot_concordia_page)
                window.results_table.show_samples([window.session.primary_68_standard])
                window.results_table.spot_concordia.restore_default_view()
                grab("09-spot-concordia")
                window.workflow_step_list.setCurrentRow(9)
                window.run_depth_profile_review()
                grab("10-depth-profile-export")
                window.set_plot_mode("final_results", refresh=True)
                window.results_table.tabs.setCurrentWidget(window.results_table.depth_concordia_page)
                grab("10-depth-concordia")
                window.workflow_step_list.setCurrentRow(8)
                window.results_table.tabs.setCurrentWidget(window.results_table.results_page)
                table = build_export_table(window.session.pipeline)
                dialog = ExportResultsDialog(table, folder="Zircon_demo", file_stem="Zircon_demo_results_table", parent=shell)
                # Acquisition metadata is present in real exports and appears
                # in the footnotes on the first preview tab. The Table tab shows
                # only neutral labels and values.
                dialog.tabs.setCurrentIndex(1)
                dialog.show()
                grab("11-export-results", dialog)
                dialog.close()
            else:
                unavailable.extend(("09-spot-concordia", "10-depth-profile-export",
                                    "10-depth-concordia", "11-export-results"))
            links = DatabaseLinks(open_bundle=lambda bundle: None,
                                  open_project=lambda path: None,
                                  parent_widget=lambda: shell)
            dialog = SignInDialog(links, "https://upbdb.example.org", shell)
            dialog.show()
            grab("12-sign-in-to-upbdb", dialog)
            dialog.close()
            dialog = UpdatesDialog(updater, shell)
            dialog.show()
            grab("13-updates", dialog)
            dialog.close()
            (out / "captures.json").write_text(json.dumps({
                "images": [path.name for path in output],
                "unavailable": unavailable,
                "missing_channels": missing,
            }, indent=2) + "\n")
        finally:
            updater._shutdown()
            window.has_unsaved_changes = lambda: False
            shell.close()
            window.thread_pool.waitForDone()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Rich CSV or folder with a Rich CSV and companion spot files")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="PNG output folder")
    parser.add_argument("--scale", type=float, help="Qt scale factor; defaults to the display's device pixel ratio")
    args = parser.parse_args(argv)
    if args.scale is not None and (not 0 < args.scale <= 4):
        parser.error("scale must be greater than zero and at most four")
    try:
        capture_manual(args.source, args.out, args.scale)
    except Exception:
        # Do not include exception text: readers can include source labels or
        # paths in errors. Inspect failures locally with a debugger if needed.
        print("Manual capture failed; no source details were logged.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
