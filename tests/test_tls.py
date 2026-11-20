"""Offline checks for explicit TLS trust and every production HTTP transport."""

import builtins
import io
import json
from pathlib import Path
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from UPbRPy import tls


@pytest.fixture(autouse=True)
def fresh_context(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    cached = tls.ssl_context
    cached.cache_clear()
    yield
    cached.cache_clear()


@pytest.mark.parametrize("file, directory", [("custom.pem", None), (None, "custom-ca"),
                                            ("custom.pem", "custom-ca")])
def test_explicit_ca_overrides_do_not_load_build_defaults(tmp_path, monkeypatch, file, directory):
    if file:
        file = str(tmp_path / file)
        Path(file).touch()
    if directory:
        directory = str(tmp_path / directory)
        Path(directory).mkdir()
    calls = []
    context = SimpleNamespace(load_verify_locations=lambda **kw: calls.append(kw))
    monkeypatch.setattr(tls.ssl, "SSLContext", lambda protocol: context)
    if file:
        monkeypatch.setenv("SSL_CERT_FILE", file)
    if directory:
        monkeypatch.setenv("SSL_CERT_DIR", directory)
    assert tls.ssl_context() is context
    assert tls.ssl_context() is context
    assert calls == [{"cafile": file, "capath": directory}]


@pytest.mark.parametrize("available", [True, False])
def test_native_trust_or_explicit_certifi_fallback(monkeypatch, available):
    original = builtins.__import__
    context = SimpleNamespace()
    calls = []

    def fake_import(name, *args, **kwargs):
        if name == "truststore":
            if not available:
                raise ImportError("unsupported platform")
            return SimpleNamespace(SSLContext=lambda protocol: calls.append(protocol) or context)
        if name == "certifi":
            return SimpleNamespace(where=lambda: "/bundled/certifi/cacert.pem")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(tls.ssl, "create_default_context",
                        lambda **kw: calls.append(kw) or context)
    assert tls.ssl_context() is context
    assert tls.ssl_context() is context
    assert calls == ([ssl.PROTOCOL_TLS_CLIENT] if available else
                     [{"cafile": "/bundled/certifi/cacert.pem"}])


def test_updater_and_health_probe_pass_shared_context_to_urllib(tmp_path, monkeypatch):
    from PySide6 import QtCore, QtWidgets
    from GUI import updater, database

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    context = object()
    calls = []
    monkeypatch.setattr(tls, "ssl_context", lambda: context)

    def open_url(request, **kwargs):
        calls.append((request, kwargs))
        return io.BytesIO(b"[]")

    monkeypatch.setattr(tls.urllib.request, "urlopen", open_url)
    settings = QtCore.QSettings(str(tmp_path / "settings.ini"), QtCore.QSettings.Format.IniFormat)
    instance = updater.Updater(settings=settings)
    with instance._opener("https://example.org", timeout=17):
        pass
    assert database._server_answers("https://lab.example.org", timeout=3)
    assert calls == [("https://example.org", {"timeout": 17, "context": context}),
                     ("https://lab.example.org/v1/health", {"timeout": 3, "context": context})]
    assert app is not None


def test_requests_adapter_has_same_context_for_direct_and_proxy_pools(monkeypatch):
    requests = pytest.importorskip("requests")
    context = tls.ssl_context()
    with tls.requests_session() as session:
        adapter = session.get_adapter("https://github.com")
        assert adapter.poolmanager.connection_pool_kw["ssl_context"] is context
        prepared = requests.Request("GET", "https://github.com").prepare()
        _, options = adapter.build_connection_pool_key_attributes(prepared, True)
        assert options["ssl_context"] is context
        for proxy in ("http://proxy.invalid", "https://proxy.invalid"):
            manager = adapter.proxy_manager_for(proxy)
            assert manager.connection_pool_kw["ssl_context"] is context
            if proxy.startswith("https:"):
                assert manager.connection_pool_kw["_proxy_config"].ssl_context is context
        connection = SimpleNamespace(ca_certs="old", ca_cert_dir="old")
        adapter.cert_verify(connection, prepared.url, True, None)
        assert connection.cert_reqs == "CERT_REQUIRED"
        assert connection.ca_certs is None and connection.ca_cert_dir is None


def test_upbdb_uses_shared_requests_transport_without_new_calls(monkeypatch):
    from UPbRPy.integration import upbdb

    original = upbdb.importlib.import_module
    exceptions = SimpleNamespace(Timeout=TimeoutError, RequestException=OSError)
    monkeypatch.setattr(upbdb.importlib, "import_module", lambda name:
                        SimpleNamespace(exceptions=exceptions) if name == "requests" else original(name))
    calls = []
    reply = SimpleNamespace(status_code=200)
    monkeypatch.setattr(tls, "request", lambda *args, **kw: calls.append((args, kw)) or reply)
    assert upbdb._request("GET", "https://lab.example.org", "health", auth=False,
                          action="check the server") is reply
    assert len(calls) == 1
    assert calls[0][0] == ("GET", "https://lab.example.org/v1/health")
    assert calls[0][1]["timeout"] == (upbdb.CONNECT_TIMEOUT, upbdb.READ_TIMEOUT)


@pytest.mark.parametrize("wrapped", [False, True])
def test_certificate_message_is_distinct_and_retains_details(monkeypatch, wrapped):
    from GUI import database
    from GUI.labupdate.state import classify_error
    from UPbRPy.integration import upbdb

    certificate = ssl.SSLCertVerificationError(1, "certificate verify failed: local issuer")
    error = URLError(certificate) if wrapped else certificate
    assert tls.is_certificate_error(error)
    assert classify_error(error)[0] == "certificate"
    assert classify_error("DNS offline")[0] == "offline"
    assert classify_error(URLError(OSError("nodename nor servname provided, or not known")))[0] == "offline"

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(tls, "urlopen", fail)
    with pytest.raises(upbdb.ConnectionFailed, match="certificate check failed") as failed:
        database._server_answers("https://lab.example.org")
    assert failed.value.details == str(error)
    monkeypatch.setattr(tls, "urlopen", lambda *a, **kw: (_ for _ in ()).throw(URLError("offline")))
    assert database._server_answers("https://lab.example.org") is False


def test_requests_certificate_wrapper_and_database_error(monkeypatch):
    requests = pytest.importorskip("requests")
    from UPbRPy.integration import upbdb

    certificate = ssl.SSLCertVerificationError(1, "certificate verify failed")
    wrapped = requests.exceptions.SSLError(URLError(certificate))
    assert tls.is_certificate_error(wrapped)

    def fail(*args, **kwargs):
        raise wrapped

    monkeypatch.setattr(tls, "request", fail)
    with pytest.raises(upbdb.ConnectionFailed, match="certificate check failed") as error:
        upbdb._request("GET", "https://lab.example.org", "health", auth=False, action="connect")
    assert error.value.details == str(wrapped)


def test_certificate_exception_type_survives_updater_worker(tmp_path, monkeypatch):
    from GUI.labupdate.qt import Updater, _Worker
    from GUI.labupdate.state import classify_error
    from GUI.update_config import update_config

    def fail(*args, **kwargs):
        raise ssl.SSLCertVerificationError(1, "issuer unavailable")

    updater = Updater(update_config(), "0.1.0", opener=fail)
    worker = _Worker("check", updater)
    events = []
    worker.result.connect(events.append)
    worker.run()
    assert events[0][0] == "failed"
    assert isinstance(events[0][1], ssl.SSLCertVerificationError)
    assert "issuer unavailable" in str(events[0][1])
    assert classify_error(events[0][1])[0] == "certificate"
    updater.shutdown()


def test_database_server_picker_shows_certificate_cause(tmp_path):
    import time
    from PySide6 import QtWidgets
    from GUI.database import ServerPickerDialog
    from UPbRPy.integration.upbdb import ConnectionFailed

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    message = tls.certificate_error_message("lab.example.org")

    def fail(server):
        raise ConnectionFailed(message, details="CERTIFICATE_VERIFY_FAILED: raw issuer detail")

    dialog = ServerPickerDialog(["https://lab.example.org"], "", "Choose server", "Saved", check=fail)
    deadline = time.monotonic() + 3
    while dialog._futures and time.monotonic() < deadline:
        dialog._collect()
        app.processEvents()
    assert not dialog._futures
    assert "certificate check failed" in dialog.list.item(0).text()
    assert "raw issuer detail" in dialog.list.item(0).toolTip()
    dialog.list.setCurrentRow(0)
    assert dialog.message.text() == message
    assert "raw issuer detail" in dialog.message.toolTip()
    dialog.close()


def test_tls_transport_preserves_paging_and_request_count(monkeypatch, capsys):
    """Compare the old requests entry point and shared transport without sockets."""
    import json
    import time
    from urllib.parse import parse_qs, urlsplit

    requests = pytest.importorskip("requests")
    from UPbRPy.integration import upbdb

    rows = [{"id": str(i), "session_key": f"session-{i}"} for i in range(5000)]
    calls = []
    transferred = []

    def send(session, prepared, **kwargs):
        calls.append(prepared)
        params = parse_qs(urlsplit(prepared.url).query)
        assert params["limit"] == ["100"]
        reply = requests.Response()
        reply.url = prepared.url
        if prepared.headers.get("If-None-Match") == '"page-0"':
            reply.status_code = 304
            reply._content = b""
        else:
            reply.status_code = 200
            page = rows[:100]
            transferred.extend(page)
            reply._content = json.dumps({"items": page}).encode()
            reply.headers.update({"ETag": '"page-0"', "X-Next-Cursor": "100"})
        return reply

    monkeypatch.setattr(requests.Session, "send", send)
    tls.ssl_context()  # Warm the context; it is created only once in Studio.
    started = time.perf_counter()
    requests.request("GET", "https://lab.example.org/v1/sessions", params={"limit": 100})
    before = time.perf_counter() - started
    assert len(calls) == 1 and len(transferred) == 100
    calls.clear()
    transferred.clear()
    previous = upbdb.set_token_store(upbdb.MemoryTokenStore())
    try:
        upbdb.store_token("https://lab.example.org", "test-token")
        started = time.perf_counter()
        page = upbdb.list_sessions("https://lab.example.org")
        after = time.perf_counter() - started
        assert len(page) == 100 and page.next_cursor == "100"
        assert len(calls) == 1 and len(transferred) == 100
        assert upbdb.list_sessions("https://lab.example.org") == page
        assert len(calls) == 2 and len(transferred) == 100
    finally:
        upbdb.forget_token("https://lab.example.org")
        upbdb.set_token_store(previous)
    print(f"Mock transport, 5000 available rows: before 1 request, {before * 1000:.3f} ms; "
          f"after 1 request, {after * 1000:.3f} ms; revalidation 1 request, 0 new rows.")


def test_no_bare_http_calls_outside_shared_helper():
    root = Path(__file__).parents[1]
    for folder in ("GUI", "UPbRPy"):
        for source in (root / folder).rglob("*.py"):
            if source == root / "UPbRPy/tls.py":
                continue
            text = source.read_text()
            assert "urllib.request.urlopen(" not in text, source
            assert "requests.request(" not in text, source


def test_streamed_request_keeps_session_until_response_closes(monkeypatch):
    pytest.importorskip("requests")
    from UPbRPy import tls

    closed = []

    class Response:
        def close(self):
            closed.append("response")

    class Session:
        def request(self, *args, **kwargs):
            return Response()

        def close(self):
            closed.append("session")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    monkeypatch.setattr(tls, "requests_session", Session)
    response = tls.request("GET", "https://example.invalid/zip", stream=True)
    assert closed == []
    response.close()
    assert closed == ["response", "session"]
    tls.request("GET", "https://example.invalid/json")
    assert closed[-1] == "session"


@pytest.mark.parametrize("error,expected", [
    (ssl.SSLCertVerificationError("untrusted issuer"), True),
    (urllib.error.URLError(ssl.SSLCertVerificationError("untrusted issuer")), True),
    (urllib.error.URLError("timed out"), False),
    (RuntimeError("CERTIFICATE_VERIFY_FAILED"), True),
    (RuntimeError("certificate verify failed"), True),
])
def test_certificate_error(error, expected):
    assert tls.is_certificate_error(error) is expected


def test_requests_certificate_error():
    requests = pytest.importorskip("requests")
    assert tls.is_certificate_error(requests.exceptions.SSLError("untrusted issuer"))


def test_ca_bundle_missing_certifi(monkeypatch):
    monkeypatch.setitem(sys.modules, "certifi", None)
    assert tls.ca_bundle() is None


def test_ca_bundle_missing_file(monkeypatch, tmp_path):
    import certifi

    monkeypatch.setattr(certifi, "where", lambda: str(tmp_path / "missing.pem"))
    assert tls.ca_bundle() is None


@pytest.mark.parametrize("has_stdout", [True, False])
def test_check_tls_offline(monkeypatch, capsys, tmp_path, has_stdout):
    pytest.importorskip("PySide6")
    from GUI import main

    def forbidden(*args, **kwargs):
        pytest.fail("Offline TLS check created a QApplication or attempted a download")

    monkeypatch.setattr(main, "build_application", forbidden)
    monkeypatch.setattr(tls, "urlopen", forbidden)
    output = tmp_path / "check-tls.json"
    with monkeypatch.context() as patch:
        if not has_stdout:
            patch.setattr(sys, "stdout", None)
        assert main.main(["--check-tls", "--check-tls-output", str(output)]) == 0
    printed = capsys.readouterr().out
    report_json = output.read_text(encoding="utf-8")
    assert printed == (report_json if has_stdout else "")
    report = json.loads(report_json)
    assert report == {
        "ca_certificates": tls.ca_certificate_count(), "ca_bundle": tls.ca_bundle(),
        "trust": tls.trust_source(), "url": None, "ok": True, "error": None, "detail": None,
    }
    assert report["trust"] in {"truststore", "certifi"}


@pytest.mark.parametrize("error,category,exit_code", [
    (None, None, 0),
    (urllib.error.URLError(ssl.SSLCertVerificationError("untrusted issuer")), "certificate", 1),
    (urllib.error.URLError("timed out"), "network", 1),
])
def test_check_tls_url(monkeypatch, capsys, tmp_path, error, category, exit_code):
    pytest.importorskip("PySide6")
    from GUI.main import main

    calls = []

    def open_request(url, *, timeout):
        calls.append((url, timeout))
        if error is not None:
            raise error
        return io.BytesIO()

    monkeypatch.setattr(tls, "urlopen", open_request)
    output = tmp_path / "check-tls.json"
    assert main(["--check-tls", "https://example.org", "--check-tls-output", str(output)]) == exit_code
    printed = capsys.readouterr().out
    assert output.read_text(encoding="utf-8") == printed
    report = json.loads(printed)
    assert calls == [("https://example.org", 10)]
    assert report["url"] == "https://example.org"
    assert report["ok"] is (error is None)
    assert report["error"] == category
    assert report["detail"] == (str(error) if error is not None else None)


@pytest.mark.parametrize("source,roots,error,returncode,stderr,expected", [
    ("file", 150, None, 0, "", None),
    ("stdout", 150, None, 0, "", None),
    ("invalid-file", 150, None, 0, "", "did not return JSON"),
    ("file", 0, None, 0, "", None),
    ("file", None, None, 0, "", None),
    ("file", 150, "certificate", 1, "", "failed"),
    ("file", 150, "network", 1, "", "Warning"),
    ("file", 150, None, 1, "", "failed"),
    ("file", 150, None, 0, "Traceback", "failed"),
])
def test_build_smoke_tls(monkeypatch, tmp_path, capsys, source, roots, error,
                         returncode, stderr, expected):
    from scripts import build_desktop as build

    monkeypatch.delenv("UPBRPY_SMOKE_SESSION", raising=False)
    monkeypatch.setattr(build, "_write_smoke_project", lambda work: work / "smoke.upbr")
    monkeypatch.setattr(build, "verify_update_self_test", lambda *args: None)
    output = tmp_path / "check-tls.json"
    output.write_text('{"stale": true}', encoding="utf-8")
    report_json = json.dumps({"ca_certificates": roots, "trust": "truststore",
                              "url": "https://github.com/", "ok": error is None,
                              "error": error, "detail": "unavailable"})

    def run_frozen(application, arguments, work, **kwargs):
        if "--export" in arguments:
            Path(arguments[arguments.index("--export") + 1]).write_bytes(b"x" * 10_000)
        elif "--verify-reduction" in arguments:
            verification = Path(arguments[arguments.index("--verification-output") + 1])
            verification.mkdir()
            (verification / "verification.json").write_text(json.dumps({
                "frozen": True, "spots": 12, "stages": ["concentration"],
            }), encoding="utf-8")
        else:
            assert arguments == ["--check-tls", "https://github.com/",
                                 "--check-tls-output", str(output)]
            assert not output.exists()
            assert kwargs["check"] is False
            overrides = kwargs["environment_overrides"]
            assert set(overrides) == {"SSL_CERT_FILE", "SSL_CERT_DIR"}
            assert all(not Path(value).exists() for value in overrides.values())
            if source != "stdout":
                output.write_text(report_json if source == "file" else "invalid", encoding="utf-8")
            # Invalid stdout must be ignored when the file exists. Valid
            # stdout must not hide a malformed file.
            stdout = "" if source == "file" else report_json
            return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(build, "_run_frozen", run_frozen)
    if expected and expected != "Warning":
        with pytest.raises(RuntimeError, match=expected):
            build.smoke_test_application(tmp_path / "app", tmp_path)
    else:
        assert build.smoke_test_application(tmp_path / "app", tmp_path).exists()
        assert ("Warning:" in capsys.readouterr().out) is (expected == "Warning")


@pytest.mark.parametrize("error,expected", [
    (None, True),
    (urllib.error.HTTPError("https://example.org", 503, "Unavailable", {}, None), True),
    (urllib.error.URLError(ssl.SSLCertVerificationError("untrusted issuer")), False),
])
def test_server_health_uses_tls(monkeypatch, error, expected):
    pytest.importorskip("PySide6")
    from GUI.database import _server_answers

    calls = []

    def open_request(url, *, timeout):
        calls.append((url, timeout))
        if error is not None:
            raise error
        return io.BytesIO()

    monkeypatch.setattr(tls, "urlopen", open_request)
    if expected:
        assert _server_answers("https://example.org/", timeout=2) is True
    else:
        from UPbRPy.integration.upbdb import ConnectionFailed
        with pytest.raises(ConnectionFailed, match="certificate check failed") as failed:
            _server_answers("https://example.org/", timeout=2)
        assert failed.value.details == str(error)
    assert calls == [("https://example.org/v1/health", 2)]


@pytest.mark.parametrize("native", [True, False])
def test_missing_overrides_use_native_or_bundled_trust(tmp_path, monkeypatch, caplog, native):
    original = builtins.__import__
    calls = []
    context = SimpleNamespace()

    def fake_import(name, *args, **kwargs):
        if name == "truststore":
            if not native:
                raise ImportError("unsupported platform")
            return SimpleNamespace(SSLContext=lambda protocol: calls.append(protocol) or context)
        if name == "certifi":
            return SimpleNamespace(where=lambda: "/bundled/certifi/cacert.pem")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(tls.ssl, "create_default_context", lambda **kw: calls.append(kw) or context)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "missing-certs"))
    with caplog.at_level("DEBUG", logger=tls.__name__):
        assert tls.ssl_context() is context
    assert tls.trust_source() == ("truststore" if native else "certifi")
    assert calls == ([ssl.PROTOCOL_TLS_CLIENT] if native else
                     [{"cafile": "/bundled/certifi/cacert.pem"}])
    assert "Ignoring SSL_CERT_FILE=" in caplog.text
    assert "Ignoring SSL_CERT_DIR=" in caplog.text


