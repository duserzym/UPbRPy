"""Mark selected grains for TIMS using the shared Studio/GrainAtlas rules."""

import ast
from datetime import date
import json
import re
import uuid
from urllib.parse import urlsplit

from PySide6 import QtCore, QtWidgets
from shiboken6 import isValid

from GUI.tims import selection_sources
from UPbRPy.integration import upbdb as db

MINERALS = ("zircon", "baddeleyite", "titanite", "rutile", "apatite", "monazite", "xenotime", "other")
NAME_PATTERN = r"[a-z]{1,3}[0-9]{1,3}[a-z]?"
LAST_SET = "tims/last_capsule_set"


def normalize_mineral(*values):
    """Use the first recognized session or workflow mineral, else zircon."""
    return next((name for value in values if (name := str(value or "").strip().casefold()) in MINERALS), "zircon")


def _error_detail(error):
    """Read structured details, including Django's stringified message dict."""
    try:
        body = json.loads(getattr(error, "details", ""))
    except (TypeError, ValueError):
        return None
    detail = body.get("detail") if isinstance(body, dict) else body
    if isinstance(detail, str) and detail.strip().startswith(("{", "[")):
        try:
            parsed = ast.literal_eval(detail)
        except (ValueError, SyntaxError, RecursionError):
            pass
        else:
            if isinstance(parsed, (dict, list)):
                detail = parsed
    return detail


def _detail_text(detail, field=""):
    """Flatten validation fields and their messages without container reprs."""
    if isinstance(detail, dict):
        return "\n".join(_detail_text(value, ".".join(filter(None, (field, str(key) if key != "__all__" else ""))))
                         for key, value in detail.items())
    if isinstance(detail, list):
        return "\n".join(_detail_text(value, field) for value in detail)
    return (field + ": " if field else "") + str(detail)


def _constraint_error(error, name):
    return (isinstance(error, db.RequestRejected)
            and bool(re.search(r"\b" + re.escape(name) + r"\b", _detail_text(_error_detail(error)))))


