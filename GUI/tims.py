"""Tab-local TIMS markup. No marks are part of a reduction or project file."""

from dataclasses import dataclass, replace
import html
import time

from PySide6 import QtCore, QtWidgets
from shiboken6 import isValid

from GUI.database import database_availability
from UPbRPy.integration import upbdb as db
from UPbRPy.io import resolve_session_manifest
from UPbRPy.spot_edits import original_spot_label


@dataclass(frozen=True)
class TimsMark:
    fraction: dict
    source: dict
    grain_only: bool = False

    @property
    def badge(self):
        label = self.fraction.get("capsule_label")
        return ((str(label) + " · " if label else "") + self.fraction.get("name", "")
                + ("†" if self.source.get("is_fragment") else ""))

    @property
    def tooltip(self):
        return (self.badge + (" · " + self.fraction["capsule_set_name"] if self.fraction.get("capsule_set_name") else "")
                + (" · grain plucked via another spot" if self.grain_only else "")
                + " · " + self.fraction.get("status", "")
                + (" · " + self.source.get("fragment_note", "") if self.source.get("is_fragment") else ""))


def join_marks(acquisitions, fractions):
    """Index sources once, then join each acquisition by spot or grain identity."""
    spots, grains = {}, {}
    for fraction in fractions:
        if fraction.get("retired_at"):
            continue
        for source in fraction.get("sources", []):
            if source.get("retired_at"):
                continue
            if source.get("spot_id"):
                spots.setdefault(source["spot_id"], []).append((fraction, source))
            if source.get("grain_id"):
                grains.setdefault(source["grain_id"], []).append((fraction, source))
    result = {}
    for acquisition in acquisitions:
        matches = [TimsMark(f, s) for f, s in spots.get(acquisition.spot_id, [])]
        seen = {m.fraction["id"] for m in matches}
        for fraction, source in grains.get(acquisition.grain_id, []):
            if fraction["id"] not in seen:
                matches.append(TimsMark(fraction, source, True))
                seen.add(fraction["id"])
        if matches:
            result[acquisition.label] = matches
    return result


def badge_text(marks):
    """Show every fraction contributing to this spot or grain."""
    return ", ".join(mark.badge for mark in marks)


def tims_html(marks):
    """Format only data already carried by the fraction listing."""
    if not marks:
        return ""
    rows = ["<b>TIMS</b>"]
    for mark in marks:
        f, s = mark.fraction, mark.source
        capsule = " · ".join(str(v) for v in (f.get("capsule_label"), f.get("capsule_set_name")) if v)
        batch = ("batch started " + str(f["batch_started_on"]) if f.get("batch_started_on")
                 else "batch (start date unavailable)" if f.get("batch_id") else "no batch")
        values = [f.get("name", ""), capsule, batch, f.get("status", "")]
        if mark.grain_only:
            values.append("grain plucked via another spot")
        if s.get("is_fragment"):
            values.append("Fragment: " + s.get("fragment_note", ""))
        outcome = f.get("chemistry_outcome") or f.get("outcome")
        if outcome:
            values.append(str(outcome))
        rows.append("<br>".join(html.escape(str(v)) for v in values if v))
    return "<br>" + "<br>".join(rows)


def selection_sources(acquisitions):
    """Validate one sample and retain the first selected spot of each grain."""
    if not acquisitions or any(not a.sample_id or not (a.spot_id or a.grain_id) for a in acquisitions):
        raise ValueError("Selected spots need sample and spot/grain ids in the session manifest.")
    if len({a.sample_id for a in acquisitions}) != 1:
        names = sorted({a.sample_label or a.sample_id for a in acquisitions})
        raise ValueError("Select spots from one sample. Selected samples: " + ", ".join(names))
    sources, seen = [], set()
    for a in acquisitions:
        key = a.grain_id or a.spot_id
        if key in seen:
            continue
        seen.add(key)
        sources.append({**({"grain_id": a.grain_id} if a.grain_id else {}),
                        **({"spot_id": a.spot_id} if a.spot_id else {})})
    if len(sources) > 500:
        raise ValueError("A fraction can contain at most 500 sources.")
    return acquisitions[0].sample_id, sources


