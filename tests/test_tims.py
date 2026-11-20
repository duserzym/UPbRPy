"""TIMS markup is independent of reductions, with bounded server reads."""
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from UPbRPy.session_manifest import SessionAcquisition, read_session_manifest
from UPbRPy.integration import upbdb as db
from fake_upbdb import Call, FakeState, Handler
from GUI.widgets import SPOT_TREE_TIMS_ROLE, SPOT_TREE_TIMS_GRAIN_ROLE, SPOT_TREE_TIMS_TOOLTIP_ROLE


def acquisition(label="A_1", **kwargs):
    return SessionAcquisition(label, spot_id="spot-1", grain_id="grain-1", sample_id="sample-1", sample_label="A", **kwargs)


def fraction(name="z3", **kwargs):
    return {"id": name, "name": name, "sample": {"id": "sample-1", "name": "A"},
            "capsule_label": "K3", "capsule_set_name": "Krogh", "batch_id": "batch-1",
            "capsule_number": 3, "status": "picked",
            "sources": [{"spot_id": "spot-1", "grain_id": "grain-1"}], **kwargs}


def test_manifest_ids(tmp_path):
    path = tmp_path / "session_manifest.toml"
    path.write_text('[[acquisition]]\nlabel="A_1"\nspot_id="s"\ngrain_id="g"\nsample_id="a"\natlas_spot_id="as"\n[[acquisition]]\nlabel="old"\n')
    rows = read_session_manifest(path).acquisitions
    assert (rows[0].spot_id, rows[0].grain_id, rows[0].sample_id, rows[0].atlas_spot_id) == ("s", "g", "a", "as")
    assert rows[1].atlas_spot_id == rows[1].spot_id == ""


def test_join_sources_fragments_and_badges():
    from GUI.tims import badge_text, join_marks, selection_sources
    a = acquisition()
    other = replace(a, label="A_2", spot_id="spot-2")
    unrelated = replace(a, label="B_1", spot_id="spot-3", grain_id="grain-3")
    f = fraction(sources=[{"spot_id": "spot-1", "grain_id": "grain-1", "is_fragment": True, "fragment_note": "half"},
                          {"spot_id": "spot-3", "grain_id": "grain-3", "is_fragment": True, "fragment_note": "half"}])
    marks = join_marks([a, other, unrelated], [f, fraction("z4", capsule_label=None, batch_id=None),
                                             fraction("z5", retired_at="today")])
    assert badge_text(marks[a.label]) == "K3 · z3†, z4"
    assert not marks[a.label][0].grain_only
    assert marks[other.label][0].grain_only
    assert "grain plucked" in marks[other.label][0].tooltip
    assert badge_text(marks[unrelated.label]) == "K3 · z3†"
    assert selection_sources([a, other])[1] == [{"grain_id": "grain-1", "spot_id": "spot-1"}]
    with pytest.raises(ValueError, match="A, B"):
        selection_sources([a, replace(unrelated, sample_id="sample-2", sample_label="B")])


@pytest.fixture
def server(monkeypatch):
    """Exercise the fake HTTP handlers in process, including in the sandbox."""
    from urllib.parse import urlencode, urlsplit
    state = FakeState()
    fake = SimpleNamespace(url="http://localhost:8765", state=state, threads=[])
    def request(method, url, *, headers, params=None, json=None, **kwargs):
        import json as codec
        import threading

        fake.threads.append(threading.get_ident())
        path = urlsplit(url).path + (("?" + urlencode(params)) if params else "")
        call = Call(method, path, headers, codec.dumps(json).encode() if json is not None else b"", json)
        state.calls.append(call)
        handler = Handler.__new__(Handler)
        handler.state = state
        handler.headers = headers
        handler._record = lambda: call
        reply = {}
        def send(status, payload=None, *, headers=None, raw=None):
            reply.update(status_code=status, headers=headers or {}, text=codec.dumps(payload), json=lambda: payload)
        handler._send = send
        getattr(handler, "do_" + method)()
        return SimpleNamespace(**reply)
    monkeypatch.setattr(db, "_requests", lambda: SimpleNamespace(request=request,
                         exceptions=SimpleNamespace(Timeout=TimeoutError, RequestException=ConnectionError)))
    previous = db.set_token_store(db.MemoryTokenStore())
    db.store_token(fake.url, state.issue_token())
    yield fake
    db.set_token_store(previous)


def test_client_paging_etag_and_scale(server):
    server.state.fractions = [fraction(f"z{i}") for i in range(3000)]
    started = time.monotonic()
    page = db.list_fractions(server.url, sample="sample-1", limit=500)
    assert len(page) == 500 and page.next_cursor == "500" and page.etag
    cached = db.list_fractions(server.url, sample="sample-1", etag=page.etag)
    assert cached.not_modified and not cached
    rows = db.all_fractions(server.url, sample="sample-1")
    assert len(rows) == 3000
    assert len(server.state.calls) == 8
    assert "after=500" in server.state.calls[3].path
    print(f"TIMS 3000 rows: 6 paged reads, one initial read + one 304, {time.monotonic()-started:.3f} s; before: 0 TIMS calls")