@pytest.mark.parametrize("valid", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
@pytest.mark.parametrize("invalid_kind", ["missing", "wrong-type"])
def test_each_override_is_validated_independently(tmp_path, monkeypatch, valid, invalid_kind):
    file = tmp_path / "roots.pem"
    directory = tmp_path / "roots"
    file.touch()
    directory.mkdir()
    if valid == "SSL_CERT_FILE":
        cafile, capath = str(file), None
        invalid_path = file if invalid_kind == "wrong-type" else tmp_path / "missing-dir"
    else:
        cafile, capath = None, str(directory)
        invalid_path = directory if invalid_kind == "wrong-type" else tmp_path / "missing.pem"
    monkeypatch.setenv("SSL_CERT_FILE", cafile or str(invalid_path))
    monkeypatch.setenv("SSL_CERT_DIR", capath or str(invalid_path))
    calls = []
    context = SimpleNamespace(load_verify_locations=lambda **kw: calls.append(kw))
    monkeypatch.setattr(tls.ssl, "SSLContext", lambda protocol: context)
    assert tls.ssl_context() is context
    assert calls == [{"cafile": cafile, "capath": capath}]
    assert tls.trust_source() == f"override:{cafile or capath}"


def test_bundled_or_native_trust_without_system_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "missing-certs"))
    context = tls.ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert tls.trust_source() in {"truststore", "certifi"}
    assert tls.ssl_context() is context
    tls.reset_cache()
    assert tls.ssl_context() is not context


