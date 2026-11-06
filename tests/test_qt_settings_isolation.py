"""Settings constructors and GUI consumers stay in the test settings folder."""

from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets


def assert_isolated(settings, folder):
    path = Path(settings.fileName()).resolve()
    assert path.is_relative_to(folder.resolve())
    assert not path.is_relative_to((Path.home() / "Library" / "Preferences").resolve())
    assert settings.format() == QtCore.QSettings.Format.IniFormat
    assert settings.scope() == QtCore.QSettings.Scope.UserScope


def test_organization_settings_overloads_use_temporary_ini_folder(qt_settings_folder):
    parent = QtCore.QObject()
    for settings in (
        QtCore.QSettings("UPbRPy"),
        QtCore.QSettings("UPbRPy", "Studio"),
        QtCore.QSettings("UPbRPy", "Studio", parent),
        QtCore.QSettings("UPbRPy", parent=parent),
        QtCore.QSettings("UPbRPy", application="Studio", parent=parent),
    ):
        assert_isolated(settings, qt_settings_folder)


@pytest.mark.parametrize("format_name", ["IniFormat", "NativeFormat"])
def test_explicit_file_settings_preserve_format_and_parent(tmp_path, format_name):
    parent = QtCore.QObject()
    format_ = getattr(QtCore.QSettings.Format, format_name)
    path = tmp_path / "explicit-settings.ini"
    settings = QtCore.QSettings(str(path), format_, parent)
    assert Path(settings.fileName()) == path
    assert settings.format() == format_
    assert settings.parent() is parent


def test_explicit_format_and_scope_settings_preserve_overload(qt_settings_folder):
    parent = QtCore.QObject()
    settings = QtCore.QSettings(
        QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.SystemScope,
        "UPbRPy", "Studio", parent,
    )
    assert Path(settings.fileName()).resolve().is_relative_to(qt_settings_folder.resolve())
    assert settings.format() == QtCore.QSettings.Format.IniFormat
    assert settings.scope() == QtCore.QSettings.Scope.SystemScope
    assert settings.parent() is parent


def test_main_window_settings_use_temporary_ini_folder(qt_settings_folder):
    from GUI.main_window import MainWindow
    from GUI.theme import build_application

    app = QtWidgets.QApplication.instance() or build_application([], "light")
    window = MainWindow()
    try:
        assert_isolated(window.settings, qt_settings_folder)
    finally:
        window.has_unsaved_changes = lambda: False
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