def test_client_choices_writes_and_rejections(server):
    sets = db.list_capsule_sets(server.url)
    assert [s["name"] for s in sets] == ["Becquerel", "Curie", "Holmes", "Wilson", "Krogh", "Aston", "Nier", "Ussher", "Rutherford", "Soddy"]
    batch_payload = {"capsule_set_id": "krogh", "started_on": "2026-11-20"}
    batch = db.create_batch(server.url, batch_payload, idempotency_key="batch-key")
    assert db.create_batch(server.url, batch_payload, idempotency_key="batch-key") == batch
    assert db.list_batches(server.url, "krogh")[0]["id"] == batch["id"]
    assert db.get_batch(server.url, batch["id"])["capsule_map"]["1"] is None
    assert db.list_recipes(server.url, lab_id="lab-1", mineral="zircon") == []
    assert db.suggest_fraction_name(server.url, "sample-1", "zircon") == "z1"
    payload = {"name": "z1", "sample_id": "sample-1", "mineral": "zircon", "batch_id": batch["id"],
               "capsule_number": 1, "sources": [{"grain_id": "grain-1", "spot_id": "spot-1"}]}
    f = db.create_fraction(server.url, payload, idempotency_key="fraction-key")
    assert db.create_fraction(server.url, payload, idempotency_key="fraction-key") == f
    assert len(server.state.fractions) == 1 and len(server.state.batches) == 1
    assert db.suggest_fraction_name(server.url, "sample-1", "zircon") == "z2"
    for changed, detail in (({"capsule_number": 2, "sources": []}, "Duplicate fraction name"),
                            ({"name": "z2", "sources": []}, "Capsule is taken")):
        with pytest.raises(db.RequestRejected) as caught:
            db.create_fraction(server.url, {**payload, **changed}, idempotency_key=detail)
        assert detail in str(caught.value) and caught.value.code == "validation_failed" and caught.value.status == 422
    for status in (409, 422):
        server.state.fail_next["/v1/fractions"] = (status, "Concurrent capsule conflict")
        with pytest.raises(db.RequestRejected, match="Concurrent capsule conflict") as caught:
            db.create_fraction(server.url, payload, idempotency_key=f"fail-{status}")
        assert caught.value.status == status
    assert all(c.headers.get("Idempotency-Key") for c in server.state.calls if c.method == "POST")


from test_database_gui import app, wait_for, controller, dialogs  # noqa: E402,F401


@pytest.fixture
def links(app, tmp_path):
    from GUI.database import DatabaseLinks
    from PySide6 import QtCore
    links = DatabaseLinks(open_bundle=lambda b: None, open_project=lambda p: None, parent_widget=lambda: None,
                          settings=QtCore.QSettings(str(tmp_path / "tims.ini"), QtCore.QSettings.Format.IniFormat))
    yield links
    assert wait_for(lambda: not links._workers)


def ready_dialog(server, links, mineral="zircon"):
    from GUI.tims_dialog import TimsDialog
    dialog = TimsDialog(server.url, [acquisition()], links, mineral=mineral)
    assert wait_for(lambda: not dialog._busy)
    assert dialog._loaded, dialog.message.text()
    return dialog


def test_dialog_defaults_validation_and_suggestions(server, links):
    from GUI.tims_dialog import LAST_SET
    server.state.batches["old"] = {"id": "old", "capsule_set_id": "krogh", "started_on": "2026-01-01"}
    server.state.batches["new"] = {"id": "new", "capsule_set_id": "krogh", "started_on": "2026-11-20", "operator_name": "Analyst"}
    server.state.fractions = [fraction(batch_id="new", capsule_number=1), fraction("z4", batch_id="new", capsule_number=2)]
    links.settings.setValue(LAST_SET, "krogh")
    d = ready_dialog(server, links)
    assert d.capsule_set.currentText() == "Krogh (K)"
    assert d.batch.currentData()["id"] == "new"
    assert d.batch.itemText(0).startswith("New batch (started ")
    assert d.capsule.value() == 3 and d.capsule_preview.text() == "K3"
    assert d.taken_label.text() == "taken: 1, 2"
    assert d.fraction_name.text() == "z5"
    d.mineral.setCurrentText("apatite")
    assert wait_for(lambda: not d._busy)
    assert d.fraction_name.text() == "a1"
    d.fraction_name.setText("own2")
    d.fraction_name.textEdited.emit("own2")
    d.mineral.setCurrentText("rutile")
    assert d.fraction_name.text() == "own2"
    for name in ("Z1", "ab999c", "abc1"):
        d.fraction_name.setText(name)
        assert d.payload()["name"] == name.lower()
    for name in ("a", "abcd1", "a1000", "a1zz", "1z", "z_1"):
        d.fraction_name.setText(name)
        with pytest.raises(ValueError, match="Fraction name"):
            d.payload()
    d.fraction_name.setText("z8")
    d.fragment.setChecked(True)
    with pytest.raises(ValueError, match="fragment note"):
        d.payload()
    d.fragment_note.setText("half grain")
    assert d.payload()["sources"][0]["fragment_note"] == "half grain"
    d.capsule.setValue(1)
    with pytest.raises(ValueError, match="taken"):
        d.payload()
    assert d.sizeHint().height() < 750 and d.minimumSizeHint().width() < 1280
    d.reject()


@pytest.mark.parametrize("material, expected", [(" Apatite ", "apatite"), ("Zircon", "zircon")])
def test_dialog_mineral_normalization(server, links, material, expected):
    from GUI.tims_dialog import normalize_mineral
    assert normalize_mineral(material) == expected
    assert normalize_mineral("unrecognized", material) == expected
    assert normalize_mineral(None, "unrecognized") == "zircon"
    d = ready_dialog(server, links, mineral=material)
    assert d.mineral.currentText() == expected
    assert d.fraction_name.text() == ("a1" if expected == "apatite" else "z1")
    d.reject()


