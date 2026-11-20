"""A clean installation checks standards over HTTPS without adopting values."""

import io
from pathlib import Path
import ssl
import stat
import tomllib
from urllib.error import URLError
import zipfile

import pytest

from GUI import standards_updates as standards


SHA = "a" * 40
LIBRARY = Path(__file__).parents[1] / "UPbRPy/reference_materials"


def snapshot_zip(*, extra=None, modify=False):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in LIBRARY.rglob("*.toml"):
            if file.name == "UPSTREAM.toml":
                continue
            name = file.relative_to(LIBRARY).as_posix()
            data = file.read_bytes()
            if modify and name == "sources.toml":
                data += b"\n# Changed upstream\n"
            archive.writestr(f"upb-standards-{SHA}/{name}", data)
        if extra:
            archive.writestr(*extra)
    return buffer.getvalue()


def install_http(monkeypatch, archive):
    calls = []

    def opener(request, *, timeout):
        calls.append(request)
        feed = (f'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                f'<id>tag:github.com,2008:Grit::Commit/{SHA}</id>'
                '<updated>2026-11-19T12:00:00Z</updated></entry></feed>').encode()
        response = io.BytesIO(feed if request.full_url.endswith(".atom") else archive)
        response.headers = {"Last-Modified": "Thu, 19 Nov 2026 12:00:00 GMT"}
        return response

    monkeypatch.setattr(standards, "urlopen", opener)
    return calls


@pytest.mark.parametrize("git_available, real_checkout", [(True, False), (False, False), (False, True)])
def test_git_free_check_fetches_snapshot_and_compares(tmp_path, monkeypatch, git_available, real_checkout):
    monkeypatch.setattr(standards.shutil, "which", lambda name: "/git" if git_available else None)
    checkout = tmp_path / "checkout"
    if real_checkout:
        (checkout / ".git").mkdir(parents=True)
        (checkout / "user.toml").write_text("value = 99\n")
    calls = install_http(monkeypatch, snapshot_zip())
    before = {p: p.read_bytes() for p in LIBRARY.rglob("*") if p.is_file()}
    checked = standards.check_standards(str(LIBRARY), checkout)
    assert not checked.warning, checked.message
    assert "Standards match GitHub main" in checked.message
    assert f"Fetched GitHub main {SHA[:12]} on " in checked.message
    assert "2026-11-19" in checked.message
    snapshot = Path(checked.snapshot_path)
    metadata = tomllib.loads((snapshot / "UPSTREAM.toml").read_text())
    assert metadata["commit"] == SHA
    assert metadata["commit_date"].startswith("2026-11-19")
    assert metadata["sha256"]["library.toml"]
    assert standards.reference_library_info(snapshot)["sha256"] == checked.digest
    assert len(calls) == 2
    assert calls[0].get_header("Accept") == "application/atom+xml"
    assert calls[1].full_url.endswith("/zip/" + SHA)
    assert all(p.read_bytes() == data for p, data in before.items())
    if real_checkout:
        assert (checkout / "user.toml").read_text() == "value = 99\n"
        assert not (checkout / "UPSTREAM.toml").exists()


def test_existing_active_checkout_is_never_replaced(tmp_path, monkeypatch):
    checkout = tmp_path / "active"
    standards.shutil.copytree(LIBRARY, checkout)
    monkeypatch.setattr(standards.shutil, "which", lambda name: None)
    before = {p: p.read_bytes() for p in checkout.rglob("*") if p.is_file()}
    install_http(monkeypatch, snapshot_zip(modify=True))
    checked = standards.check_standards(str(checkout), checkout)
    assert checked.warning
    assert "configured library on disk differs from GitHub main" in checked.message
    assert not Path(checked.snapshot_path).is_relative_to(checkout)
    assert all(p.read_bytes() == data for p, data in before.items())
    # Repeated checks safely reuse the same immutable revision.
    assert standards.check_standards(str(checkout), checkout).snapshot_path == checked.snapshot_path


@pytest.mark.parametrize("unsafe", ["/escape.toml", "../escape.toml",
                                    f"upb-standards-{SHA}/../escape.toml",
                                    f"upb-standards-{SHA}/C:/escape.toml",
                                    f"upb-standards-{SHA}/a\\escape.toml"])
def test_bad_zip_is_rejected_without_installing(tmp_path, monkeypatch, unsafe):
    checkout = tmp_path / "checkout"
    install_http(monkeypatch, snapshot_zip(extra=(unsafe, b"value = 1")))
    result = standards.check_standards(str(LIBRARY), checkout)
    assert result.warning
    assert "Unsafe path" in result.details
    assert not checkout.exists()
    assert not (tmp_path / "escape.toml").exists()


