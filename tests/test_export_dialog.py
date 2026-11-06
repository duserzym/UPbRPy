"""Compact Studio export choices, preview refresh and workspace persistence."""
from dataclasses import replace
import json

import pytest
pytest.importorskip('PySide6')
from PySide6 import QtCore, QtWidgets

from GUI.export_dialog import ExportResultsDialog
from GUI.theme import build_application
from UPbRPy.export_table import ExportSettings, build_export_table
from test_export_table import synthetic_pipeline


@pytest.fixture
def app():
    application = QtWidgets.QApplication.instance() or build_application([], theme_mode='light')
    yield application
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    application.processEvents()


def test_dialog_columns_preview_settings_and_laptop_size(app, tmp_path, monkeypatch):
    pipeline = synthetic_pipeline()
    table = build_export_table(pipeline, excluded_spots=('B_3',))
    monkeypatch.setattr(pipeline, 'run_finalize', lambda: pytest.fail('preview rebuilt engine table'))
    dialog = ExportResultsDialog(table, folder=tmp_path, excluded_spots=('B_3',))
    try:
        dialog.show()
        app.processEvents()
        assert dialog.width() <= 1180 and dialog.height() <= 740
        assert dialog.minimumSizeHint().width() <= 1200 and dialog.minimumSizeHint().height() <= 740
        assert '1 analyses excluded' in dialog.exclusion_label.text()
        assert dialog.selected_formats() == ('xlsx', 'csv', 'md')
        assert dialog.audit_check.isChecked()
        for col in table.columns:
            if col.key in dialog.column_items:
                assert (dialog.column_items[col.key].checkState(0) == QtCore.Qt.CheckState.Checked) == (not col.optional)
        assert 'footnote 1' in dialog.text_fields['laboratory'].placeholderText()
        before = dialog.preview['headings']
        dialog.group_items['Composition'].setCheckState(0, QtCore.Qt.CheckState.Unchecked)
        # Exercise the debounced signal path, not just a direct method call.
        from PySide6.QtTest import QTest
        QTest.qWait(200)
        app.processEvents()
        assert all(item.checkState(0) == QtCore.Qt.CheckState.Unchecked
                   for key, item in dialog.column_items.items()
                   if any(c.key == key and c.group == 'Composition' for c in table.columns))
        assert len(dialog.preview['headings']) < len(before)
        assert 'Composition' not in dialog.preview['groups']
        dialog.text_fields['laboratory'].setText('Test laboratory')
        dialog.numeric_fields['a_tio2'].setValue(.8)
        dialog.refresh_preview()
        assert 'Test laboratory' in dialog.footnotes_edit.toPlainText()
        assert 'aTiO2 = 0.8' in dialog.footnotes_edit.toPlainText()
        dialog.defaults_button.click()
        assert dialog.export_settings().optional_columns == ()
        assert dialog.export_settings().excluded_columns == ()
        assert dialog.export_settings().laboratory == 'Test laboratory'
        # All tabs and the export action remain reachable at 1280x800.
        for index in range(dialog.tabs.count()):
            dialog.tabs.setCurrentIndex(index)
            app.processEvents()
            assert dialog.export_button.geometry().bottom() <= dialog.height()
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_nonzircon_hides_zircon_groups(app):
    pipeline = synthetic_pipeline()
    pipeline.config = replace(pipeline.config, material='titanite')
    dialog = ExportResultsDialog(build_export_table(pipeline))
    try:
        assert 'Ti-in-zircon' not in dialog.group_items
        assert 'Grimes et al. (2015)' not in dialog.group_items
        assert 'Composition' in dialog.group_items
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_settings_saved_in_project_workspace_and_old_projects_default(app, tmp_path, monkeypatch):
    from GUI.services import RestoredProject
    from GUI.studio_window import StudioWindow
    from UPbRPy.pipeline import PipelineStage
    from UPbRPy.project import write_reduction_project, read_reduction_project
    from synthetic_bundles import write_raw_directory
    monkeypatch.setattr('UPbRPy.io.auto_select_baseline_peak', lambda experiment, *a, **k:
                        experiment.assign(baseline_start_times=0., baseline_end_times=4.,
                            plateau_start_times=8., plateau_end_times=16.,
                            signal_start_times=6., signal_end_times=18.))
    raw = tmp_path/'Apatite_synthetic'
    write_raw_directory(raw)
    shell = StudioWindow()
    shell.add_tab()
    window = shell.active_reduction
    try:
        window.load_experiment_directory(str(raw), background=False)
        settings = ExportSettings(optional_columns=('204Pb',), excluded_columns=('notes',),
                                  laboratory='Lab', a_tio2=.8, csv_full_precision=True)
        window.export_settings = settings
        workspace = window.project_workspace_state()
        target = write_reduction_project(window.session.pipeline, tmp_path/'review.upbr', workspace=workspace)
        saved = read_reduction_project(target)['workspace']
        assert ExportSettings.from_dict(saved['export_settings']) == settings
        restored = RestoredProject(window.session, window.workflow_preset, target, None,
            PipelineStage.IMPORT, (PipelineStage.IMPORT,), json.loads(json.dumps(saved)))
        window.export_settings = ExportSettings()
        window.install_restored_project(restored)
        assert window.export_settings == settings
        restored.workspace.pop('export_settings')
        window.install_restored_project(restored)
        assert window.export_settings == ExportSettings()
    finally:
        window.has_unsaved_changes = lambda: False
        shell.close()
        shell.deleteLater()
        app.processEvents()