def test_dialog_save_sequence_and_same_key_retry(server, links, monkeypatch):
    from GUI.tims_dialog import LAST_SET
    links.settings.setValue(LAST_SET, "krogh")
    server.state.recipes = [{"id": "recipe-1", "lab_id": "lab-1", "mineral": "zircon", "active": True}]
    d = ready_dialog(server, links)
    original = db.create_fraction
    keys = []
    def fail_after_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        keys.append(kwargs["idempotency_key"])
        if len(keys) == 1:
            raise db.ConnectionFailed("lost reply")
        return result
    monkeypatch.setattr(db, "create_fraction", fail_after_commit)
    d.save()
    assert wait_for(lambda: not d._busy)
    assert "did not answer" in d.message.text()
    assert d.fraction_name.text() == "z1" and len(server.state.batches) == 1
    d.save()
    assert wait_for(lambda: d.result_fraction is not None)
    assert len(server.state.batches) == len(server.state.fractions) == 1
    posts = [c for c in server.state.calls if c.method == "POST"]
    assert [c.path for c in posts] == ["/v1/tims/batches", "/v1/fractions", "/v1/fractions"]
    assert keys[0] == keys[1] and posts[1].headers["Idempotency-Key"] == posts[2].headers["Idempotency-Key"]
    assert posts[0].json["recipe_id"] == "recipe-1"
    assert d.result_fraction["status"] == "picked"


def test_dialog_rejection_keeps_inputs(server, links):
    d = ready_dialog(server, links)
    server.state.fail_next["/v1/fractions"] = (422, "Duplicate fraction name")
    d.save()
    assert wait_for(lambda: not d._busy)
    assert "Duplicate fraction name" in d.message.text()
    assert not d.result_fraction and not d._closed and d.fraction_name.text() == "z1"
    d.reject()


@pytest.mark.parametrize("constraint", ["tims_fraction_sample_name", "tims_fraction_capsule"])
@pytest.mark.parametrize("stringified", [False, True])
def test_dialog_real_constraint_errors(server, links, constraint, stringified):
    from GUI.tims_dialog import LAST_SET, TimsDialog

    links.settings.setValue(LAST_SET, "krogh")
    server.state.batches["batch-1"] = {"id": "batch-1", "capsule_set_id": "krogh", "started_on": "2026-11-20"}
    d = TimsDialog(server.url, [replace(acquisition(), sample_label="HJ-gc1")], links)
    assert wait_for(lambda: d._loaded and not d._busy)
    assert d.capsule.value() == 1 and d.fraction_name.text() == "z1"
    calls_before = len(server.state.calls)
    # Another user occupies K1 after this dialog loaded its capsule map.
    if constraint == "tims_fraction_capsule":
        server.state.fractions.append(fraction("z8", capsule_number=1))
    detail = {"__all__": [f"Constraint “{constraint}” is violated."]}
    body = {"detail": str(detail) if stringified else detail, "code": "validation_failed"}
    server.state.fail_next["/v1/fractions"] = (422, body)
    started = time.monotonic()
    d.save()
    assert wait_for(lambda: not d._busy)
    calls = server.state.calls[calls_before:]
    if constraint == "tims_fraction_sample_name":
        assert d.message.text() == "Fraction name z1 is already used for sample HJ-gc1. Choose another name."
        assert len(calls) == 1 and d.capsule.value() == 1
    else:
        assert d.message.text() == "Capsule K1 is already taken in this batch. Choose a free capsule."
        assert d.taken_label.text() == "taken: 1" and d.capsule.value() == 2
        assert d.capsule_preview.text() == "K2"
        assert [c.method for c in calls] == ["POST", "GET"]
        assert calls[-1].path == "/v1/tims/batches/batch-1"
        assert calls[-1].headers.get("If-None-Match")
        print(f"Capsule refusal: before 1 POST; after 1 POST + 1 conditional GET, {time.monotonic()-started:.3f} s")
    assert d.fraction_name.text() == "z1" and not d._closed and d.result_fraction is None
    assert d.save_button.isEnabled()
    d.reject()


@pytest.mark.parametrize("detail, expected", [
    ({"name": ["Required.", "Invalid."], "sources": {"fragment_note": ["Explain the fragment."]}},
     "name: Required.\nname: Invalid.\nsources.fragment_note: Explain the fragment."),
    ([{"name": ["Required."]}, {"mineral": ["Invalid."]}], "name: Required.\nmineral: Invalid."),
])
def test_dialog_structured_validation_detail(server, links, detail, expected):
    d = ready_dialog(server, links)
    server.state.fail_next["/v1/fractions"] = (422, {"detail": detail, "code": "validation_failed"})
    d.save()
    assert wait_for(lambda: not d._busy)
    assert d.message.text() == "HTTP 422: " + expected
    assert not d._closed
    d.reject()


