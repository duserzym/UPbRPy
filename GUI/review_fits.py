"""Live concordia regression controls, cancellable workers and retained overlays."""
from __future__ import annotations

import json
import importlib
import threading
import time
import hashlib
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pandas as pd
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from UPbRPy.regression import (
    Anchor, BootstrapResult, bootstrap_discordia, concordia_xy, fit_discordia,
    fit_record, model_name, write_isochron_fits,
)
from UPbRPy.results import format_mswd, format_value_with_uncertainty


_BOOT_CHUNK = 500
_PARTIAL_INTERVAL = .2
_MODEL_TOOLTIPS = (
    "Maximum-likelihood (York) fit weighted by each analysis's analytical uncertainties "
    "and error correlations. Reports MSWD and p; reported uncertainties are expanded "
    "by sqrt(MSWD) when p < alpha (default 0.05). Isoplot/IsoplotR model 1.",
    "Total-least-squares (orthogonal) line in Tera-Wasserburg coordinates, ignoring "
    "analytical uncertainties. Parameter uncertainties come from point scatter about "
    "the line. Wetherill results are transformed back. Isoplot/IsoplotR model 2.",
    "Maximum likelihood with extra dispersion fitted with the line: in radiogenic "
    "end-member age (Ma), or in common 207Pb/206Pb for a common-Pb anchor. An anchor "
    "uncertainty fixes that dispersion instead of fitting it. Reports dispersion. "
    "Isoplot/IsoplotR model 3.",
)


def _chunk_size(resamples):
    # Retain finer previews for short, explicitly requested bootstraps.
    return _BOOT_CHUNK if resamples > 500 else 125


def _scope_key(scope):
    """Hash numerical content, including acquisition groups and their order."""
    digest = hashlib.blake2b(digest_size=24)
    def add(value):
        if isinstance(value, np.ndarray):
            digest.update(str((value.dtype.str, value.shape)).encode())
            if value.dtype.kind in "OUS":
                add(value.tolist())
            else:
                digest.update(np.ascontiguousarray(value).tobytes())
        elif isinstance(value, dict):
            for key in sorted(value):
                add(key)
                add(value[key])
        elif isinstance(value, (list, tuple)):
            digest.update(str(len(value)).encode())
            for item in value:
                add(item)
        else:
            data = repr(value).encode()
            digest.update(str(len(data)).encode()+b":"+data)
    add((scope["points"], scope["kwargs"], scope["depth"]))
    return digest.digest()


class _FitCache:
    """Bounded LRU shared by a panel and its snapshot workers."""

    def __init__(self, capacity=32):
        self.capacity = capacity
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            if key not in self.entries:
                return None
            self.entries.move_to_end(key)
            return self.entries[key]

    def put(self, key, value):
        with self.lock:
            self.entries[key] = value
            self.entries.move_to_end(key)
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)



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

    def __init__(self, generation, scopes, options, cancelled, cache=None):
        super().__init__()
        self.generation, self.scopes, self.options = generation, scopes, options
        self.cancelled = cancelled
        self.cache = cache if cache is not None else _FitCache()
        self.chunk_size = _chunk_size(options["resamples"])
        self._last_emit = -float("inf")
        self.signals = _FitSignals()

    def run(self):
        results, errors = [], []
        try:
            for scope in self.scopes:
                if self.cancelled.is_set():
                    return
                try:
                    key = ("fit", _scope_key(scope))
                    fit = self.cache.get(key)
                    if fit is None:
                        fit = fit_discordia(*scope["points"], **scope["kwargs"])
                        self.cache.put(key, fit)
                    results.append((scope, fit, None))
                except (ValueError, RuntimeError, ArithmeticError, np.linalg.LinAlgError) as exc:
                    errors.append(f"{scope['label']} · {scope['n_valid']} of {scope['n_total']} "
                                  f"({scope['excluded_count']} excluded): {exc}")
                self._emit(results, errors)
            for index, (scope, fit, _) in enumerate(results):
                chunks = []
                target = self.options["resamples"]
                key = ("bootstrap", _scope_key(scope), target, self.options["seed"], self.chunk_size)
                cached = self.cache.get(key)
                if cached is not None:
                    results[index] = scope, fit, cached
                    self._emit(results, errors)
                    continue
                for offset in range(0, target, self.chunk_size):
                    if self.cancelled.is_set():
                        return
                    # Each chunk has an explicit seed. At the recorded chunk
                    # size, the preview is a prefix of the final distribution.
                    chunk = bootstrap_discordia(
                        *scope["points"], **scope["kwargs"], fit=fit,
                        method="resample" if scope["depth"] else "parametric",
                        n_resamples=min(self.chunk_size, target-offset),
                        seed=self.options["seed"] + offset // self.chunk_size,
                        chunk=self.chunk_size,
                    )
                    chunks.append(chunk)
                    count = sum(c.n_resamples for c in chunks)
                    if count == target or time.monotonic()-self._last_emit >= _PARTIAL_INTERVAL:
                        boot = BootstrapResult(
                            chunk.method, count, self.options["seed"], chunk.confidence,
                            *(np.concatenate([getattr(c, name) for c in chunks]) for name in
                              ("intercept", "slope", "lower_ma", "upper_ma", "common_pb_76", "converged")),
                            elapsed_s=sum(c.elapsed_s for c in chunks),
                            notes=list(chunk.notes) + [
                                f"Studio bootstrap chunks: {self.chunk_size} resamples; chunk k uses seed + k (k starts at 0)."],
                        )
                        results[index] = scope, fit, boot
                        if count == target:
                            self.cache.put(key, boot)
                        self._emit(results, errors)
        except Exception as exc:
            errors.append(str(exc))
            self._emit(results, errors)
        finally:
            self._emit(results, errors, force=True)
            self.signals.finished.emit(self.generation)

    def _emit(self, results, errors, *, force=False):
        now = time.monotonic()
        if not self.cancelled.is_set() and (force or now-self._last_emit >= _PARTIAL_INTERVAL):
            self._last_emit = now
            self.signals.partial.emit((self.generation, list(results), list(errors)))


