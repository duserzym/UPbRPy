"""Live concordia regression controls, cancellable workers and retained overlays."""
from __future__ import annotations

import json
import importlib
import threading
from pathlib import Path

import numpy as np
import pandas as pd
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from UPbRPy.regression import (
    Anchor, BootstrapResult, bootstrap_discordia, concordia_xy, fit_discordia,
    fit_record, write_isochron_fits,
)
from UPbRPy.results import format_mswd, format_value_with_uncertainty


_BOOT_CHUNK = 125


def _export_svg(scene, path):
    """Use pyqtgraph's SVG exporter with Qt's close-path tokens preserved.

    This pyqtgraph version's coordinate mapper assumes every path token has
    an x,y pair, but Qt emits standalone Z tokens for closed paths. Temporarily
    remove those tokens while it transforms coordinates, then restore them.
    The hook is scoped to this synchronous GUI-thread export.
    """
    module = importlib.import_module("pyqtgraph.exporters.SVGExporter")
    original = module.correctCoordinates

    def coordinates(node, defs, item, options):
        closed = []
        for element in node.getElementsByTagName("path"):
            tokens = element.getAttribute("d").split()
            positions = [i for i, token in enumerate(tokens) if token.upper() == "Z"]
            if positions:
                closed.append((element, positions))
                element.setAttribute("d", " ".join(token for token in tokens if token.upper() != "Z"))
        original(node, defs, item, options)
        for element, positions in closed:
            tokens = element.getAttribute("d").split()
            for index in positions:
                tokens.insert(index, "Z")
            element.setAttribute("d", " ".join(tokens))

    module.correctCoordinates = coordinates
    try:
        module.SVGExporter(scene).export(str(path))
    finally:
        module.correctCoordinates = original


class _FitSignals(QtCore.QObject):
    partial = QtCore.Signal(object)
    finished = QtCore.Signal(object)


class _FitWorker(QtCore.QRunnable):
    """Compute fits and deterministic bootstrap chunks without accessing widgets."""

    def __init__(self, generation, scopes, options, cancelled):
        super().__init__()
        self.generation, self.scopes, self.options = generation, scopes, options
        self.cancelled = cancelled
        self.signals = _FitSignals()

    def run(self):
        results, errors = [], []
        try:
            for scope in self.scopes:
                if self.cancelled.is_set():
                    return
                try:
                    fit = fit_discordia(*scope["points"], **scope["kwargs"])
                    results.append((scope, fit, None))
                except (ValueError, RuntimeError, ArithmeticError, np.linalg.LinAlgError) as exc:
                    errors.append(f"{scope['label']} · {scope['n_valid']} of {scope['n_total']} "
                                  f"({scope['excluded_count']} excluded): {exc}")
                self._emit(results, errors)
            for index, (scope, fit, _) in enumerate(results):
                chunks = []
                target = self.options["resamples"]
                for offset in range(0, target, _BOOT_CHUNK):
                    if self.cancelled.is_set():
                        return
                    # Each chunk has an explicit seed. The preview is a prefix
                    # of the final distribution; changing the target preserves it.
                    chunk = bootstrap_discordia(
                        *scope["points"], **scope["kwargs"], fit=fit,
                        method="resample" if scope["depth"] else "parametric",
                        n_resamples=min(_BOOT_CHUNK, target-offset),
                        seed=self.options["seed"] + offset // _BOOT_CHUNK,
                        chunk=_BOOT_CHUNK,
                    )
                    chunks.append(chunk)
                    count = sum(c.n_resamples for c in chunks)
                    if count % 250 == 0 or count == target:
                        boot = BootstrapResult(
                            chunk.method, count, self.options["seed"], chunk.confidence,
                            *(np.concatenate([getattr(c, name) for c in chunks]) for name in
                              ("intercept", "slope", "lower_ma", "upper_ma", "common_pb_76", "converged")),
                            elapsed_s=sum(c.elapsed_s for c in chunks),
                            notes=list(chunk.notes) + [
                                f"Studio bootstrap chunks: {_BOOT_CHUNK} resamples; chunk k uses seed + k (k starts at 0)."],
                        )
                        results[index] = scope, fit, boot
                        self._emit(results, errors)
        except Exception as exc:
            errors.append(str(exc))
            self._emit(results, errors)
        finally:
            self.signals.finished.emit(self.generation)

    def _emit(self, results, errors):
        if not self.cancelled.is_set():
            self.signals.partial.emit((self.generation, list(results), list(errors)))


