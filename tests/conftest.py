"""Test-session switches.

Qt settings are redirected to a temporary INI folder for the whole run, so
tests never read or write the user's real UPbRPy Studio preferences (window
geometry, panel widths, servers).

``UPBRPY_BLOCK_IMPORTS=requests,keyring`` makes those packages unimportable
for the whole run, so the suite can be checked "with the [database] extra
absent" in an environment that has it installed::

    UPBRPY_BLOCK_IMPORTS=requests,keyring QT_QPA_PLATFORM=offscreen python -m pytest tests -q
"""

from __future__ import annotations

import importlib.abc
import os
import sys
from pathlib import Path

import pytest


class _BlockedImports(importlib.abc.MetaPathFinder):
    def __init__(self, names: set[str]) -> None:
        self.names = names

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in self.names:
            raise ModuleNotFoundError(
                f"{fullname} is blocked by UPBRPY_BLOCK_IMPORTS", name=fullname
            )
        return None


_blocked = {
    name.strip()
    for name in os.environ.get("UPBRPY_BLOCK_IMPORTS", "").split(",")
    if name.strip()
}
if _blocked:
    for _name in list(sys.modules):
        if _name.split(".", 1)[0] in _blocked:
            del sys.modules[_name]
    sys.meta_path.insert(0, _BlockedImports(_blocked))


def _isolate_qt_settings() -> Path | None:
    try:
        from PySide6 import QtCore
    except ImportError:
        return
    import tempfile

    folder = tempfile.mkdtemp(prefix="upbrpy-test-settings-")
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    for scope in (QtCore.QSettings.Scope.UserScope, QtCore.QSettings.Scope.SystemScope):
        QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, scope, folder)

    class IsolatedQSettings(QtCore.QSettings):
        """Redirect the organization-name overload, which always uses native settings."""

        def __init__(self, *args, **kwargs):
            if args and isinstance(args[0], str) and (len(args) < 2 or isinstance(args[1], str)):
                super().__init__(self.Format.IniFormat, self.Scope.UserScope, *args, **kwargs)
            else:
                super().__init__(*args, **kwargs)

    # Install before collection imports any GUI modules. Explicit format and
    # file-name overloads still go through to Qt unchanged; enums are inherited.
    QtCore.QSettings = IsolatedQSettings
    return Path(folder)


_qt_settings_folder = _isolate_qt_settings()


@pytest.fixture
def qt_settings_folder() -> Path:
    """Return the temporary settings root installed before test collection."""
    if _qt_settings_folder is None:
        pytest.skip("PySide6 is unavailable")
    return _qt_settings_folder