def test_card_review_column_filter_and_selection(app):
    from GUI.widgets import ResultsTableWidget, SpotInfoCard
    from GUI.tims import join_marks
    marks = join_marks([acquisition()], [fraction(chemistry_outcome="dissolved in CA", sources=[
        {"grain_id": "grain-1", "spot_id": "spot-1", "is_fragment": True, "fragment_note": "half <grain>"}])])
    card = SpotInfoCard()
    card.show_spot("A_1", {"preferred_date_method": "206Pb/238U"}, {}, excluded=False, tims_marks=marks["A_1"])
    for text in ("TIMS", "z3", "K3 · Krogh", "picked", "half &lt;grain&gt;", "dissolved in CA", "start date unavailable"):
        assert text in card.body.text()
    assert card.body.text().index("</table>") < card.body.text().index("<b>TIMS</b>") < card.body.text().index("* The method")
    assert "TIMS: K3 · z3† · picked" in card.compact_body.text()
    assert not any(text in card.compact_body.text() for text in ("half", "dissolved", "Krogh", "batch"))
    card.show_spot("other", {}, {}, excluded=False)
    assert "TIMS" not in card.body.text() and "TIMS" not in card.compact_body.text()
    review = ResultsTableWidget()
    frame = pd.DataFrame({"spot_name": ["A_1", "B_1"], "sample": ["A", "B"]})
    original = frame.copy(deep=True)
    finalized = SimpleNamespace(selected_results=frame, parallel_results=frame)
    review.show_results(finalized)
    review.set_selected_spots(["A_1"])
    flags = dict(review.include_flags)
    review.set_tims_marks(marks)
    assert review.selected_spots == {"A_1"} and review.include_flags == flags
    assert review.results_model.frame["TIMS"].tolist() == ["K3 · z3†", ""]
    for grouped in (False, True):
        review.results_group_toggle.setChecked(grouped)
        view = review.results_tree if grouped else review.results_table
        review.plucked_only.setChecked(True)
        assert review._visible_spot_names(view) == {"A_1"}
        review.results_filter.setText("B")
        assert not review._visible_spot_names(view)
        review.results_filter.clear()
        review.plucked_only.setChecked(False)
        assert review._visible_spot_names(view) == {"A_1", "B_1"}
    pd.testing.assert_frame_equal(frame, original)
    review.close()
    card.close()


@pytest.mark.parametrize("mark_before_results", [False, True])
def test_review_tims_position_width_and_frozen_group(app, mark_before_results):
    from PySide6 import QtCore, QtWidgets
    from GUI.tims import join_marks
    from GUI.widgets import RESULTS_FROZEN_COLUMNS, ResultsTableWidget, results_column_group

    frame = pd.DataFrame({"sample": ["A"] * 324,
                          "spot_name": ["A_1", *[f"A_{n}" for n in range(2, 325)]],
                          **{f"result_{n}": range(324) for n in range(30)}})
    original = frame.copy(deep=True)
    finalized = SimpleNamespace(selected_results=frame, parallel_results=frame)
    review = ResultsTableWidget()
    review.resize(1000, 700)
    review.show_results(finalized)
    review.tabs.setCurrentWidget(review.results_page)
    review.show()
    assert wait_for(lambda: not review._pending_column_sizes)
    table, frozen = review.results_table, review.results_frozen
    horizontal = QtCore.Qt.Orientation.Horizontal
    headers = lambda view: [view.model().headerData(n, horizontal) for n in range(view.model().columnCount())]
    unlinked_headers = headers(table)
    unlinked_widths = [table.columnWidth(n) for n in range(table.model().columnCount())]
    unlinked_frozen_width = frozen.frozen_width()
    assert [unlinked_headers[n] for n in frozen.frozen_columns()] == ["Shown", "sample", "spot_name"]
    assert review.plucked_only.isHidden()

    marks = join_marks([acquisition()], [fraction("z13", capsule_label="K12", sources=[
        {"grain_id": "grain-1", "spot_id": "spot-1", "is_fragment": True, "fragment_note": "half"}])])
    review.set_tims_marks(marks)
    if mark_before_results:
        review.show_results(finalized)
        assert wait_for(lambda: not review._pending_column_sizes)
    columns = headers(table)
    tims_column = columns.index("TIMS")
    assert tims_column == columns.index("spot_name") + 1
    assert "TIMS" in RESULTS_FROZEN_COLUMNS and results_column_group("TIMS") == "Spot"
    assert [columns[n] for n in frozen.frozen_columns()] == ["Shown", "sample", "spot_name", "TIMS"]
    assert review.results_model.frame["TIMS"].iloc[0] == "K12 · z13†"
    width = table.columnWidth(tims_column)
    assert table.fontMetrics().horizontalAdvance("K12 · z13†") + 12 <= width < 150
    assert table.horizontalHeader().sectionResizeMode(tims_column) == QtWidgets.QHeaderView.ResizeMode.Fixed
    table.horizontalScrollBar().setValue(table.horizontalScrollBar().maximum())
    app.processEvents()
    assert frozen.isVisible() and not frozen.isColumnHidden(tims_column)
    assert frozen.columnWidth(tims_column) == width
    # The frozen view has an existing outer inset; the badge and its text
    # padding must fit even where that inset clips the cell's trailing edge.
    badge_width = frozen.fontMetrics().horizontalAdvance("K12 · z13†") + 12
    assert frozen.columnViewportPosition(tims_column) + badge_width <= frozen.viewport().width()
    review.set_key_columns_only(True)
    assert not table.isColumnHidden(tims_column)
    review.set_key_columns_only(False)

    review.results_group_toggle.setChecked(True)
    tree = review.results_tree
    columns = headers(tree)
    assert columns.index("TIMS") == columns.index("spot_name") + 1
    assert tree.columnWidth(columns.index("TIMS")) == width
    assert tree.header().sectionResizeMode(columns.index("TIMS")) == QtWidgets.QHeaderView.ResizeMode.Fixed
    review.set_tims_marks({}, linked=False)
    assert headers(tree) == unlinked_headers
    assert headers(table) == unlinked_headers and review.plucked_only.isHidden()
    assert [table.columnWidth(n) for n in range(table.model().columnCount())] == unlinked_widths
    assert frozen.frozen_width() == unlinked_frozen_width
    pd.testing.assert_frame_equal(review.results_model.frame, original)
    pd.testing.assert_frame_equal(frame, original)
    review.close()


