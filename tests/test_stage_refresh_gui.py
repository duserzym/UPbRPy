"""Offscreen guards for cached theme, icons, status and detector previews."""
import os
import weakref
from types import SimpleNamespace

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6 import QtCore, QtGui, QtWidgets
from GUI import main_window, widgets
from GUI.theme import THEMES, build_application
from UPbRPy.qtegra import QtegraSession


@pytest.fixture(scope='module')
def app():
    return QtWidgets.QApplication.instance() or build_application([], 'light')


def dispose(widget, app):
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def test_trace_reconfigures_only_for_theme_or_mode_changes(app, monkeypatch):
    widget = widgets.TracePlotWidget()
    try:
        widget.apply_theme(THEMES['light'])
        configure, interact = [], []
        original = widget._configure_plot
        def record():
            configure.append(True)
            original()
        monkeypatch.setattr(widget, '_configure_plot', record)
        monkeypatch.setattr(widget, '_apply_interaction_mode', lambda: interact.append(True))
        widget.apply_theme(THEMES['light'])
        widget.set_interaction_mode('zoom')
        widget._reset_items()
        assert len(configure) == 1
        assert interact == []
        configure.clear()
        widget.set_interaction_mode('pan')
        widget.set_interaction_mode('pan')
        assert len(interact) == 1
        widget.apply_theme(THEMES['dark'])
        widget.apply_theme(THEMES['dark'])
        assert len(configure) == 1
        assert len(interact) == 2
    finally:
        dispose(widget, app)


def test_icon_arguments_and_theme_colors_cached(app, monkeypatch):
    # Use only the call-site cache, without starting unrelated window workers.
    owner = SimpleNamespace(_workflow_icons={})
    calls = []
    def icon(*args, **kwargs):
        calls.append((args, kwargs))
        return QtGui.QIcon()
    monkeypatch.setattr(main_window, 'build_workflow_stage_icon', icon)
    build = lambda **kw: main_window.MainWindow._workflow_stage_icon(owner, 'baseline', **kw)
    first = build(state='ready', accent='blue')
    assert build(accent='blue', state='ready') is first
    build(state='complete', accent='blue')
    build(state='ready', accent='red')
    assert len(calls) == 3


def test_result_status_requests_coalesce_and_completion_cancels(app):
    calls = []
    owner = SimpleNamespace(_active_worker=object(), _defer_result_details=False, _refresh_workflow_status=lambda: calls.append(True))
    timer = owner._workflow_status_timer = QtCore.QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: main_window.MainWindow._flush_view_status(owner))
    request = lambda: main_window.MainWindow._refresh_view_status(owner)
    request(); request()
    assert not calls
    app.processEvents()
    assert calls == []
    request()
    timer.stop()  # _refresh_workflow_status stops the pending result request.
    owner._active_worker = None
    request()
    assert calls == [True]
    app.processEvents()
    assert calls == [True]


def test_preview_retains_input_and_recomputes_for_new_session(app, monkeypatch):
    monkeypatch.setattr(widgets, 'stage_process_enabled', lambda: False)
    calls = []
    monkeypatch.setattr(widgets, 'calibration_preview', lambda q, options: (calls.append(weakref.ref(q)) or (object(), object())))
    panel = widgets.DetectorCalibrationPanel()
    monkeypatch.setattr(panel, '_fill_preview', lambda *args: None)
    def session():
        return SimpleNamespace(qtegra=QtegraSession([], 'rich', frozenset({'analog_counter'}), ()),
                               import_settings={'sep': '_'}, primary_68_standard='S513')
    try:
        first = session()
        panel.set_session(first)
        reference = weakref.ref(first.qtegra)
        first.qtegra = None
        assert reference() is panel._preview_qtegra
        second = session()
        panel.set_session(second)
        assert len(calls) == 2
        panel.set_session(second)
        assert len(calls) == 2
        assert panel._preview_qtegra is second.qtegra
        panel.set_session(None)
        assert panel._preview_qtegra is None
    finally:
        dispose(panel, app)


def test_stage_auxiliary_refresh_coalesces_and_direct_refresh_is_synchronous(app, monkeypatch):
    from pathlib import Path
    window = main_window.MainWindow()
    source = Path(__file__).parent / 'fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv'
    try:
        window.load_experiment_directory(str(source), background=False)
        app.processEvents()
        calls = []
        monkeypatch.setattr(window, '_refresh_session_overview', lambda: calls.append('overview'))
        monkeypatch.setattr(window, '_refresh_view_details', lambda: calls.append('details'))
        # Timers retain their original bound slots; reconnect the instrumentation.
        for timer, callback in ((window._overview_refresh_timer, window._refresh_session_overview),
                                (window._view_details_timer, window._refresh_view_details)):
            timer.timeout.disconnect()
            timer.timeout.connect(callback)
        window._defer_result_details = True
        window.refresh_view()
        window.refresh_view()
        assert calls == []
        assert window._rendered_view_key is not None
        assert window.session.selected_spot == window.current_spot_name()
        app.processEvents()
        assert calls == ['overview', 'details']
        window._defer_result_details = False
        window.refresh_view()
        assert calls == ['overview', 'details', 'overview', 'details']
    finally:
        window._defer_result_details = False
        window.detector_calibration_panel.cancel_preview()
        dispose(window, app)
