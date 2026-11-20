"""Database download, revalidation and uncached calculation have exact identity."""
from collections import Counter
import itertools
import shutil
import time

import pytest
from PySide6 import QtCore, QtWidgets

from GUI import database, restore_process, results_cache, services
from GUI.launch import parse_open_target
from UPbRPy import session_bundle as bundles
from UPbRPy.integration import upbdb as db
from fake_upbdb import FakeState, InProcessUpbdb
from scripts.snapshot_restore import compare_snapshots, snapshot_restored
from synthetic_bundles import manifest_toml
from test_restore_process import fixture_project


@pytest.fixture
def remote(fixture_project, tmp_path, monkeypatch):
    source = tmp_path / "server" / "Zircon_demo"
    raw = source / "raw"
    raw.mkdir(parents=True)
    shutil.copyfile(fixture_project.parent / "fixture_Rich.csv", raw / "Zircon_demo_Rich.csv")
    from UPbRPy.qtegra import read_rich_csv
    labels = read_rich_csv(raw / "Zircon_demo_Rich.csv").labels
    (source / "session_manifest.toml").write_text(manifest_toml(
        session_id="session-1", session_key=source.name, labels=labels,
        raw_paths=[raw / "Zircon_demo_Rich.csv"] * len(labels), aliases={},
    ).replace('primary_68 = "MADUCSB"', 'primary_68 = "S513"')
     .replace('material = "apatite"', 'material = "zircon"'))
    fake = InProcessUpbdb(FakeState(sessions={"session-1": source},
        runs={"run-1": "session-1"}, run_projects={"run-1": fixture_project.read_bytes()}))
    previous = db.set_token_store(db.MemoryTokenStore())
    db.store_token(fake.url, fake.state.issue_token())
    monkeypatch.setattr(db.tls, "request", fake.request)
    monkeypatch.setattr(db, "_requests", lambda: fake.transport(db.tls.request))
    monkeypatch.setattr(database, "database_availability", lambda: (True, ""))
    monkeypatch.setenv("UPBRPY_RESULTS_CACHE_DIR", str(tmp_path / "results"))
    yield fake
    db.set_token_store(previous)


@pytest.fixture
def links(remote, tmp_path, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    opened = []
    contexts = []
    stages = []

    def open_project(path):
        progress = []
        restored = restore_process.RestoreTask().restore(path, progress.append)
        opened.append(restored)
        stages.append(progress)
        return restored

    links = database.DatabaseLinks(
        open_bundle=lambda bundle: open_project(bundle.project_file),
        open_project=open_project, parent_widget=lambda: None,
        settings=QtCore.QSettings(str(tmp_path / "settings.ini"), QtCore.QSettings.Format.IniFormat))
    links.cache_root = tmp_path / "sessions"
    monkeypatch.setattr(links, "_opened", lambda window, **kwargs: contexts.append(kwargs))
    monkeypatch.setattr(links, "show_error", lambda title, error: pytest.fail(f"{title}: {error}"))
    # Keep the application alive even in tests without another GUI fixture.
    links._test_app = app
    return links, opened, contexts, stages


def test_database_run_cold_warm_and_disabled_cache_are_identical(remote, links, monkeypatch):
    links, opened, contexts, stages = links
    def synchronous(function, success, **kwargs):
        success(function())
    monkeypatch.setattr(links, "run", synchronous)
    # Exercise the download branch on every open, as when a run has no local
    # project index entry. install_run_project still preserves the existing file.
    monkeypatch.setattr(database, "find_project_for_run", lambda *args: None)
    counts = Counter()
    hashed = bundles._sha256_file

    def tracked(path):
        if path.parent.name == "raw":
            counts[path.name] += 1
        return hashed(path)
    monkeypatch.setattr(bundles, "_sha256_file", tracked)
    target = parse_open_target(
        f"upbr://run/run-1?server={remote.url}&via=upblab&acquisition=acq-1")
    links.handle(target)
    assert "Reading data" in stages[-1]
    project = opened[-1].project_path
    request = results_cache.prepare(project)
    # Successful restores commit the disposable cache in the background.
    deadline = time.monotonic() + 30
    while results_cache.load(request) is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert results_cache.load(request) is not None
    assert list(counts.values()) == [1]
    for conditional in (True, False):
        remote.state.conditional_bundles = conditional
        counts.clear()
        links.handle(target)
        assert stages[-1] == ["Loading saved results"]
        exports = [r for c, r in zip(remote.state.calls, remote.responses)
                   if c.path.endswith("/export/upbrpy")]
        assert exports[-1].status_code == (304 if conditional else 200)
        assert exports[-1].body_reads == 0 and exports[-1].closed
        assert list(counts.values()) == [1]
    monkeypatch.setenv("UPBRPY_RESULTS_CACHE", "0")
    links.handle(target)
    assert "Reading data" in stages[-1] and "Loading saved results" not in stages[-1]
    snapshots = [snapshot_restored(restored) for restored in opened]
    for left, right in itertools.combinations(snapshots, 2):
        assert compare_snapshots(left, right) == []
    assert all(c["via"] == "upblab" and c["acquisition"] == "acq-1" for c in contexts)