def test_unlinked_menu_and_save_unaffected(controller, dialogs, tmp_path, monkeypatch):
    from PySide6 import QtWidgets
    from synthetic_bundles import write_session_bundle
    from GUI.tims import join_marks
    from GUI import tims
    monkeypatch.setattr(tims, "database_availability", lambda: (True, ""))
    bundle = write_session_bundle(tmp_path / "bundle")
    window = controller.create_window(show=False).active_reduction
    window.load_experiment_directory(str(bundle / "raw"))
    assert wait_for(lambda: window.session.loaded and window._active_worker is None)
    window.session.origin = None
    menu = QtWidgets.QMenu()
    action = window.tims().add_action(menu, ["AFB_3"])
    assert not action.isEnabled() and "Link" in action.toolTip()
    assert window.spot_list.columnCount() == 1
    assert window.results_table.plucked_only.isHidden()
    assert "TIMS" not in window.results_table.results_model.frame
    # Save a linked tab with and without marks, keeping its origin identical.
    monkeypatch.setattr(window.tims(), "refresh", lambda: None)
    window.session.origin = {"server": "http://localhost:8765", "session_id": "session-1"}
    window.tims().sync()
    window.save_project(tmp_path / "before.upbr")
    before = json.loads((tmp_path / "before.upbr").read_text())
    settings_before = json.dumps(before["pipeline"]["config"], sort_keys=True).encode()
    fingerprint = window.scientific_project_fingerprint()
    name = window.session.spot_names[0]
    window.tims().marks = join_marks([replace(acquisition(), label=name)], [fraction()])
    window.tims().apply()
    item = window._spot_tree_item(name)
    assert item.text(0) == name and item.data(0, SPOT_TREE_TIMS_ROLE) == "K3 · z3"
    assert item.data(0, __import__('PySide6').QtCore.Qt.ItemDataRole.UserRole) == name
    assert window.scientific_project_fingerprint() == fingerprint
    window.save_project(tmp_path / "after.upbr")
    after = json.loads((tmp_path / "after.upbr").read_text())
    assert settings_before == json.dumps(after["pipeline"]["config"], sort_keys=True).encode()
    assert before["pipeline"] == after["pipeline"]
    assert before["workspace"] == after["workspace"]
    def keys(value):
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(v) for v in value))
        return set()
    assert not any("tims" in key.lower() for key in keys(after))


def test_controller_progressive_pages_revalidation_and_accounts(server, links, app, monkeypatch):
    import threading
    from PySide6 import QtWidgets
    import GUI.tims as tims
    from UPbRPy.session_manifest import SessionManifest

    monkeypatch.setattr(tims, "database_availability", lambda: (True, ""))
    rows = [replace(acquisition(), label=f"A_{i}", spot_id=f"spot-{i}", grain_id=f"grain-{i}") for i in range(3000)]
    server.state.batches["batch-1"] = {"id": "batch-1", "capsule_set_id": "krogh", "started_on": "2026-11-20"}
    server.state.fractions = [fraction(f"z{i}", sources=[{"spot_id": row.spot_id, "grain_id": row.grain_id}]) for i, row in enumerate(rows)]
    window = QtWidgets.QWidget()
    window.session = SimpleNamespace(origin={"server": server.url, "session_id": "session-1"},
                                     experiment=pd.DataFrame(), spot_names=[r.label for r in rows])
    window.spot_list = QtWidgets.QTreeWidget()
    displayed = []
    window.results_table = SimpleNamespace(set_tims_marks=lambda marks, **kw: displayed.append(sum(bool(v) for v in marks.values())))
    window.database = lambda: links
    logs = []
    window.append_log = logs.append
    controller = tims.TimsController(window)
    controller.manifest = SessionManifest(Path("/tmp/session_manifest.toml"), acquisitions=tuple(rows))
    started = time.monotonic()
    controller.refresh()
    assert wait_for(lambda: logs)
    assert len(server.state.calls) == 7 and len(controller.marks) == 3000
    assert displayed == [0, 500, 1000, 1500, 2000, 2500, 3000, 3000]
    assert controller.marks["A_1"][0].fraction["batch_started_on"] == "2026-11-20"
    assert all(thread != threading.get_ident() for thread in server.threads)
    assert "7 requests" in logs[-1]
    logs.clear()
    controller.refresh()
    assert wait_for(lambda: logs)
    assert len(server.state.calls) == 14
    assert all(call.headers.get("If-None-Match") for call in server.state.calls[7:])
    assert len(controller.marks) == 3000
    print(f"TIMS progressive refresh/revalidate: 7 + 7 requests (6 fraction pages + 1 batch summary), {time.monotonic()-started:.3f} s")
    # A different token must never reuse another account's validators or marks.
    db.store_token(server.url, server.state.issue_token())
    server.state.fractions = []
    logs.clear()
    controller.refresh()
    assert wait_for(lambda: logs)
    assert not server.state.calls[-1].headers.get("If-None-Match") and not controller.marks
    logs.clear()
    controller.refresh()
    assert wait_for(lambda: logs)
    assert server.state.calls[-1].headers.get("If-None-Match")  # Empty pages are revalidated too.
    db.forget_token(server.url)
    calls = len(server.state.calls)
    controller.account_changed()
    assert not controller._cache and not controller.marks and len(server.state.calls) == calls
    assert "not signed in" in logs[-1]
    window.close()
    window.spot_list.close()