class ReviewFitPanel(QtWidgets.QWidget):
    """Fit one concordia page using the engine and a coalesced worker job."""

    recordsChanged = QtCore.Signal()

    def __init__(self, review, kind):
        super().__init__()
        self.review, self.kind = review, kind
        self.view_name = "spot means" if kind == "spot" else "depth-profile intervals"
        self.records = []
        self._generation = 0
        self._cancelled = threading.Event()
        self._workers = {}
        self._overlays = []
        self._restoring = False
        self._was_enabled = False
        self._fit_view_pending = False
        self._scope_count = 0
        self._analytical_complete = False
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._start)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        row = QtWidgets.QHBoxLayout()
        self.fit_button = QtWidgets.QToolButton()
        self.fit_button.setText("Fit")
        self.fit_button.setCheckable(True)
        self.fit_button.setToolTip("Live discordia / isochron fits of selected spots, or all shown spots")
        row.addWidget(self.fit_button)
        self.options_button = QtWidgets.QToolButton()
        self.options_button.setText("Options")
        self.options_button.setToolTip("Fit options: model, anchor, decay constants, bootstrap, export")
        self.options_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(self.options_button)
        form_widget = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(form_widget)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.together = QtWidgets.QCheckBox("One fit through all selected")
        form.addRow(self.together)
        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.addItems(["Model 1", "Model 2", "Model 3"])
        form.addRow("Model", self.model_combo)
        self.anchor_combo = QtWidgets.QComboBox()
        for label, key in (("None", "none"), ("Common 207Pb/206Pb", "common_pb"),
                           ("Stacey–Kramers", "stacey_kramers"), ("Age (Ma)", "age")):
            self.anchor_combo.addItem(label, key)
        form.addRow("Anchor", self.anchor_combo)
        self.anchor_value = QtWidgets.QDoubleSpinBox()
        self.anchor_value.setDecimals(6)
        self.anchor_value.setRange(0, 10000)
        self.anchor_value.setValue(.85)
        form.addRow("Anchor value", self.anchor_value)
        self.anchor_sigma = QtWidgets.QDoubleSpinBox()
        self.anchor_sigma.setDecimals(6)
        self.anchor_sigma.setRange(0, 10000)
        form.addRow("± 1s", self.anchor_sigma)
        self.decay_check = QtWidgets.QCheckBox("Decay constants")
        form.addRow(self.decay_check)
        self.resamples = QtWidgets.QSpinBox()
        self.resamples.setRange(1, 100000)
        self.resamples.setValue(2000)
        form.addRow("Bootstrap resamples", self.resamples)
        self.seed = QtWidgets.QSpinBox()
        self.seed.setRange(0, 2147480000)
        self.seed.setValue(1)
        form.addRow("Seed", self.seed)
        action = QtWidgets.QWidgetAction(menu)
        action.setDefaultWidget(form_widget)
        menu.addAction(action)
        menu.addSeparator()
        menu.addAction("Export fits…", self.export_fits)
        self.options_button.setMenu(menu)
        self.fit_button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.options_button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0; }")
        self.options_button.setFixedWidth(self.options_button.fontMetrics().horizontalAdvance("Options") + 40)
        row.addWidget(self.options_button)
        layout.addLayout(row)
        self.readout = QtWidgets.QPlainTextEdit()
        self.readout.setReadOnly(True)
        self.readout.setPlaceholderText("Fit selected spots or all shown spots")
        self.readout.setMinimumHeight(60)
        self.readout.setMaximumHeight(150)
        self.readout.setMinimumWidth(0)
        self.readout.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        self.readout.setVisible(False)
        layout.addWidget(self.readout)
        for control in (self.together, self.decay_check, self.fit_button):
            control.toggled.connect(self._changed)
        self.model_combo.currentIndexChanged.connect(self._changed)
        self.anchor_combo.currentIndexChanged.connect(self._anchor_changed)
        for control in (self.anchor_value, self.anchor_sigma, self.resamples, self.seed):
            control.valueChanged.connect(self._changed)
        if review._settings is not None:
            try:
                saved = json.loads(review._settings.value(f"review_fit_{kind}_v1", "{}", type=str))
                self.restore_settings(saved)
            except (TypeError, ValueError):
                pass
        self._anchor_enabled()
        self.recordsChanged.connect(review.appearance_changed)
        # A running worker owns only snapshots. Cancelling on destruction stops
        # its next chunk; Qt disconnects the receiver automatically.
        self._lifetime_token = [self._cancelled]
        tokens = self._lifetime_token
        self.destroyed.connect(lambda: tokens[0].set())

    @property
    def canvas(self):
        return getattr(self.review, self.kind + "_concordia")

    def settings(self) -> dict:
        """Return JSON-safe fit controls, including whether live fits are enabled."""
        return dict(enabled=self.fit_button.isChecked(), together=self.together.isChecked(),
                    model=self.model_combo.currentIndex()+1, anchor=self.anchor_combo.currentData(),
                    anchor_value=self.anchor_value.value(), anchor_sigma=self.anchor_sigma.value(),
                    decay=self.decay_check.isChecked(), resamples=self.resamples.value(), seed=self.seed.value())

    def restore_settings(self, settings: dict) -> None:
        """Apply saved fit settings without writing them back as user defaults."""
        self._restoring = True
        try:
            for key, control in (("enabled", self.fit_button), ("together", self.together), ("decay", self.decay_check)):
                if key in settings:
                    control.setChecked(bool(settings[key]))
            self.model_combo.setCurrentIndex(max(0, min(2, int(settings.get("model", 1))-1)))
            self.anchor_combo.setCurrentIndex(max(0, self.anchor_combo.findData(settings.get("anchor", "none"))))
            for key, control in (("anchor_value", self.anchor_value), ("anchor_sigma", self.anchor_sigma),
                                 ("resamples", self.resamples), ("seed", self.seed)):
                if key in settings:
                    control.setValue(settings[key])
            self._anchor_enabled()
        finally:
            self._restoring = False
        self.readout.setVisible(self.fit_button.isChecked())
        self._was_enabled = self.fit_button.isChecked()
        self._fit_view_pending = self._was_enabled
        self.invalidate()

    def set_enabled_default(self, enabled: bool) -> None:
        """Start a new session with live fits on or off, keeping other options.

        The per-user preference is not changed.
        """
        self._restoring = True
        try:
            self.fit_button.setChecked(bool(enabled))
        finally:
            self._restoring = False
        self.readout.setVisible(self.fit_button.isChecked())
        self._was_enabled = self.fit_button.isChecked()
        self._fit_view_pending = self._was_enabled
        self.invalidate()

    def _anchor_enabled(self):
        enabled = self.anchor_combo.currentData() in {"common_pb", "age"}
        self.anchor_value.setEnabled(enabled)
        self.anchor_sigma.setEnabled(enabled)

    def _anchor_changed(self):
        self._anchor_enabled()
        if not self._restoring:
            blocker = QtCore.QSignalBlocker(self.anchor_value)
            self.anchor_value.setValue(1000 if self.anchor_combo.currentData() == "age" else .85)
            del blocker
        self._changed()

    def _changed(self, *_args):
        if self._restoring:
            return
        enabled = self.fit_button.isChecked()
        if enabled and not self._was_enabled:
            self._fit_view_pending = True
        self._was_enabled = enabled
        self.readout.setVisible(enabled)
        if self.review._settings is not None:
            self.review._settings.setValue(f"review_fit_{self.kind}_v1", json.dumps(self.settings()))
        self.invalidate()
        self.review.appearance_changed.emit()

    def invalidate(self):
        """Cancel stale work immediately and coalesce the next fit update."""
        self._cancelled.set()
        self._generation += 1
        self._timer.stop()
        self._clear_overlays()
        if self.fit_button.isChecked():
            self.records = []
            self.readout.setPlainText("Fitting…")
            self._timer.start(60)

    def _clear_overlays(self):
        for plot, items in self._overlays:
            for item in items:
                if item in plot.items:
                    plot.removeItem(item)
        self._overlays.clear()

    def _scopes(self):
        canvas = self.canvas
        frame = canvas._frame
        if "spot_name" not in frame or frame.empty:
            return []
        names = frame.spot_name.astype(str)
        shown = names.map(lambda name: self.review.include_flags.get(name, True))
        if self.review.selected_spots:
            shown &= names.isin(self.review.selected_spots)
        scoped = frame.loc[shown]
        settings = self.settings()
        groups = [("selection", scoped)] if settings["together"] else list(
            scoped.groupby("sample", sort=False, dropna=False)) if "sample" in scoped else [("selection", scoped)]
        xcol, ycol, sxcol, sycol = canvas._ratio_columns()
        if not all(c is not None and c in frame for c in (xcol, ycol, sxcol, sycol)):
            return []
        branch = "with_calibration" if canvas._include_calibration_uncertainty else "no_calibration"
        stem = "err_corr" if canvas._diagram == "wetherill" else "tw_rho"
        rho_column = next((c for c in (f"{stem}_{branch}", stem) if c in frame), None)
        anchor = Anchor(settings["anchor"], settings["anchor_value"], settings["anchor_sigma"])
        scopes = []
        for label, scope in groups:
            excluded = sorted(set(scope.spot_name.astype(str)) & self.review.excluded_spots)
            used = scope.loc[~scope.spot_name.astype(str).isin(excluded)]
            def numeric(column):
                return pd.to_numeric(used[column], errors="coerce").to_numpy(dtype=float)
            x, y, sx, sy = (numeric(c) for c in (xcol, ycol, sxcol, sycol))
            rho = numeric(rho_column) if rho_column else np.zeros(len(used))
            rho = np.clip(np.nan_to_num(rho, nan=0., posinf=np.inf, neginf=-np.inf), -.999, .999)
            valid = np.isfinite(x) & np.isfinite(y) & np.isfinite(sx) & np.isfinite(sy) & np.isfinite(rho) & (sx > 0) & (sy > 0)
            # Let the engine report an informative insufficient-points error.
            kwargs = dict(space=canvas._diagram, model=settings["model"], anchor=anchor,
                          include_decay_uncertainty=settings["decay"])
            depth = self.kind == "depth"
            if depth:
                kwargs.update(groups=used.spot_name.astype(str).to_numpy(),
                              order=numeric("signal_relative_time") if "signal_relative_time" in used else np.arange(len(used)))
            scopes.append(dict(label=str(label), points=(x, y, sx/2, sy/2, rho), kwargs=kwargs,
                               depth=depth, n_total=len(scope), n_valid=int(valid.sum()), excluded=excluded,
                               excluded_count=len(scope)-len(used),
                               items=list(dict.fromkeys(used.loc[valid, "spot_name"].astype(str))),
                               branch="with calibration" if canvas._include_calibration_uncertainty else "analytical"))
        return scopes

    def _start(self):
        if not self.fit_button.isChecked():
            return
        scopes = self._scopes()
        if not scopes:
            self.readout.setPlainText("No compatible shown points to fit")
            return
        self._scope_count = len(scopes)
        self._analytical_complete = False
        self._cancelled = threading.Event()
        self._lifetime_token[0] = self._cancelled
        worker = _FitWorker(self._generation, scopes, self.settings(), self._cancelled)
        self._workers[self._generation] = worker
        worker.signals.partial.connect(self._accept, QtCore.Qt.ConnectionType.QueuedConnection)
        worker.signals.finished.connect(self._finished, QtCore.Qt.ConnectionType.QueuedConnection)
        QtCore.QThreadPool.globalInstance().start(worker)

    @QtCore.Slot(object)
    def _finished(self, generation):
        self._workers.pop(generation, None)

    @QtCore.Slot(object)
    def _accept(self, payload):
        generation, results, errors = payload
        if generation != self._generation or not self.fit_button.isChecked():
            return
        records = []
        for scope, fit, boot in results:
            record = fit_record(fit, boot)
            record.update(label=scope["label"], view=self.view_name, uncertainty_branch=scope["branch"],
                          items=scope["items"], excluded=scope["excluded"], n_total=scope["n_total"],
                          excluded_count=scope["excluded_count"], bootstrap_chunk_size=_BOOT_CHUNK,
                          bootstrap_seed_rule="seed + chunk index", requested_resamples=self.resamples.value(),
                          requested_seed=self.seed.value())
            for which in ("lower", "upper"):
                age = getattr(fit, which)
                if age:
                    record[which + "_note"] = age.note
            records.append(record)
        self.records = records
        self._analytical_complete = len(results) + len(errors) >= self._scope_count
        self.show_records(errors)
        self._draw_records()
        self.recordsChanged.emit()

    def show_records(self, errors=()):
        blocks = []
        for r in self.records:
            anchor = "no anchor" if r.get("anchor") == "none" else r.get("anchor", "")
            lines = [f"{r.get('label')} · {r.get('n')} of {r.get('n_total')} ({r.get('excluded_count', len(r.get('excluded', [])))} excluded) · model {r.get('model')} · {anchor}"]
            suffix = "_1s_with_decay" if r.get("decay_uncertainty_included") else "_1s"
            for which in ("lower", "upper"):
                age = r.get(which + "_age_ma", np.nan)
                if np.isfinite(age):
                    text = format_value_with_uncertainty(age, 2*r.get(which + "_age" + suffix, np.nan))
                    line = f"{which.title()} {text} Ma (2s)"
                    low, high = r.get(f"bootstrap_{which}_ma_low"), r.get(f"bootstrap_{which}_ma_high")
                    if low is not None and np.isfinite(low) and np.isfinite(high):
                        line += f" · bootstrap {low:.4g}–{high:.4g} (95 %)"
                    if r.get(which + "_note"):
                        line += " · " + r[which + "_note"]
                    lines.append(line)
            common = format_value_with_uncertainty(r.get("common_207Pb_206Pb"), 2*r.get("common_207Pb_206Pb_1s", np.nan))
            lines.append(f"Common 207Pb/206Pb {common} · MSWD {format_mswd(r.get('mswd'))}, p {r.get('p_value', np.nan):.3g}")
            if r.get("model") == 3:
                lines.append(f"Dispersion {r.get('dispersion', np.nan):.6g} {r.get('dispersion_units', '')}")
            if self.kind == "depth":
                lines.append(f"Lag-1 {r.get('autocorrelation', np.nan):.3g} · effective n {r.get('n_effective', np.nan):.4g}")
            lines.extend(str(note) for note in r.get("notes", []) if not str(note).startswith("Successive intervals"))
            if "bootstrap_resamples" in r:
                target = r.get("requested_resamples", r["bootstrap_resamples"])
                lines.append(f"{r['bootstrap_resamples']} of {target} resamples · seed {r['bootstrap_seed']}")
            elif "requested_resamples" in r:
                lines.append(f"Bootstrapping… {r['requested_resamples']} resamples · seed {r['requested_seed']}")
            blocks.append("\n".join(lines))
        self.readout.setPlainText("\n\n".join([*blocks, *errors]))

    def _draw_records(self):
        canvas = self.canvas
        plot = canvas._plot_item
        if plot is None:
            return
        while len(self._overlays) > len(self.records):
            old_plot, items = self._overlays.pop()
            for item in items:
                old_plot.removeItem(item)
        fit_x, fit_y = [], []
        for index, r in enumerate(self.records):
            if index >= len(self._overlays):
                items = [pg.PlotDataItem() for _ in range(3)] + [pg.ScatterPlotItem(symbol="d", size=9)]
                for item in items:
                    item.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
                    item.setZValue(5)
                    plot.addItem(item, ignoreBounds=True)
                self._overlays.append((plot, items))
            items = self._overlays[index][1]
            color = canvas._sample_colors.get(r["label"], canvas._theme.accent)
            for item in items[:3]:
                item.setPen(pg.mkPen(color, width=1.5 if item is items[0] else .8,
                    style=QtCore.Qt.PenStyle.SolidLine if item is items[0] else QtCore.Qt.PenStyle.DashLine))
            selected_x = [x for name, x, _y, _color in canvas._points if name in r["items"]]
            ages = [r.get(which + "_age_ma", np.nan) for which in ("lower", "upper")]
            ages = np.asarray([age for age in ages if np.isfinite(age)])
            cx, cy = concordia_xy(ages, r["space"])
            bounds = selected_x + list(cx)
            if r["space"] == "tera_wasserburg":
                bounds.append(0.)
            if not bounds:
                continue
            xs = np.linspace(min(bounds), max(bounds), 200)
            ys = r["intercept"] + r["slope"]*xs
            fit_x.extend([xs[0], xs[-1]])
            fit_y.extend([ys[0], ys[-1]])
            variance = r["intercept_1s"]**2 + 2*xs*r["cov_intercept_slope"] + xs**2*r["slope_1s"]**2
            delta = 1.96*np.sqrt(np.maximum(variance, 0))
            for item, values in zip(items[:3], (ys, ys-delta, ys+delta)):
                item.setData(xs, values)
            items[3].setData(cx, cy, pen=pg.mkPen(color), brush=pg.mkBrush(color))
        if self._fit_view_pending and self._analytical_complete and fit_x:
            current_x, current_y = plot.vb.viewRange()
            plot.setRange(xRange=(min(*current_x, *fit_x), max(*current_x, *fit_x)),
                          yRange=(min(*current_y, *fit_y), max(*current_y, *fit_y)), padding=.04)
            self._fit_view_pending = False

    def export_fits(self):
        """Write the fit table, footnotes, and current SVG and PNG canvas."""
        if not self.records:
            self.readout.setPlainText("Turn Fit on to calculate fits before export")
            return
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Export fits")
        if not folder:
            return
        try:
            stem = "spot_means_isochron_fits" if self.kind == "spot" else "depth_intervals_isochron_fits"
            write_isochron_fits(self.records, folder, stem)
            from pyqtgraph.exporters import ImageExporter
            _export_svg(self.canvas.scene(), Path(folder) / (stem + ".svg"))
            ImageExporter(self.canvas.scene()).export(str(Path(folder) / (stem + ".png")))
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Fit export failed", str(exc))
