"""Studio identity, embedded update key and bundled release notes."""

from pathlib import Path

from UPbRPy.tls import urlopen
from GUI.labupdate.config import UpdateConfig

PUBLIC_KEY = 'dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDcxMkQ3QjY1RjczQUY4QjkKUldTNStEcjNaWHN0Y1lweDF4WUgyZEUvem4wMW1kUFYrL0FyQnFwTXRQUHVCR0RoMm1BRU01UGUK'


def release_notes(version: str) -> str | None:
    """Read a source or frozen note beside the bundled GUI and engine packages."""
    if not version or Path(version).name != version or "\\" in version:
        return None
    path = Path(__file__).resolve().parents[1] / "release-notes" / f"{version.removeprefix('v')}.md"
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def update_config() -> UpdateConfig:
    """Return the latest stable feed using Studio's explicit system TLS trust."""
    return UpdateConfig("UPbRPy Studio", "duserzym/UPbRPy", PUBLIC_KEY,
                        settings_org="UPbRPy", settings_app="Studio",
                        opener=urlopen, release_notes=release_notes)