def test_studio_routes_review_exclusions_and_logs_files(app, tmp_path, monkeypatch):
    import GUI.main_window as main_module
    from GUI.studio_window import StudioWindow
    shell = StudioWindow()
    shell.add_tab()
    window = shell.active_reduction
    pipeline = synthetic_pipeline()
    window.session.pipeline = pipeline
    window.session.directory = str(tmp_path/'session')
    window.results_table.include_flags.update({'B_0': True, 'B_3': False})
    monkeypatch.setattr(window, '_require_loaded', lambda: True)
    monkeypatch.setattr(main_module, 'ensure_shared_pipeline', lambda *args: pipeline)
    built = []
    def build(*args, **kwargs):
        built.append(kwargs)
        return build_export_table(*args, **kwargs)
    monkeypatch.setattr(main_module, 'build_export_table', build)
    labels, logs = [], []
    def background(label, calculation, on_success):
        labels.append(label)
        on_success(calculation())
    monkeypatch.setattr(window, 'run_background', background)
    monkeypatch.setattr(window, 'append_log', logs.append)
    monkeypatch.setattr(window, 'refresh_view', lambda: None)
    previous_folder = window.settings.value('publication_export_folder')
    try:
        window.choose_reduction_bundle_export()
        app.processEvents()
        dialog = window._export_dialog
        assert built == [{'excluded_spots': ('B_3',)}]
        assert dialog.excluded_spots == ('B_3',)
        assert dialog.table.rows.set_index('spot_name').loc['B_3', 'excluded']
        dialog.folder_edit.setText(str(tmp_path))
        dialog.stem_edit.setText('chosen')
        dialog.format_checks['xlsx'].setChecked(False)
        dialog.format_checks['csv'].setChecked(False)
        dialog.audit_check.setChecked(False)
        dialog.text_fields['laboratory'].setText('Lab')
        dialog.accept()
        app.processEvents()
        assert labels == ['Preparing export preview', 'Exporting results']
        assert len(built) == 1
        assert (tmp_path/'chosen.md').exists()
        assert logs == [f'Exported: {tmp_path / "chosen.md"}']
        assert window.export_settings.laboratory == 'Lab'
    finally:
        if previous_folder is None:
            window.settings.remove('publication_export_folder')
        else:
            window.settings.setValue('publication_export_folder', previous_folder)
        window.has_unsaved_changes = lambda: False
        shell.close()
        shell.deleteLater()
        app.processEvents()