class TimsDialog(QtWidgets.QDialog):
    """Load choices and save on DatabaseLinks' worker pool; retain failed inputs."""

    def __init__(self, server, acquisitions, links, *, mineral="zircon", parent=None):
        super().__init__(parent)
        self.sample_id, self.sources = selection_sources(acquisitions)
        self.sample_label = acquisitions[0].sample_label or self.sample_id
        self.server, self.links = server, links
        self.result_fraction = None
        self.started_on = date.today().isoformat()
        self._generation = 0
        self._closed = False
        self._busy = False
        self._writing = False
        self._name_typed = False
        self._taken = set()
        self._batch_writes = {}
        self._fraction_writes = {}
        self._loaded = False
        self.setWindowTitle("Mark for TIMS…")
        self.resize(520, 560)
        layout = QtWidgets.QVBoxLayout(self)
        summary = QtWidgets.QLabel(f"{acquisitions[0].sample_label or self.sample_id} · {len(self.sources)} {'grain' if len(self.sources) == 1 else 'grains'}")
        summary.setWordWrap(True)
        layout.addWidget(summary)
        self.fields = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(self.fields)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.capsule_set = QtWidgets.QComboBox()
        self.batch = QtWidgets.QComboBox()
        self.capsule = QtWidgets.QSpinBox()
        self.capsule.setRange(1, 15)
        self.capsule_preview = QtWidgets.QLabel()
        self.taken_label = QtWidgets.QLabel()
        self.taken_label.setWordWrap(True)
        capsule_row = QtWidgets.QWidget()
        capsule_row.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Fixed)
        capsule_layout = QtWidgets.QHBoxLayout(capsule_row)
        capsule_layout.setContentsMargins(0, 0, 0, 0)
        capsule_layout.addWidget(self.capsule)
        capsule_layout.addWidget(self.capsule_preview)
        capsule_layout.addWidget(self.taken_label, 1)
        self.fraction_name = QtWidgets.QLineEdit()
        self.mineral = QtWidgets.QComboBox()
        self.mineral.addItems(MINERALS)
        self.mineral.setCurrentText(normalize_mineral(mineral))
        self.fragment = QtWidgets.QCheckBox("Fragment")
        self.fragment_note = QtWidgets.QLineEdit()
        self.fragment_note.setEnabled(False)
        self.notes = QtWidgets.QPlainTextEdit()
        self.notes.setMaximumHeight(90)
        for label, widget in (("Capsule set", self.capsule_set), ("Batch", self.batch),
                              ("Capsule", capsule_row), ("Fraction name", self.fraction_name),
                              ("Mineral", self.mineral), ("", self.fragment),
                              ("Fragment note", self.fragment_note), ("Notes", self.notes)):
            form.addRow(label, widget)
        layout.addWidget(self.fields)
        self.message = QtWidgets.QLabel()
        self.message.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.message)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save |
                                                  QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.save_button = self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save)
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.reload_button = self.buttons.addButton("Reload choices", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self.reload_button.clicked.connect(self.load)
        self.sign_in_button = self.buttons.addButton("Sign in…", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self.sign_in_button.clicked.connect(lambda: self.links._authorize(self.server, "", self.load, "Mark for TIMS"))
        self.capsule_set.currentIndexChanged.connect(self._set_changed)
        self.batch.currentIndexChanged.connect(self._batch_changed)
        self.capsule.valueChanged.connect(self._capsule_changed)
        self.fraction_name.textEdited.connect(self._edited_name)
        self.mineral.currentTextChanged.connect(self._mineral_changed)
        self.fragment.toggled.connect(self.fragment_note.setEnabled)
        self.finished.connect(self._finished)
        self._set_busy(True, "Loading TIMS choices…")
        QtCore.QTimer.singleShot(0, self, self.load)

    def _finished(self, *_args):
        self._closed = True
        self._generation += 1

    def reject(self):
        if not self._writing:
            super().reject()

    def _edited_name(self, *_args):
        self._name_typed = True

    def _set_busy(self, busy, message=""):
        self._busy = busy
        self.fields.setEnabled(not busy)
        self.save_button.setEnabled(not busy and self._loaded)
        self.reload_button.setEnabled(not busy)
        self.sign_in_button.setEnabled(not busy)
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setEnabled(not self._writing)
        self.message.setText(message)

    def _run(self, function, done, message, *, on_failure=None):
        self._generation += 1
        generation = self._generation
        self._set_busy(True, message)

        def valid():
            return isValid(self) and not self._closed and generation == self._generation

        def success(value):
            if valid():
                self._set_busy(False)
                done(value)

        def failed(error):
            if valid():
                self._writing = False
                self._set_busy(False, self._error_text(error))
                if on_failure is not None:
                    on_failure(error)
        self.links.run(function, success, title="Mark for TIMS", on_failure=failed)

    def _error_text(self, error):
        if _constraint_error(error, "tims_fraction_sample_name"):
            name = self.fraction_name.text().strip().lower()
            return f"Fraction name {name} is already used for sample {self.sample_label}. Choose another name."
        if _constraint_error(error, "tims_fraction_capsule"):
            return f"Capsule {self.capsule_preview.text()} is already taken in this batch. Choose a free capsule."
        if isinstance(error, db.ConnectionFailed):
            host = urlsplit(self.server).netloc or self.server
            saved_batch = any(write.get("result") for write in self._batch_writes.values())
            return (f"upbdb at {host} did not answer; nothing was saved. Try again when you are online."
                    if not saved_batch else
                    f"upbdb at {host} did not answer; the batch was created but the fraction is unconfirmed. Try Save again when you are online.")
        status = getattr(error, "status", None)
        detail = _error_detail(error)
        text = _detail_text(detail) if isinstance(detail, (dict, list)) else str(error)
        return (f"HTTP {status}: " if status else "") + text

    def _refresh_capsules(self, batch_id, message):
        """Revalidate the conflicted batch on the pool, keeping its refusal visible."""
        def done(detail):
            self._apply_batch(detail)
            availability = self.message.text()
            self.message.setText(message + ("\n" + availability if availability else ""))

        def failed(error):
            self.message.setText(message + "\nCould not refresh capsules: " + self._error_text(error))
        self._run(lambda: db.get_batch(self.server, batch_id), done, message, on_failure=failed)

    def load(self):
        if self._closed or self._writing:
            return
        self._loaded = False
        def done(sets):
            with QtCore.QSignalBlocker(self.capsule_set):
                self.capsule_set.clear()
                multiple = len({row['lab_id'] for row in sets}) > 1
                for row in sets:
                    lab = row.get("lab_name") or row.get("lab", {}).get("name") or row["lab_id"]
                    label = f"{row['name']} ({row['code']})" + (f" · {lab}" if multiple else "")
                    self.capsule_set.addItem(label, row)
                last = self.links.settings.value(LAST_SET, "", type=str)
                index = next((i for i, row in enumerate(sets) if row["id"] == last), 0)
                self.capsule_set.setCurrentIndex(index if sets else -1)
            if not sets:
                self.message.setText("No active capsule sets are available for your account.")
                return
            self._set_changed()
        self._run(lambda: db.list_capsule_sets(self.server), done, "Loading capsule sets…")

    def _set_changed(self, *_args):
        capsule_set = self.capsule_set.currentData()
        if not capsule_set:
            return
        mineral = self.mineral.currentText()
        def fetch():
            batches = db.list_batches(self.server, capsule_set["id"])
            batches = sorted((b for b in batches if not b.get("retired_at")),
                             key=lambda b: (b["started_on"], b.get("id", "")), reverse=True)
            detail = db.get_batch(self.server, batches[0]["id"]) if batches else None
            suggestion = db.suggest_fraction_name(self.server, self.sample_id, mineral) if not self._name_typed else None
            return batches, detail, suggestion
        def done(result):
            batches, detail, suggestion = result
            self.capsule.setRange(1, capsule_set["positions"])
            with QtCore.QSignalBlocker(self.batch):
                self.batch.clear()
                self.batch.addItem(f"New batch (started {self.started_on})", None)
                for batch in batches:
                    operator = batch.get("operator_name")
                    self.batch.addItem(f"{capsule_set['code']} batch started {batch['started_on']}" +
                                       (f" · {operator}" if operator else ""), batch)
                self.batch.setCurrentIndex(1 if batches else 0)
            if suggestion is not None and not self._name_typed:
                self.fraction_name.setText(suggestion)
            self._apply_batch(detail)
            self._loaded = True
            self._set_busy(False)
        self._run(fetch, done, "Loading batches and suggested name…")

    def _batch_changed(self, *_args):
        batch = self.batch.currentData()
        if not batch:
            self._apply_batch(None)
            return
        self._run(lambda: db.get_batch(self.server, batch["id"]), self._apply_batch,
                  "Loading capsule map…")

    def _apply_batch(self, detail):
        self._taken = {int(number) for number, value in (detail or {}).get("capsule_map", {}).items() if value}
        self.taken_label.setText("taken: " + (", ".join(map(str, sorted(self._taken))) or "none"))
        free = next((n for n in range(1, self.capsule.maximum() + 1) if n not in self._taken), None)
        self.capsule.setValue(free or 1)
        self._capsule_changed()
        if free is None:
            self.message.setText("This batch is full. Choose another batch or New batch.")

    def _capsule_changed(self, *_args):
        row = self.capsule_set.currentData() or {}
        self.capsule_preview.setText(f"{row.get('code', '')}{self.capsule.value()}")

    def _mineral_changed(self, mineral):
        if self._name_typed or not self._loaded:
            return
        def done(name):
            if not self._name_typed:
                self.fraction_name.setText(name)
        self._run(lambda: db.suggest_fraction_name(self.server, self.sample_id, mineral),
                  done, "Suggesting fraction name…")

    def payload(self):
        """Validate local fields and return the fraction's immutable save inputs."""
        name = self.fraction_name.text().strip().lower()
        if not re.fullmatch(NAME_PATTERN, name):
            raise ValueError("Fraction name must match ^[a-z]{1,3}[0-9]{1,3}[a-z]?$ (for example z1).")
        if self.capsule.value() in self._taken:
            raise ValueError("This capsule is taken. Choose a free capsule.")
        fragment_note = self.fragment_note.text().strip()
        if self.fragment.isChecked() and not fragment_note:
            raise ValueError("A fragment note is required.")
        if not self.capsule_set.currentData():
            raise ValueError("Choose an active capsule set.")
        return {"sample_id": self.sample_id, "name": name, "mineral": self.mineral.currentText(),
                "capsule_number": self.capsule.value(), "status": "picked", "notes": self.notes.toPlainText(),
                "sources": [{**source, "is_fragment": self.fragment.isChecked(),
                             "fragment_note": fragment_note if self.fragment.isChecked() else ""}
                            for source in self.sources]}

    def save(self):
        if self._busy or not self._loaded:
            return
        try:
            payload = self.payload()
        except ValueError as error:
            self.message.setText(str(error))
            return
        self.fraction_name.setText(payload["name"])
        capsule_set = dict(self.capsule_set.currentData())
        batch = self.batch.currentData()
        batch_payload = {"capsule_set_id": capsule_set["id"], "started_on": self.started_on}
        # Retain the same batch attempt even if the fraction inputs are corrected.
        batch_identity = json.dumps({**batch_payload, "mineral": payload["mineral"]}, sort_keys=True)
        batch_write = self._batch_writes.setdefault(batch_identity, {"key": str(uuid.uuid4())})
        identity = json.dumps({**payload, "batch": batch["id"] if batch else batch_identity}, sort_keys=True)
        key = self._fraction_writes.setdefault(identity, str(uuid.uuid4()))
        attempt = {}
        self._writing = True
        def write():
            if batch:
                batch_id = batch["id"]
            else:
                if "payload" not in batch_write:
                    recipes = db.list_recipes(self.server, lab_id=capsule_set["lab_id"], mineral=payload["mineral"])
                    recipes = [r for r in recipes if r.get("active", True)]
                    batch_write["payload"] = {**batch_payload, **({"recipe_id": recipes[0]["id"]} if len(recipes) == 1 else {})}
                if "result" not in batch_write:
                    batch_write["result"] = db.create_batch(self.server, batch_write["payload"], idempotency_key=batch_write["key"])
                batch_id = batch_write["result"]["id"]
            attempt["batch_id"] = batch_id
            return db.create_fraction(self.server, {**payload, "batch_id": batch_id}, idempotency_key=key)
        def failed(error):
            if _constraint_error(error, "tims_fraction_capsule") and attempt.get("batch_id"):
                self._refresh_capsules(attempt["batch_id"], self._error_text(error))
        def done(fraction):
            self._writing = False
            self.result_fraction = fraction
            self.links.settings.setValue(LAST_SET, capsule_set["id"])
            self.accept()
        self._run(write, done, "Saving TIMS fraction…", on_failure=failed)