def test_controller_failure_is_inline_and_discards_stale_response(server, links, app, monkeypatch):
    from PySide6 import QtWidgets
    import GUI.tims as tims
    from UPbRPy.session_manifest import SessionManifest

    monkeypatch.setattr(tims, "database_availability", lambda: (True, ""))
    window = QtWidgets.QWidget()
    window.session = SimpleNamespace(origin={"server": server.url, "session_id": "session-1"}, experiment=pd.DataFrame(), spot_names=["A_1"])
    window.spot_list = QtWidgets.QTreeWidget()
    window.results_table = SimpleNamespace(set_tims_marks=lambda marks, **kw: None)
    window.database = lambda: links
    logs = []
    window.append_log = logs.append
    c = tims.TimsController(window)
    c.manifest = SessionManifest(Path("/tmp/session_manifest.toml"), acquisitions=(acquisition(),))
    pending = []
    monkeypatch.setattr(links, "run", lambda function, done, **kw: pending.append((function, done, kw["on_failure"])))
    c.refresh()
    old = pending[-1]
    c.refresh()
    old[1](db.FractionPage([fraction()]))
    assert not c.marks  # The newer refresh owns the tab now.
    pending[-1][2](db.ConnectionFailed("offline"))
    assert logs == ["TIMS marks: offline"] and not c.marks
    assert "Link" not in c.disabled_reason(["A_1"])
    c.manifest = SessionManifest(Path("/tmp/session_manifest.toml"), acquisitions=(replace(acquisition(), spot_id="", grain_id=""),))
    menu = QtWidgets.QMenu()
    action = c.add_action(menu, ["A_1"])
    assert not action.isEnabled() and "ids" in action.toolTip()
    monkeypatch.setattr(tims, "database_availability", lambda: (False, "Database extra missing"))
    assert c.disabled_reason(["A_1"]) == "Database extra missing"
    window.close()
    window.spot_list.close()


def test_dialog_multi_sample_refusal_and_fragment_sources(server, links):
    from GUI.tims_dialog import TimsDialog
    with pytest.raises(ValueError, match="Selected samples: A, B"):
        TimsDialog(server.url, [acquisition(), replace(acquisition(), sample_id="b", sample_label="B")], links)
    rows = [acquisition(), replace(acquisition(), label="A_2", spot_id="spot-2"),
            replace(acquisition(), label="A_3", spot_id="spot-3", grain_id="grain-3")]
    d = TimsDialog(server.url, rows, links)
    assert wait_for(lambda: d._loaded and not d._busy)
    d.fragment.setChecked(True)
    d.fragment_note.setText("two fragments")
    sources = d.payload()["sources"]
    assert [s["spot_id"] for s in sources] == ["spot-1", "spot-3"]
    assert all(s["is_fragment"] and s["fragment_note"] == "two fragments" for s in sources)
    d.reject()


def test_dialog_retries_lost_batch_reply_with_same_key_and_date(server, links, monkeypatch):
    import GUI.tims_dialog as module
    d = ready_dialog(server, links)
    original = db.create_batch
    writes = []
    def lost_reply(*args, **kwargs):
        result = original(*args, **kwargs)
        writes.append((dict(args[1]), kwargs["idempotency_key"]))
        if len(writes) == 1:
            raise db.ConnectionFailed("lost batch reply")
        return result
    monkeypatch.setattr(db, "create_batch", lost_reply)
    d.save()
    assert wait_for(lambda: not d._busy)
    assert len(server.state.batches) == 1 and not server.state.fractions
    # An overnight retry is still the same Save, including its original date.
    from datetime import date
    monkeypatch.setattr(module, "date", SimpleNamespace(today=lambda: date(2099, 1, 1)))
    d.save()
    assert wait_for(lambda: d.result_fraction is not None)
    assert writes[0] == writes[1]
    assert len(server.state.batches) == len(server.state.fractions) == 1


def test_choice_and_detail_revalidation_clear_on_sign_out(server):
    server.state.batches["b"] = {"id": "b", "capsule_set_id": "krogh", "started_on": "2026-11-20"}
    readers = [lambda: db.list_capsule_sets(server.url), lambda: db.list_batches(server.url, "krogh"),
               lambda: db.list_recipes(server.url, lab_id="lab-1", mineral="zircon"),
               lambda: db.get_batch(server.url, "b")]
    for read in readers:
        first = read()
        assert read() == first
        assert server.state.calls[-1].headers.get("If-None-Match")
    server.state.batches["b"]["started_on"] = "2026-11-21"
    assert db.get_batch(server.url, "b")["started_on"] == "2026-11-21"
    db.forget_token(server.url)
    assert not any(key[0] == server.url for key in db._tims_responses)
    db.store_token(server.url, server.state.issue_token())
    db.list_capsule_sets(server.url)
    assert not server.state.calls[-1].headers.get("If-None-Match")