class ReviewFitPanel(QtWidgets.QWidget):
    """Fit one concordia page using the engine and a coalesced worker job."""

    recordsChanged = QtCore.Signal()

    def __init__(self, review, kind):
        super().__init__()
        self.review, self.kind = review, kind
        self.view_name = "spot means" if kind == "spot" else "depth-profile intervals"
        self.records = []
        self._live_records = self.records
        self._errors = []
        self._generation = 0
        self._cancelled = threading.Event()
        self._workers = {}
        self._cache = _FitCache()
        self._request_key = None
        self._pending_scopes = []
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
        for model, tooltip in enumerate(_MODEL_TOOLTIPS, 1):
            self.model_combo.addItem(model_name(model), model)
            self.model_combo.setItemData(model-1, tooltip, QtCore.Qt.ItemDataRole.ToolTipRole)
        self.model_combo.setToolTip(_MODEL_TOOLTIPS[0])
        self.model_combo.currentIndexChanged.connect(
            lambda index: self.model_combo.setToolTip(_MODEL_TOOLTIPS[index]))
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
        # Restoring a project may next install saved records with a different
        # recorded scope/chunk size. Stop any current job even when the controls
        # happen to match; the caller can stop the new coalescing timer.
        self._request_key = None
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
        enabled = self.fit_button.isChecked()
        scopes = self._scopes() if enabled else []
        options = self.settings()
        key = (enabled, tuple((scope["label"], _scope_key(scope)) for scope in scopes),
               options["resamples"], options["seed"], _chunk_size(options["resamples"]))
        def metadata_key(values):
            return [(v["label"], v["n_total"], v["excluded_count"], v["items"], v["excluded"], v["branch"])
                    for v in values]
        metadata_changed = metadata_key(scopes) != metadata_key(self._pending_scopes)
        self._pending_scopes = scopes
        live = self.records is self._live_records
        if key == self._request_key and (live or not metadata_changed):
            # Counts and exclusions can change while the fitted arrays stay
            # identical (for example, selecting an already-excluded spot).
            current = {scope["label"]: scope for scope in scopes}
            update_records = False
            if live and metadata_changed:
                for record in self.records:
                    if record.get("label") in current:
                        update_records |= self._record_scope_metadata(record, current[record["label"]])
            if enabled:
                if update_records:
                    self.show_records(self._errors)
                self._draw_records()
                if update_records:
                    self.recordsChanged.emit()
            return
        self._request_key = key
        self._cancelled.set()
        self._generation += 1
        self._timer.stop()
        self._clear_overlays()
        if enabled:
            self.records = []
            self._live_records = self.records
            self._errors = []
            self.readout.setPlainText("Fitting…")
            self._timer.start(60)

    def _clear_overlays(self):
        for plot, items in self._overlays:
            for item in items:
                if item in plot.items:
                    plot.removeItem(item)
        self._overlays.clear()

    def _scopes(self):
        self.review._ensure_fit_canvas(self.kind)
        # User defaults are restored while the owner is still constructing
        # this page, before its canvas has been assigned.
        canvas = getattr(self.review, self.kind + "_concordia", None)
        if canvas is None:
            return []
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
                return pd.to_numeric(used[column], errors="coerce").to_numpy(dtype=float, copy=True)
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
        scopes = self._pending_scopes
        if not scopes:
            self.readout.setPlainText("No compatible shown points to fit")
            return
        self._scope_count = len(scopes)
        self._analytical_complete = False
        self._cancelled = threading.Event()
        self._lifetime_token[0] = self._cancelled
        worker = _FitWorker(self._generation, scopes, self.settings(), self._cancelled, self._cache)
        self._workers[self._generation] = worker
        worker.signals.partial.connect(self._accept, QtCore.Qt.ConnectionType.QueuedConnection)
        worker.signals.finished.connect(self._finished, QtCore.Qt.ConnectionType.QueuedConnection)
        QtCore.QThreadPool.globalInstance().start(worker)

    @QtCore.Slot(object)
    def _finished(self, generation):
        self._workers.pop(generation, None)

    def _record_scope_metadata(self, record, scope):
        metadata = dict(label=scope["label"], view=self.view_name, uncertainty_branch=scope["branch"],
                        items=scope["items"], excluded=scope["excluded"], n_total=scope["n_total"],
                        excluded_count=scope["excluded_count"])
        changed = any(record.get(key) != value for key, value in metadata.items())
        record.update(metadata)
        return changed

    @QtCore.Slot(object)
    def _accept(self, payload):
        generation, results, errors = payload
        if generation != self._generation or not self.fit_button.isChecked():
            return
        records = []
        current = {scope["label"]: scope for scope in self._pending_scopes}
        for scope, fit, boot in results:
            scope = current.get(scope["label"], scope)
            record = fit_record(fit, boot)
            self._record_scope_metadata(record, scope)
            record.update(bootstrap_chunk_size=_chunk_size(self.resamples.value()),
                          bootstrap_seed_rule="seed + chunk index", requested_resamples=self.resamples.value(),
                          requested_seed=self.seed.value())
            for which in ("lower", "upper"):
                age = getattr(fit, which)
                if age:
                    record[which + "_note"] = age.note
            records.append(record)
        self.records = records
        self._live_records = records
        self._errors = list(errors)
        self._analytical_complete = len(results) + len(errors) >= self._scope_count
        self.show_records(errors)
        self._draw_records()
        self.recordsChanged.emit()

    def show_records(self, errors=()):
        blocks = []
        for r in self.records:
            anchor = "no anchor" if r.get("anchor") == "none" else r.get("anchor", "")
            lines = [f"{r.get('label')} · {r.get('n')} of {r.get('n_total')} ({r.get('excluded_count', len(r.get('excluded', [])))} excluded) · {model_name(r.get('model', 1))} (model {r.get('model')}) · {anchor}"]
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
        canvas = getattr(self.review, self.kind + "_concordia", None)
        if canvas is None:
            return
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
            old_plot, items = self._overlays[index]
            if old_plot is not plot:
                for item in items:
                    if item in old_plot.items:
                        old_plot.removeItem(item)
                self._overlays[index] = plot, items
            for item in items:
                if item not in plot.items:
                    plot.addItem(item, ignoreBounds=True)
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
            from GUI.concordia_geometry import sample_curve
            low, high = min(bounds), max(bounds)
            fit_x.extend([low, high])
            fit_y.extend([r["intercept"]+r["slope"]*low, r["intercept"]+r["slope"]*high])
            visible = plot.vb.viewRange()[0]
            left, right = max(low, visible[0]), min(high, visible[1])
            if left >= right:
                left, right = low, high
            # A straight fitted line is exact with two endpoints. Retain its
            # complete extent (including the T-W intercept at x=0) while only
            # the curved confidence envelopes need visible-range refinement.
            items[0].setData([low, high], [r["intercept"]+r["slope"]*low,
                                         r["intercept"]+r["slope"]*high])
            for item, sign in zip(items[1:3], (-1, 1)):
                def curve(xs, sign=sign):
                    ys = r["intercept"] + r["slope"]*xs
                    variance = (r["intercept_1s"]**2 + 2*xs*r["cov_intercept_slope"]
                                + xs**2*r["slope_1s"]**2)
                    return xs, ys + sign*1.96*np.sqrt(np.maximum(variance, 0))
                xs, ys = sample_curve(curve, left, right, plot.vb)
                item.setData(xs, ys)
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
            self.readout.setPlainText(f"Fit export failed: {exc}\nChoose a writable export folder and try again.")
