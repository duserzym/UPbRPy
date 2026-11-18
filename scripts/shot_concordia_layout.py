#!/usr/bin/env python3
"""Save concordia layout previews with QWidget.grab(), without opening windows.

The nine-channel reader fixture lacks 202Hg and cannot run Studio's complete
reduction. These images therefore use its measured, uncalibrated Pb/U ratios,
marked as a fixture preview. No missing channel or calibrated result is invented.
Use --project with a complete .upbr project to capture reduced results instead.
Output stays in the folder supplied on the command line.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fixture_preview():
    """Return measured ratio means and sweeps for layout review only."""
    import numpy as np
    import pandas as pd
    from UPbRPy.age import (calc_68_date, calc_68_date_uncertainty, calc_75_date,
                           calc_75_date_uncertainty, calc_76_date, calc_76_date_uncertainty)
    from UPbRPy.constants import common_U238_U235
    from UPbRPy.qtegra import open_qtegra

    session = open_qtegra(ROOT / "tests/fixtures/qtegra/Zircon_demo")
    rows, sweeps = [], []
    for acquisition in session.acquisitions:
        columns = {name: acquisition.y[:, i] for i, name in enumerate(acquisition.channel_names)}
        uranium = columns["238U"]
        # Keep the high-signal sweeps of this acquisition. This is a preview,
        # not an alternative reduction or saved integration-window choice.
        signal = np.isfinite(uranium) & (uranium > np.nanpercentile(uranium, 55))
        with np.errstate(divide="ignore", invalid="ignore"):
            ratios = {"206Pb/238U": columns["206Pb"][signal] / uranium[signal],
                      "207Pb/235U": columns["207Pb"][signal] / uranium[signal] * common_U238_U235,
                      "207Pb/206Pb": columns["207Pb"][signal] / columns["206Pb"][signal],
                      "238U/206Pb": uranium[signal] / columns["206Pb"][signal]}
        row = {"spot_name": acquisition.label, "sample": acquisition.label.rsplit("_", 1)[0]}
        depth = pd.DataFrame(ratios)
        depth["spot_name"], depth["sample"] = row["spot_name"], row["sample"]
        depth["signal_relative_time"] = np.arange(len(depth), dtype=float)
        for ratio, values in ratios.items():
            valid = values[np.isfinite(values) & (values > 0)]
            row[ratio] = np.mean(valid) if valid.size else np.nan
            row[ratio + "_2e"] = 2*np.std(valid, ddof=1)/np.sqrt(valid.size) if valid.size > 1 else np.nan
            depth[ratio + "_2e"] = row[ratio + "_2e"]
        for ratio, date, error in (("206Pb/238U", calc_68_date, calc_68_date_uncertainty),
                                   ("207Pb/235U", calc_75_date, calc_75_date_uncertainty),
                                   ("207Pb/206Pb", calc_76_date, calc_76_date_uncertainty)):
            row[ratio + "_age"] = float(date(row[ratio]))
            row[ratio + "_age_2e"] = float(error(row[ratio], row[ratio + "_2e"]))
        rows.append(row)
        sweeps.append(depth)
    frame = pd.DataFrame(rows)
    # The depth renderer consumes these column names. For this explicitly
    # uncalibrated preview only, put the measured values into those slots.
    depth = pd.concat(sweeps).rename(columns={
        column: column.replace(ratio, ratio + "_fractionation_corrected")
        for column in sweeps[0].columns for ratio in
        ("206Pb/238U", "207Pb/235U", "207Pb/206Pb", "238U/206Pb")
        if column.startswith(ratio)})
    return SimpleNamespace(selected_results=frame.copy(), parallel_results=frame,
                           decision=SimpleNamespace(include_calibration_uncertainty=False)), depth


def detail_crops(shell, window, output, app):
    """Render vector details and a long-sample popup directly at DPR 2."""
    import pandas as pd
    from PySide6 import QtCore, QtGui, QtWidgets

    review = window.results_table
    finalized, _ = fixture_preview()
    # Synthetic labels only: repeat measured fixture ratios for layout review.
    frame = pd.concat([finalized.parallel_results.iloc[[i % len(finalized.parallel_results)]]
                       for i in range(20)], ignore_index=True)
    frame["sample"] = [f"Long sample name for layout preview {i:02d} with additional field description"
                       for i in range(20)]
    frame["spot_name"] = [f"preview_{i}" for i in range(20)]
    review.show_depth_profile(pd.DataFrame(), "")
    review.show_results(SimpleNamespace(selected_results=frame.copy(), parallel_results=frame))
    review.refresh_spot_concordia(reset_view=True)
    review.set_spot_excluded({"preview_1"}, True)
    review.set_selected_spots({"preview_0"})
    review.tabs.setCurrentWidget(review.spot_concordia_page)
    window.plots_stack.setCurrentWidget(review)

    def save(widget, rect, filename):
        # QWidget.render repaints at the target DPR; no enlarged 1x bitmap.
        pixmap = QtGui.QPixmap(widget.width() * 2, widget.height() * 2)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        QtWidgets.QWidget.render(widget, pixmap)
        pixels = QtCore.QRect(rect.x()*2, rect.y()*2, rect.width()*2, rect.height()*2)
        if not pixmap.copy(pixels).save(str(output / filename)):
            raise RuntimeError(f"Could not save {filename}")

    for size in ((1280, 800), (1440, 900)):
        window.set_plot_focus(False)
        shell.resize(*size)
        window.apply_default_layout()
        for _ in range(12): app.processEvents()
        canvas = review.spot_concordia
        canvas._set_hover(None)
        review.spot_card.hide()
        for name, label in (("preview_0", "selected-outline"), ("preview_1", "hidden-dashed")):
            canvas.set_included_spots({name})
            handle = next(ellipse for key, ellipse, _ in canvas._ellipses if key == name)
            scene = canvas._plot_item.vb.mapViewToScene(handle.path.boundingRect())
            rect = canvas.mapFromScene(scene).boundingRect().adjusted(-16, -16, 16, 16)
            rect = rect.intersected(canvas.rect())
            save(canvas, rect, f"{label}-{size[0]}x{size[1]}-dpr2.png")
        canvas.set_included_spots(set(frame.spot_name))
        save(canvas, QtCore.QRect(0, max(0, canvas.height()-120), min(320, canvas.width()),
                                 min(120, canvas.height())),
             f"lower-left-{size[0]}x{size[1]}-dpr2.png")
        review.spot_sample_chips.menu_button.showMenu()
        for _ in range(8): app.processEvents()
        popup = review.spot_sample_chips.menu_button.menu()
        save(popup, popup.rect(), f"samples-long-20-{size[0]}x{size[1]}-dpr2.png")
        popup.hide()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--project", type=Path, help="A complete local .upbr project instead of the fixture preview")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    from PySide6 import QtCore
    from GUI.theme import build_application
    from GUI import main_window
    from GUI.studio_window import StudioWindow

    app = build_application([], "light")
    main_window.Updater.start_scheduler = lambda self: None
    original_settings = QtCore.QSettings
    with tempfile.TemporaryDirectory(prefix="upbrpy-concordia-shots-") as scratch:
        settings = original_settings(str(Path(scratch)/"viewer.ini"), original_settings.Format.IniFormat)
        QtCore.QSettings = lambda *a, **kw: settings
        shell = StudioWindow(theme_mode="light")
        window = shell.active_reduction
        try:
            if args.project:
                window.load_project_file(args.project, background=False)
                window.set_plot_mode("final_results", refresh=True)
            else:
                rich = ROOT / "tests/fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"
                window.load_experiment_directory(str(rich), background=False, offer_restore=False)
                finalized, depth = fixture_preview()
                window.session.reduction_cache["finalized_results"] = finalized
                window.results_table.show_results(finalized)
                focused = next(key for key, shown in window.results_table.include_flags.items() if shown)
                window.results_table.show_depth_profile(depth, focused, include_calibration_uncertainty=False)
                window.session.plot_mode = "final_results"
                window.plots_stack.setCurrentWidget(window.results_table)
                window.results_table.summary_label.setText("Uncalibrated fixture preview · measured ratios")
            review = window.results_table
            shown = [key for key, included in review.include_flags.items() if included]
            if shown:
                review.set_selected_spots({shown[0]})
            review.tabs.setCurrentWidget(review.spot_concordia_page)
            window._apply_interaction_controls_to_current_plot()
            shell.show()
            dimensions = []
            for theme in ("light", "dark"):
                window.apply_theme_mode(theme, persist=False)
                for width, height in ((1280, 800), (1440, 900)):
                    window.set_plot_focus(False)
                    shell.resize(width, height)
                    window.apply_default_layout()
                    for focus in (False, True):
                        window.set_plot_focus(focus)
                        for _ in range(12): app.processEvents()
                        for kind in ("spot", "depth"):
                            review.tabs.setCurrentWidget(getattr(review, kind + "_concordia_page"))
                            for _ in range(8): app.processEvents()
                            if not args.project:
                                review.summary_label.setText("Uncalibrated fixture preview · measured ratios")
                            filename = f"{kind}-{width}x{height}-{'focus' if focus else 'open'}-{theme}.png"
                            if not shell.grab().save(str(args.output / filename)):
                                raise RuntimeError(f"Could not save {filename}")
                            plot = getattr(review, kind + "_concordia")
                            toolbar = getattr(review, kind + "_toolbar")
                            folded = [menu.title() for _, menu, _, _ in toolbar.entries
                                      if menu.menuAction().isVisible()]
                            dimensions.append(f"{filename}: plot {plot.width()}x{plot.height()} · "
                                              f"stage visible={window.stage_segment_bar.isVisible()} · "
                                              f"overflow={', '.join(folded) or 'none'}" +
                                              (f" · card {review.spot_card.width()}x{review.spot_card.height()}"
                                               if kind == "spot" else ""))
            window.apply_theme_mode("light", persist=False)
            detail_crops(shell, window, args.output, app)
            (args.output / "dimensions.txt").write_text("\n".join(dimensions) + "\n")
            print("\n".join(dimensions))
        finally:
            window.set_plot_focus(False)
            window.has_unsaved_changes = lambda: False
            shell.close()
            app.processEvents()
            QtCore.QSettings = original_settings


if __name__ == "__main__":
    main()