def test_sign_out_clears_every_open_tab(server, links, app, monkeypatch, tmp_path):
    from PySide6 import QtCore, QtWidgets
    from GUI.database import DatabaseLinks
    from GUI.tims import TimsController, join_marks

    monkeypatch.setattr(DatabaseLinks, "require_available", lambda *args: True)
    other_links = DatabaseLinks(open_bundle=lambda b: None, open_project=lambda p: None,
        parent_widget=lambda: None,
        settings=QtCore.QSettings(str(tmp_path / "other.ini"), QtCore.QSettings.Format.IniFormat))
    windows, controllers = [], []
    for tab_links in (links, other_links):
        window = QtWidgets.QWidget()
        window.session = SimpleNamespace(origin={"server": server.url, "session_id": "session-1"},
            directory=None, experiment=pd.DataFrame(), spot_names=["A_1"])
        window.spot_list = QtWidgets.QTreeWidget(window)
        window.results_table = SimpleNamespace(set_tims_marks=lambda marks, **kw: None)
        window.database = lambda value=tab_links: value
        window.append_log = lambda text: None
        c = TimsController(window)
        c.sync()  # Connect each tab to its own DatabaseLinks instance.
        c.marks = join_marks([acquisition()], [fraction()])
        c._cache["cached-page"] = object()
        windows.append(window)
        controllers.append(c)
    links.sign_out(server.url)
    assert wait_for(lambda: not links._workers)
    assert not db.is_signed_in(server.url)
    assert all(not c.marks and not c._cache for c in controllers)
    for window in windows:
        window.close()


def test_tims_visibility_follows_project_link(links, app, monkeypatch):
    from PySide6 import QtCore, QtWidgets
    import GUI.tims as tims
    from GUI.widgets import ResultsTableWidget
    from UPbRPy.session_manifest import SessionManifest

    manifest = SessionManifest(Path("/tmp/session_manifest.toml"), acquisitions=(acquisition(),))
    monkeypatch.setattr(tims, "resolve_session_manifest", lambda *args: manifest)
    window = QtWidgets.QWidget()
    window.session = SimpleNamespace(origin=None, directory="/tmp", experiment=pd.DataFrame(), spot_names=["A_1"])
    window.spot_list = QtWidgets.QTreeWidget(window)
    item = QtWidgets.QTreeWidgetItem(window.spot_list, ["A_1"])
    item.setData(0, QtCore.Qt.ItemDataRole.UserRole, "A_1")
    window.results_table = ResultsTableWidget(window)
    frame = pd.DataFrame({"spot_name": ["A_1"], "sample": ["A"]})
    window.results_table.show_results(SimpleNamespace(selected_results=frame, parallel_results=frame))
    original = window.results_table.results_model.frame.copy(deep=True)
    window.database = lambda: links
    window.append_log = lambda text: None
    c = tims.TimsController(window)
    monkeypatch.setattr(c, "refresh", lambda: None)
    c.sync()
    assert window.spot_list.columnCount() == 1 and window.results_table.plucked_only.isHidden()
    pd.testing.assert_frame_equal(window.results_table.results_model.frame, original)

    window.session.origin = {"server": "http://localhost:8765", "session_id": "one"}
    c.sync()
    c.marks = tims.join_marks([acquisition()], [fraction()])
    c.apply()
    assert window.spot_list.columnCount() == 1 and not window.results_table.plucked_only.isHidden()
    assert item.data(0, SPOT_TREE_TIMS_ROLE) == "K3 · z3"
    assert window.results_table.results_model.frame["TIMS"].tolist() == ["K3 · z3"]
    window.results_table.plucked_only.setChecked(True)

    # A project switch must clear the marks and restore the original controls.
    window.session = SimpleNamespace(origin=None, directory="/tmp", experiment=pd.DataFrame(), spot_names=["A_1"])
    c.sync()
    assert not c.marks and not item.data(0, SPOT_TREE_TIMS_ROLE)
    assert window.spot_list.columnCount() == 1 and window.results_table.plucked_only.isHidden()
    assert not window.results_table.plucked_only.isChecked()
    pd.testing.assert_frame_equal(window.results_table.results_model.frame, original)
    window.session.origin = {"server": "http://localhost:8765", "session_id": "two"}
    c.sync()
    window.session.origin = None  # Unlink this same project.
    c.sync()
    assert window.spot_list.columnCount() == 1 and window.results_table.plucked_only.isHidden()
    pd.testing.assert_frame_equal(window.results_table.results_model.frame, original)
    window.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [180, 320])
