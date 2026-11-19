"""Explicit TLS trust for urllib and the optional requests integration.

Use native system trust unless SSL_CERT_FILE or SSL_CERT_DIR explicitly
selects a CA store. No global SSL patches are installed.
"""

from functools import lru_cache
import logging
import os
from pathlib import Path
import ssl
import urllib.request


logger = logging.getLogger(__name__)


def ca_bundle() -> str | None:
    """Return certifi's CA bundle when it is installed and present."""
    try:
        import certifi
    except ImportError:
        return None
    bundle = certifi.where()
    return bundle if Path(bundle).is_file() else None


def _override(name: str, *, directory: bool = False) -> str | None:
    path = os.environ.get(name)
    if path:
        exists = Path(path).is_dir() if directory else Path(path).is_file()
        if exists:
            return path
        logger.debug("Ignoring %s=%s: no existing %s", name, path,
                     "directory" if directory else "file")
    return None


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """Return the cached, verifying client context (configure overrides before use)."""
    cafile = _override("SSL_CERT_FILE")
    capath = _override("SSL_CERT_DIR", directory=True)
    if cafile or capath:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=cafile, capath=capath)
        context._upbrpy_trust = f"override:{cafile or capath}"
        return context
    try:
        import truststore
    except ImportError:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
        context._upbrpy_trust = "certifi"
        return context
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context._upbrpy_trust = "truststore"
    return context


def reset_cache() -> None:
    """Discard the shared context after changing trust configuration."""
    ssl_context.cache_clear()


def trust_source() -> str:
    """Return truststore, certifi, or override:<path> for the cached context.

    When both overrides exist, the reported path is the CA file; both stores
    are loaded into the context.
    """
    return ssl_context()._upbrpy_trust


def ca_certificate_count() -> int | None:
    """Return the OpenSSL CA count, or None when native trust cannot report it."""
    try:
        return ssl_context().cert_store_stats()["x509_ca"]
    except NotImplementedError:
        return None


def urlopen(request, timeout=20):
    """Open a URL using shared trust and a socket stall timeout."""
    return urllib.request.urlopen(request, timeout=timeout, context=ssl_context())


def requests_session():
    """Create an optional requests session with explicit trust, including proxies.

    Sessions belong to the caller; do not share cookies across users or threads.
    """
    import requests
    from requests.adapters import HTTPAdapter

    class TrustAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["ssl_context"] = ssl_context()
            return super().init_poolmanager(*args, **kwargs)

        def proxy_manager_for(self, proxy, **kwargs):
            kwargs["ssl_context"] = ssl_context()
            if proxy.lower().startswith("https:"):
                kwargs["proxy_ssl_context"] = ssl_context()
            return super().proxy_manager_for(proxy, **kwargs)

        def build_connection_pool_key_attributes(self, request, verify, cert=None):
            host, options = super().build_connection_pool_key_attributes(request, verify, cert)
            # Requests 2.32 can supply its own preloaded context per request,
            # overriding the pool manager's context.
            if verify is True:
                options["ssl_context"] = ssl_context()
            return host, options

        def cert_verify(self, conn, url, verify, cert):
            if verify is True:
                # Retain requests' client-certificate validation, but do not
                # add its default certifi bundle to an explicit CA override.
                super().cert_verify(conn, url, False, cert)
                conn.cert_reqs = "CERT_REQUIRED"
                conn.ca_cert_dir = None
            else:
                super().cert_verify(conn, url, verify, cert)

    session = requests.Session()
    session.mount("https://", TrustAdapter())
    return session


def request(method, url, **kwargs):
    """Make one requests call with shared trust and no persistent account state.

    A streamed response keeps its session (and pooled connection) until the
    caller closes the response; closing the session first could cut a
    download short.
    """
    if not kwargs.get("stream"):
        with requests_session() as session:
            return session.request(method, url, **kwargs)
    session = requests_session()
    try:
        response = session.request(method, url, **kwargs)
    except BaseException:
        session.close()
        raise
    close = response.close

    def close_with_session():
        try:
            close()
        finally:
            session.close()

    response.close = close_with_session
    return response


def is_certificate_error(error) -> bool:
    """Recognize certificate failures through urllib/requests/urllib3 wrappers."""
    try:
        from requests.exceptions import SSLError
    except ImportError:
        certificate_errors = (ssl.SSLCertVerificationError,)
    else:
        certificate_errors = (ssl.SSLCertVerificationError, SSLError)
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, certificate_errors):
            return True
        text = str(current).lower()
        if any(marker in text for marker in (
            "certificate_verify_failed", "certificate verify failed", "certificate check failed",
            "ssl certificate problem", "unable to get local issuer certificate",
            "certificate rejected", "ssl:",
        )):
            return True
        if isinstance(current, BaseException):
            pending.extend(current.args)
            pending.extend(getattr(current, name, None)
                           for name in ("reason", "__cause__", "__context__"))
    return False


def certificate_error_message(service: str) -> str:
    """Return an actionable certificate failure message for a named service."""
    return (
        f"Can't verify the secure connection to {service} (certificate check failed). "
        "If you are on a network that inspects traffic, install its certificate "
        "in Keychain Access (macOS) or the Windows certificate store, or try another network."
    )