def test_zip_symlinks_and_size_bombs_are_rejected(monkeypatch):
    link = zipfile.ZipInfo(f"upb-standards-{SHA}/zircon/link.toml")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError, match="Unsafe"):
        standards._snapshot_files(snapshot_zip(extra=(link, b"../../escape")), SHA)
    monkeypatch.setattr(standards, "MAX_EXPANDED", 100)
    with pytest.raises(ValueError, match="size limit"):
        standards._snapshot_files(snapshot_zip(), SHA)


def test_only_expected_files_are_extracted():
    selected = standards._snapshot_files(snapshot_zip(extra=(f"upb-standards-{SHA}/run.py", b"bad")), SHA)
    assert "run.py" not in selected


def test_bad_manifest_and_oversized_download_are_rejected(tmp_path, monkeypatch):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(f"upb-standards-{SHA}/library.toml",
                         'sources = "sources.toml"\nmaterial_files = ["../outside.toml"]\n')
        archive.writestr(f"upb-standards-{SHA}/sources.toml", "")
    install_http(monkeypatch, data.getvalue())
    checkout = tmp_path / "checkout"
    result = standards.check_standards(str(LIBRARY), checkout)
    assert result.warning and "manifest refers outside" in result.details
    assert not checkout.exists()
    install_http(monkeypatch, snapshot_zip())
    monkeypatch.setattr(standards, "MAX_DOWNLOAD", 100)
    result = standards.check_standards(str(LIBRARY), checkout)
    assert result.warning and "response exceeds the size limit" in result.details
    assert not checkout.exists()


def test_git_certificate_failure_preserves_cause(tmp_path, monkeypatch):
    import subprocess

    standards.shutil.copytree(LIBRARY, tmp_path / "checkout")
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir()
    monkeypatch.setattr(standards.shutil, "which", lambda name: "/git")

    def git(path, *args):
        if args[0] == "fetch":
            raise subprocess.CalledProcessError(128, "git fetch", stderr="SSL certificate problem: issuer")
        if args[0] == "rev-parse":
            return SHA
        return ""

    monkeypatch.setattr(standards, "_git", git)
    result = standards.check_standards(str(LIBRARY), checkout)
    assert "certificate check failed" in result.message
    assert result.details == "SSL certificate problem: issuer"


def test_snapshot_uses_app_data_if_checkout_parent_is_read_only(tmp_path, monkeypatch):
    checkout = tmp_path / "read-only/checkout"
    app_data = tmp_path / "app-data"
    original = standards.tempfile.TemporaryDirectory

    def temporary(**kwargs):
        if kwargs["dir"] == checkout.parent:
            raise PermissionError("Read-only installation")
        return original(**kwargs)

    monkeypatch.setattr(standards.tempfile, "TemporaryDirectory", temporary)
    monkeypatch.setattr(standards, "_app_data_snapshots", lambda: app_data)
    install_http(monkeypatch, snapshot_zip())
    checked = standards.check_standards(str(LIBRARY), checkout)
    assert not checked.warning, checked.message
    assert Path(checked.snapshot_path) == app_data / SHA
    assert not checkout.exists()


@pytest.mark.parametrize("certificate", [False, True])
def test_offline_and_certificate_errors_are_distinct(tmp_path, monkeypatch, certificate):
    raw = (ssl.SSLCertVerificationError(1, "certificate verify failed") if certificate else "offline")

    def fail(*args, **kwargs):
        raise URLError(raw)

    monkeypatch.setattr(standards, "urlopen", fail)
    result = standards.check_standards(str(LIBRARY), tmp_path / "checkout")
    assert result.warning
    assert ("certificate check failed" in result.message) is certificate
    assert ("Check your internet connection" in result.message) is not certificate
    assert str(raw) in result.details


def test_second_check_reuses_snapshot_without_downloading(tmp_path, monkeypatch):
    monkeypatch.setattr(standards.shutil, "which", lambda name: None)
    monkeypatch.setattr(standards, "_app_data_snapshots", lambda: tmp_path / "app-data")
    checkout = tmp_path / "checkout"
    calls = install_http(monkeypatch, snapshot_zip())
    first = standards.check_standards(str(LIBRARY), checkout)
    assert len(calls) == 2 and not first.warning, first.message
    second = standards.check_standards(str(LIBRARY), checkout)
    # Only the commit lookup; the ZIP for an unchanged commit is not fetched again.
    assert len(calls) == 3
    assert second.snapshot_path == first.snapshot_path
    assert "Standards match GitHub main" in second.message