def test_urlopen_uses_shared_https_context(monkeypatch):
    calls = []
    response = object()

    def open_request(request, **kwargs):
        calls.append((request, kwargs))
        return response

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    request = urllib.request.Request("https://example.org")
    assert tls.urlopen(request, timeout=10) is response
    assert tls.urlopen("https://example.org/again", timeout=20) is response
    context = tls.ssl_context()
    assert calls == [(request, {"context": context, "timeout": 10}),
                     ("https://example.org/again", {"context": context, "timeout": 20})]
    tls.reset_cache()
    tls.urlopen(request, timeout=10)
    assert calls[-1][1]["context"] is not context


@pytest.mark.parametrize("count", [0, 99, None])
def test_check_tls_does_not_require_openssl_ca_count(monkeypatch, capsys, count):
    from GUI.main import main

    monkeypatch.setattr(tls, "ca_certificate_count", lambda: count)
    assert main(["--check-tls"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ca_certificates"] == count
    assert report["ok"] is True


def test_native_context_without_certificate_statistics(monkeypatch):
    def unavailable():
        raise NotImplementedError

    monkeypatch.setattr(tls, "ssl_context", lambda: SimpleNamespace(cert_store_stats=unavailable))
    assert tls.ca_certificate_count() is None


@pytest.mark.parametrize("error,category,exit_code", [
    (OSError("context unavailable"), "network", 1),
    (ssl.SSLCertVerificationError("untrusted issuer"), "certificate", 1),
])
def test_check_tls_reports_context_failure(monkeypatch, capsys, error, category, exit_code):
    from GUI.main import main

    def fail():
        raise error

    monkeypatch.setattr(tls, "ssl_context", fail)
    assert main(["--check-tls"]) == exit_code
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["error"] == category
    assert report["detail"] == str(error)


def test_check_tls_dispatches_before_qt_imports(tmp_path, monkeypatch):
    """Retain the old smoke check's headless dispatch guarantee."""
    import runpy

    output = tmp_path / "result.json"
    original = builtins.__import__

    def no_qt(name, *args, **kwargs):
        if name.startswith(("PySide6", "GUI.theme", "GUI.updater")):
            pytest.fail("TLS check imported Qt or settings")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_qt)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(sys, "argv", ["GUI/main.py", "--theme", "light", "--check-tls",
                                    "--check-tls-output", str(output)])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(Path(__file__).parents[1] / "GUI/main.py"), run_name="__main__")
    assert result.value.code == 0
    assert json.loads(output.read_text())["ok"] is True


def test_compatibility_module_reexports_shared_helper():
    from UPbRPy.integration import tls as compatibility

    for name in ("urlopen", "is_certificate_error", "ca_bundle", "reset_cache", "ca_certificate_count"):
        assert getattr(compatibility, name) is getattr(tls, name)
    assert compatibility.https_context is tls.ssl_context