class TimsController(QtCore.QObject):
    """Fetch pages progressively through DatabaseLinks and decorate one tab."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.manifest = None
        self.marks = {}
        self._identity = None
        self._cache = {}
        self._generation = 0
        self._links = None

    def sync(self):
        window = self.window
        origin = window.session.origin or {}
        identity = (id(window.session), window.session.directory,
                    origin.get("server"), origin.get("session_id"))
        if identity == self._identity:
            return
        self._identity = identity
        self._generation += 1
        self.marks = {}
        self.manifest = None
        try:
            self.manifest = resolve_session_manifest(window.session.directory, None) if window.session.directory else None
        except (ValueError, OSError) as error:
            window.append_log(f"TIMS marks: {error}")
        self.apply()
        if origin.get("server") and origin.get("session_id"):
            links = window.database()
            if links is not self._links:
                if self._links is not None:
                    self._links.signed_in_changed.disconnect(self.account_changed)
                self._links = links
                links.signed_in_changed.connect(self.account_changed)
            self.refresh()

    def account_changed(self):
        self._cache.clear()
        self.marks = {}
        self.apply()
        self.refresh()

    def acquisition(self, name):
        raw = original_spot_label(self.window.session.experiment, name)
        return self.manifest.acquisition(raw) if self.manifest else None

    def disabled_reason(self, names):
        available, reason = database_availability()
        if not available:
            return reason
        origin = self.window.session.origin or {}
        if not (origin.get("server") and origin.get("session_id")):
            return "Link this project to a upbdb session first."
        rows = [self.acquisition(name) for name in names]
        if not rows or any(a is None or not (a.spot_id or a.grain_id) or not a.sample_id for a in rows):
            return "Selected spots have no sample and spot/grain ids in the session manifest."
        return ""

    def add_action(self, menu, names):
        names = list(names)
        action = menu.addAction("Mark for TIMS…", lambda: self.mark(names))
        reason = self.disabled_reason(names)
        action.setEnabled(not reason)
        action.setToolTip(reason or "Pluck the selected grains into one TIMS fraction")
        menu.setToolTipsVisible(True)
        return action

    def mark(self, names):
        reason = self.disabled_reason(names)
        if reason:
            return
        from GUI.tims_dialog import TimsDialog, normalize_mineral

        sample_names = self.window._spot_sample_map()
        rows = [replace(self.acquisition(name), sample_label=sample_names.get(name) or self.acquisition(name).sample_label)
                for name in names]
        try:
            selection_sources(rows)
        except ValueError as error:
            QtWidgets.QMessageBox.information(self.window, "Mark for TIMS", str(error))
            return
        session = self.window.session
        server = session.origin["server"]
        links = self.window.database()

        def show():
            if not isValid(self.window) or self.window.session is not session:
                return
            dialog = TimsDialog(server, rows, links,
                                mineral=normalize_mineral(self.manifest.material, self.window.workflow_preset.key),
                                parent=self.window)
            if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
                self.window.append_log(f"Marked for TIMS: {dialog.result_fraction['name']} ({len(rows)} spots).")
                self.refresh()
        links._authorize(server, "", show, "Mark for TIMS")

    def refresh(self):
        self._generation += 1
        generation = self._generation
        window = self.window
        session = window.session
        origin = session.origin or {}
        server = origin.get("server")
        if not server or not origin.get("session_id") or not self.manifest:
            return
        started = time.monotonic()
        self.marks = {}
        self.apply()
        if not database_availability()[0] or not db.is_signed_in(server):
            self._cache.clear()
            window.append_log("TIMS marks: database unavailable or not signed in.")
            return
        # Token identity isolates accounts without another account lookup request.
        identity = (db.normalize_server(server), db.stored_token(server))
        self._cache = {k: v for k, v in self._cache.items() if k[:2] == identity}
        samples = sorted({a.sample_id for a in self.manifest.acquisitions if a.sample_id})
        rows, dates, calls = [], {}, 0
        links = window.database()

        def valid():
            return isValid(window) and window.session is session and generation == self._generation

        def failed(error):
            if valid():
                self.marks = {}
                self.apply()
                window.append_log(f"TIMS marks: {error}")

        def publish():
            enriched = [{**row, **({"batch_started_on": dates[row["batch_id"]]}
                                  if row.get("batch_id") in dates else {})} for row in rows]
            self.marks = join_marks(self.manifest.acquisitions, enriched)
            self.apply()

        def fetch(index=0, cursor=None, seen=frozenset(), *, batches=False):
            nonlocal calls
            if not valid():
                return
            if index >= len(samples) and not batches:
                fetch(batches=True)
                return
            if index >= len(samples):
                window.append_log(f"TIMS marks: {len(rows)} fractions, {calls} requests in {time.monotonic()-started:.2f} s.")
                return
            key = (*identity, "batches" if batches else "fractions", samples[index], cursor)
            cached = self._cache.get(key)
            calls += 1

            def done(page):
                if not valid():
                    return
                if page.not_modified:
                    if cached is None:
                        failed(ValueError("TIMS returned 304 without a cached page."))
                        return
                    page = cached
                self._cache[key] = page
                if batches:
                    dates.update({row["id"]: row["started_on"] for row in page if not row.get("retired_at")})
                else:
                    rows.extend(page)
                publish()
                nxt = page.next_cursor
                if nxt and nxt in seen:
                    failed(ValueError("TIMS repeated a page cursor."))
                elif nxt:
                    fetch(index, nxt, seen | {nxt}, batches=batches)
                else:
                    fetch(index + 1, batches=batches)
            read = (lambda: db.list_sample_batches(server, samples[index], cursor=cursor,
                                                   etag=cached.etag if cached is not None else None)) if batches else (
                    lambda: db.list_fractions(server, sample=samples[index], cursor=cursor,
                                              etag=cached.etag if cached is not None else None))
            links.run(read, done,
                      title="TIMS marks", on_failure=failed)
        fetch()

    def display_marks(self):
        return {name: self.marks.get(original_spot_label(self.window.session.experiment, name), [])
                for name in self.window.session.spot_names}

    def apply(self):
        from GUI.widgets import SPOT_TREE_TIMS_ROLE, SPOT_TREE_TIMS_GRAIN_ROLE, SPOT_TREE_TIMS_TOOLTIP_ROLE

        window = self.window
        if not hasattr(window, "spot_list"):
            return
        origin = window.session.origin or {}
        linked = bool(origin.get("server") and origin.get("session_id"))
        marks = self.display_marks()
        with QtCore.QSignalBlocker(window.spot_list):
            iterator = QtWidgets.QTreeWidgetItemIterator(window.spot_list)
            while iterator.value() is not None:
                item = iterator.value()
                matched = marks.get(item.data(0, QtCore.Qt.ItemDataRole.UserRole), []) if linked else []
                item.setData(0, SPOT_TREE_TIMS_ROLE, badge_text(matched) or None)
                item.setData(0, SPOT_TREE_TIMS_GRAIN_ROLE, all(m.grain_only for m in matched) if matched else None)
                item.setData(0, SPOT_TREE_TIMS_TOOLTIP_ROLE, "\n".join(m.tooltip for m in matched) or None)
                iterator += 1
        window.results_table.set_tims_marks(marks, linked=linked)
