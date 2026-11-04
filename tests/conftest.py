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


def _isolate_qt_settings() -> None:
    try:
        from PySide6 import QtCore
    except ImportError:
        return
    import tempfile

    folder = tempfile.mkdtemp(prefix="upbrpy-test-settings-")
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    for scope in (QtCore.QSettings.Scope.UserScope, QtCore.QSettings.Scope.SystemScope):
        QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, scope, folder)


_isolate_qt_settings()
