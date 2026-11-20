"""Tauri feed parsing, platform selection, limits and SemVer precedence."""

from dataclasses import replace
import io
import json
import urllib.error

import pytest

from GUI.labupdate.config import UpdateConfig, platform_key
from GUI.labupdate import config as config_module
from GUI.labupdate.manifest import NoPublicRelease, NoUpdateForPlatform, compare_versions, fetch_manifest, parse_manifest


def config(**kwargs):
    return UpdateConfig("Test", "owner/repo", "", settings_org="Tests", settings_app="labupdate", **kwargs)


def feed():
    return {"version": "v1.2.3", "notes": "Notes", "pub_date": "2026-11-19T00:00:00Z", "platforms": {
        platform_key(): {"url": "https://example.org/app.zip", "signature": "signed"}}}


def test_defaults_and_parse():
    cfg = config()
    assert cfg.feed_url == "https://github.com/owner/repo/releases/latest/download/latest.json"
    assert cfg.download_page == "https://github.com/owner/repo/releases/latest"
    release = parse_manifest(json.dumps(feed()).encode(), cfg)
    assert (release.version, release.notes, release.signature) == ("v1.2.3", "Notes", "signed")
    with pytest.raises(NoUpdateForPlatform):
        parse_manifest(feed(), cfg, target="unknown")


@pytest.mark.parametrize("system,machine,key", [("Darwin", "arm64", "darwin-aarch64"), ("Darwin", "x86_64", "darwin-x86_64"),
    ("Windows", "AMD64", "windows-x86_64"), ("Linux", "x86_64", "linux-x86_64")])
def test_platform(system, machine, key, monkeypatch):
    monkeypatch.setattr(config_module.platform, "system", lambda: system)
    monkeypatch.setattr(config_module.platform, "machine", lambda: machine)
    assert platform_key() == key


def test_unsupported_architecture(monkeypatch):
    monkeypatch.setattr(config_module.platform, "machine", lambda: "riscv64")
    with pytest.raises(ValueError):
        platform_key()


@pytest.mark.parametrize("left,right", [("0.1.10", "0.1.9"), ("1.0.0", "1.0.0-beta.1"), ("1.0.0-beta.11", "1.0.0-beta.2"),
    ("1.0.0-beta", "1.0.0-alpha"), ("1.0.0-alpha.1", "1.0.0-alpha"), ("1.0.0-alpha.a", "1.0.0-alpha.9")])
def test_version_order(left, right):
    assert compare_versions(left, right) == 1
    assert compare_versions(right, left) == -1
    assert compare_versions("v1.0.0+abc", "1.0.0+def") == 0


@pytest.mark.parametrize("value", ["1.2", "1.2.3-01", "01.2.3", "1.2.3-.", "1.2.3+", "1.2.3+.."])
def test_bad_versions(value):
    with pytest.raises(ValueError):
        parse_manifest({**feed(), "version": value}, config())


@pytest.mark.parametrize("field,value", [("version", 1), ("notes", []), ("pub_date", 123), ("platforms", []), ("platforms", {"x": {"url": "https://x"}})])
def test_validate_types(field, value):
    with pytest.raises(ValueError):
        parse_manifest({**feed(), field: value}, config())


def test_size_limit():
    cfg = config(max_manifest_bytes=32)
    calls = []
    def opener(request, *, timeout):
        calls.append((request.full_url, timeout))
        return io.BytesIO(b" " * 10000)
    with pytest.raises(ValueError, match="size limit"):
        fetch_manifest(cfg, opener)
    assert len(calls) == 1


def test_404():
    def opener(request, *, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO())
    with pytest.raises(NoPublicRelease):
        fetch_manifest(config(), opener)


def test_file_manifest(tmp_path):
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(feed()))
    assert fetch_manifest(config(feed_url=path.as_uri())).version == "v1.2.3"


def test_plain_http_rejected():
    data = feed()
    data["platforms"][platform_key()]["url"] = "http://example.org/unsafe.zip"
    with pytest.raises(ValueError):
        parse_manifest(data, config())