def test_spot_tree_badge_rendering(controller, app, monkeypatch, theme, width, tmp_path):
    import numpy as np
    from PySide6 import QtCore, QtGui, QtWidgets
    from GUI.theme import active_theme, apply_theme
    from GUI.tims import join_marks

    window = controller.create_window(show=False).active_reduction
    previous_theme = active_theme().name
    name = "HJ-gc1_a_very_long_spot_name_123"
    window.session.experiment = pd.DataFrame({"spot_names": [name], "sample": ["HJ-gc1"]})
    window._populate_spot_tree()
    tree = window.spot_list
    tree.setFixedSize(width, 220)
    window.window().resize(1280, 800)
    window.window().show()
    tree.expandAll()
    item = window._spot_tree_item(name)
    assert item is not None and tree.columnCount() == 1
    tooltip_before = item.toolTip(0)
    c = window.tims()
    monkeypatch.setattr(c, "refresh", lambda: None)

    def grab():
        for _ in range(5):
            app.processEvents()
        image = tree.grab().toImage().convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
        data = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(image.height(), image.bytesPerLine())
        return data[:, :image.width() * 4].reshape(image.height(), image.width(), 4).copy()

    try:
        apply_theme(app, theme)
        before = grab()
        window.session.origin = {"server": "http://localhost:8765", "session_id": "session-1"}
        c.apply()
        assert np.array_equal(grab(), before)  # Linking an unmarked tab changes no tree pixels.
        c.marks = join_marks([replace(acquisition(), label=name)], [fraction(sources=[{
            "grain_id": "grain-1", "spot_id": "spot-1", "is_fragment": True, "fragment_note": "half grain"}])])
        c.apply()
        marked = grab()
        assert tree.columnCount() == 1 and item.text(0) == name
        assert item.data(0, SPOT_TREE_TIMS_ROLE) == "K3 · z3†"
        assert item.toolTip(0) == tooltip_before
        index = tree.indexFromItem(item)
        delegate = tree.itemDelegate()
        option = QtWidgets.QStyleOptionViewItem()
        tree.initViewItemOption(option)
        option.rect = tree.visualRect(index)
        font, rect = delegate._tims_layout(option, index)
        assert rect.width() >= QtGui.QFontMetrics(font).horizontalAdvance("K3 · z3†")
        area = rect.translated(tree.viewport().pos())
        scale = tree.devicePixelRatioF()
        def crop(pixels):
            return pixels[round(area.top() * scale):round((area.bottom() + 1) * scale),
                          round(area.left() * scale):round((area.right() + 1) * scale)]
        badge = crop(marked)
        assert badge.size and np.any(badge[:, :, :3] != badge[0, 0, :3])
        assert not np.array_equal(crop(before), badge)
        # Removing the long name cannot change the badge pixels: it was elided first.
        with QtCore.QSignalBlocker(tree):
            item.setText(0, "")
        assert np.array_equal(crop(grab()), badge)
        with QtCore.QSignalBlocker(tree):
            item.setText(0, name)
        c.marks[name] = [replace(c.marks[name][0], grain_only=True)]
        c.apply()
        grain_badge = crop(grab())
        assert item.data(0, SPOT_TREE_TIMS_GRAIN_ROLE)
        assert not np.array_equal(grain_badge, badge)
        shown = []
        monkeypatch.setattr(QtWidgets.QToolTip, "showText", lambda *args: shown.append(args[1]))
        event = QtGui.QHelpEvent(QtCore.QEvent.Type.ToolTip, rect.center(), tree.viewport().mapToGlobal(rect.center()))
        assert delegate.helpEvent(event, tree, option, index)
        assert shown == [item.data(0, SPOT_TREE_TIMS_TOOLTIP_ROLE)]
        assert all(text in shown[0] for text in ("K3 · z3†", "Krogh", "picked", "half grain", "grain plucked via another spot"))
        window.session.origin = None
        c.apply()
        assert not item.data(0, SPOT_TREE_TIMS_ROLE)
        assert item.toolTip(0) == tooltip_before and np.array_equal(grab(), before)
        # The existing rename editor still receives only the full original name.
        window.session.origin = {"server": "http://localhost:8765", "session_id": "session-1"}
        c.apply()
        editor = delegate.createEditor(tree.viewport(), option, index)
        delegate.setEditorData(editor, index)
        assert isinstance(editor, QtWidgets.QLineEdit) and editor.text() == name
        editor.deleteLater()
        tree.grab().save(str(tmp_path / "tims-tree.png"))
    finally:
        apply_theme(app, previous_theme)


def test_results_exports_exclude_display_marks(app, tmp_path):
    import zipfile
    from GUI.export_dialog import ExportResultsDialog
    from GUI.tims import join_marks
    from GUI.widgets import ResultsTableWidget, results_display_frame
    from UPbRPy.export_table import build_export_table, write_export_csv
    from UPbRPy.reproducibility import export_reduction_bundle
    from test_export_table import synthetic_pipeline

    pipeline = synthetic_pipeline()
    finalized = pipeline.run_finalize()
    before = build_export_table(pipeline)
    before_files = write_export_csv(before, tmp_path / "before.csv")
    review = ResultsTableWidget()
    review.show_results(finalized)
    name = finalized.selected_results.iloc[0]["spot_name"]
    review.set_tims_marks(join_marks([replace(acquisition(), label=name)], [fraction()]))
    assert "TIMS" in review.results_model.frame
    assert "TIMS" not in results_display_frame(finalized.selected_results, finalized.parallel_results)
    after = build_export_table(pipeline)
    pd.testing.assert_frame_equal(before.rows, after.rows)
    after_files = write_export_csv(after, tmp_path / "after.csv")
    assert [p.read_bytes() for p in before_files] == [p.read_bytes() for p in after_files]
    dialog = ExportResultsDialog(after)
    assert "TIMS" not in dialog.table.rows and "TIMS" not in dialog.column_items
    pipeline.run_spot_means = lambda: finalized.parallel_results.copy()
    bundle = export_reduction_bundle(pipeline, tmp_path / "export", include_depth_profile=False,
        formats=("xlsx", "csv", "md"), table=dialog.table)
    for path in bundle.files:
        if path.suffix == ".xlsx":
            with zipfile.ZipFile(path) as archive:
                assert all(b"TIMS" not in archive.read(name) for name in archive.namelist())
        else:
            assert b"TIMS" not in path.read_bytes()
    assert "TIMS" not in finalized.selected_results and "TIMS" not in finalized.parallel_results
    dialog.reject()
    review.close()
