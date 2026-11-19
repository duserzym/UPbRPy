"""Compact Studio export choices, preview refresh and workspace persistence."""
from dataclasses import replace
import json

import pytest
pytest.importorskip('PySide6')
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtTest import QTest

from GUI.export_dialog import ExportResultsDialog, citation_html
from GUI.theme import build_application
from UPbRPy.export_table import ExportSettings, build_export_table, export_column_preset
from test_export_table import mineral_pipeline, synthetic_pipeline


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
        assert dialog.selected_formats() == ('xlsx', 'csv', 'md', 'si')
        assert dialog.format_checks['si'].text() == 'SI workbook (.xlsx)'
        assert dialog.audit_check.isChecked()
        for col in table.columns:
            if col.key in dialog.column_items:
                assert dialog.column_items[col.key].isChecked() == (not col.optional)
        assert 'footnote 1' in dialog.text_fields['laboratory'].placeholderText()
        before = dialog.preview['headings']
        dialog.group_items['Composition'].checkbox.setCheckState(QtCore.Qt.CheckState.Unchecked)
        # Exercise the debounced signal path, not just a direct method call.
        QTest.qWait(200)
        app.processEvents()
        assert all(not item.isChecked()
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
    window.results_table.set_spot_excluded({'B_3'}, True)
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
        dialog.format_checks['si'].setChecked(False)
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


@pytest.fixture
def make_dialog(app):
    dialogs = []

    def create(material='zircon', settings=None):
        pipeline = mineral_pipeline(material)
        table = build_export_table(pipeline, settings=settings or ExportSettings())
        dialog = ExportResultsDialog(table)
        dialogs.append(dialog)
        dialog.show()
        app.processEvents()
        return dialog

    yield create
    for dialog in dialogs:
        dialog.close()
        dialog.deleteLater()
    app.processEvents()


def settle(app):
    QTest.qWait(230)
    app.processEvents()


def test_presets_and_group_tristate(app, make_dialog):
    from UPbRPy.export_citations import group_info

    dialog = make_dialog()
    defaults = {key: chip.isChecked() for key, chip in dialog.column_items.items()}
    assert dialog.preset_buttons['publication'].isChecked()
    assert not dialog.preset_buttons['custom'].isEnabled()
    assert [dialog.tabs.tabText(i).replace('&&', '&') for i in range(3)] == ['Columns & footnotes', 'Table', 'Settings']
    dialog.preset_buttons['everything'].click()
    assert all(chip.isChecked() for chip in dialog.column_items.values())
    assert all(card.checkbox.checkState() == QtCore.Qt.CheckState.Checked for card in dialog.group_items.values())
    settle(app)
    everything_headings = dialog.preview['headings']
    assert dialog.table.settings.optional_columns == export_column_preset(dialog.base_table, 'everything')[0]
    dialog.preset_buttons['minimal'].click()
    for group, card in dialog.group_items.items():
        if group_info(group).section in {'trace', 'literature'}:
            assert all(not chip.isChecked() for chip in card.chips)
    settle(app)
    assert len(dialog.preview['headings']) < len(everything_headings)
    optional_keys = {col.key for col in dialog.base_table.columns if col.optional}
    assert set(dialog.table.settings.excluded_columns) == set(export_column_preset(dialog.base_table, 'minimal')[1]) - optional_keys
    assert dialog.preset_buttons['minimal'].isChecked()
    dialog.preset_buttons['publication'].click()
    assert {key: chip.isChecked() for key, chip in dialog.column_items.items()} == defaults
    dialog.column_items['U'].click()
    assert dialog.preset_buttons['custom'].isChecked()
    card = dialog.group_items['Composition']
    assert card.checkbox.checkState() == QtCore.Qt.CheckState.PartiallyChecked
    # A partially checked group selects every column on the first click, then clears it.
    card.checkbox.click()
    assert all(chip.isChecked() for chip in card.chips)
    card.checkbox.click()
    assert not any(chip.isChecked() for chip in card.chips)
    settle(app)
    assert 'Composition' not in dialog.preview['groups']


def test_filter_citations_columns_and_unchanged_choices(app, make_dialog):
    dialog = make_dialog()
    states = {key: chip.isChecked() for key, chip in dialog.column_items.items()}
    groups = set(dialog.group_items)
    for query in ('Grimes', 'doi:10.1007/s00410-015-1199-3', '1199-3'):
        dialog.filter_edit.setText(query)
        settle(app)
        assert {group for group, card in dialog.group_items.items() if not card.isHidden()} == {'Grimes et al. (2015)'}
        assert not dialog.sections['literature'][0].isHidden()
        assert dialog.sections['core'][0].isHidden()
    dialog.filter_edit.setText('204Pb')
    settle(app)
    card = dialog.group_items['Composition']
    assert not card.isHidden() and not card.body.isHidden()
    for column, chip in zip(card.columns, card.chips):
        assert chip.isHidden() == ('204pb' not in (column.label + ' ' + column.key).casefold())
    dialog.filter_edit.clear()
    settle(app)
    assert {group for group, card in dialog.group_items.items() if not card.isHidden()} == groups
    assert all(not chip.isHidden() for chip in dialog.column_items.values())
    assert {key: chip.isChecked() for key, chip in dialog.column_items.items()} == states
    assert card.body.isHidden()  # Clearing the filter restores the collapsed core card.


def test_citation_html_unverified_tag_and_popup(app, make_dialog, monkeypatch):
    from UPbRPy import export_citations

    html = citation_html('Grimes et al. (2015)')
    assert 'href="https://doi.org/10.1007/s00410-015-1199-3"' in html
    assert 'Contrib' in html and 'Used for:' in html and 'Caveat:' in html
    monkeypatch.setitem(export_citations.REFERENCES, 'grimes2015',
                        replace(export_citations.reference('grimes2015'), verified=False, note='Needs verification'))
    dialog = make_dialog()
    card = dialog.group_items['Grimes et al. (2015)']
    assert not card.unverified_tag.isHidden()
    assert card.unverified_tag.text() == 'unverified'
    assert 'Needs verification' in card.toolTip()
    assert 'https://' not in card.toolTip()
    assert dialog.group_items['Composition'].unverified_tag.isHidden()
    opened = []
    monkeypatch.setattr(QtGui.QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()))
    card.info_button.click()
    app.processEvents()
    popup = dialog._citation_popup
    assert popup.isVisible()
    label = next(label for label in popup.findChildren(QtWidgets.QLabel) if label.textFormat() == QtCore.Qt.TextFormat.RichText)
    label.linkActivated.emit('https://doi.org/10.1007/s00410-015-1199-3')
    assert opened == ['https://doi.org/10.1007/s00410-015-1199-3']
    popup.close()


def test_zircon_thermometry_and_hidden_settings(make_dialog):
    settings = ExportSettings(pressure_gpa=2.3, rutile_calibration='FW2007', citation='Session citation')
    dialog = make_dialog(settings=settings)
    card = dialog.group_items['Ti-in-zircon']
    assert card.checkbox.text() == 'Ti-in-zircon temperature'
    assert {'a_sio2', 'a_tio2'} <= dialog.numeric_fields.keys()
    assert 'pressure_gpa' not in dialog.numeric_fields
    assert not hasattr(dialog, 'calibration_combo')
    assert '4800' in dialog.numeric_fields['a_tio2'].toolTip()
    assert '4800' in card.toolTip()
    result = dialog.export_settings()
    assert result.pressure_gpa == 2.3 and result.rutile_calibration == 'FW2007'
    assert result.citation == 'Session citation'
    settings_page = dialog.tabs.widget(2)
    assert not any(spin in settings_page.findChildren(QtWidgets.QDoubleSpinBox)
                   for key, spin in dialog.numeric_fields.items() if key in {'a_sio2', 'a_tio2'})


def test_rutile_calibration_inputs_footnotes_and_retained_values(app, make_dialog):
    dialog = make_dialog('rutile')
    card = dialog.group_items['Zr-in-rutile']
    assert card.checkbox.text() == 'Zr-in-rutile temperature'
    assert dialog.calibration_combo.currentText() == 'Kohn (2020)'
    assert 'pressure_gpa' in dialog.numeric_fields
    assert 'a_sio2' not in dialog.numeric_fields and 'a_tio2' not in dialog.numeric_fields
    assert 'Kohn' in dialog.footnotes_edit.toPlainText()
    assert '71360' in dialog.calibration_combo.toolTip()
    assert 'aSiO2 = aZrSiO4 = 1' in dialog.thermometry_inputs.findChildren(QtWidgets.QLabel)[-1].text()
    dialog.numeric_fields['pressure_gpa'].setValue(1.7)
    dialog.calibration_combo.setCurrentIndex(1)
    assert dialog.calibration_combo.currentText() == 'Ferry and Watson (2007)'
    assert 'a_sio2' in dialog.numeric_fields and 'pressure_gpa' not in dialog.numeric_fields
    assert dialog.export_settings().rutile_calibration == 'FW2007'
    assert dialog.export_settings().pressure_gpa == 1.7
    dialog.numeric_fields['a_sio2'].setValue(.75)
    settle(app)
    assert 'Ferry and Watson (2007)' in dialog.footnotes_edit.toPlainText()
    assert '4530' in card.toolTip() and '71360' not in card.toolTip()
    dialog.calibration_combo.setCurrentIndex(0)
    assert dialog.numeric_fields['pressure_gpa'].value() == 1.7
    dialog.calibration_combo.setCurrentIndex(1)
    assert dialog.numeric_fields['a_sio2'].value() == .75
    # Full settings can also load an alternate calibration with signals suppressed.
    dialog.preview_timer.stop()
    settings = replace(dialog.export_settings(), rutile_calibration='Kohn2020', pressure_gpa=3.2,
                       a_sio2=.9, a_tio2=.8, laboratory='Restored lab', csv_full_precision=True,
                       discordance_definition='7/6-6/8', discordance_threshold_percent=8.2)
    dialog._set_settings(settings)
    assert dialog.export_settings() == settings
    assert not dialog.preview_timer.isActive()
    dialog.calibration_combo.setCurrentIndex(1)
    assert dialog.numeric_fields['a_sio2'].value() == .9


def test_titanite_and_material_without_thermometry(make_dialog):
    dialog = make_dialog('titanite')
    assert dialog.group_items['Zr-in-titanite'].checkbox.text() == 'Zr-in-titanite temperature'
    assert {'a_sio2', 'a_tio2', 'pressure_gpa'} <= dialog.numeric_fields.keys()
    assert not hasattr(dialog, 'calibration_combo')
    other = make_dialog('apatite', ExportSettings(a_sio2=.9, a_tio2=.8, pressure_gpa=2.))
    assert 'thermometry' not in other.sections
    assert set(other.numeric_fields) == {'discordance_threshold_percent'}
    assert (other.export_settings().a_sio2, other.export_settings().a_tio2,
            other.export_settings().pressure_gpa) == (.9, .8, 2.)


@pytest.mark.parametrize('screen_size', [(1280, 800), (1512, 982)])
def test_layout_clamp_export_reachable_and_flow(app, make_dialog, monkeypatch, screen_size):
    width, height = screen_size
    monkeypatch.setattr(QtGui.QScreen, 'availableGeometry', lambda self: QtCore.QRect(0, 0, width, height))
    dialog = make_dialog()
    assert dialog.size() == QtCore.QSize(min(1180, width - 40), min(760, height - 80))
    assert dialog.minimumSizeHint().width() <= dialog.width()
    for index in range(dialog.tabs.count()):
        dialog.tabs.setCurrentIndex(index)
        app.processEvents()
        button_rect = QtCore.QRect(dialog.export_button.mapTo(dialog, QtCore.QPoint()), dialog.export_button.size())
        assert dialog.rect().contains(button_rect)
        assert not dialog.left_scroll.horizontalScrollBar().isVisible()
        assert dialog.left_scroll.widget().width() <= dialog.left_scroll.viewport().width()
    dialog.group_items['Composition'].expand_button.click()
    dialog.filter_edit.setText('Pb')
    settle(app)
    assert not dialog.left_scroll.horizontalScrollBar().isVisible()
    # Format chips stay inside the top row, including its wrapped arrangement.
    for chip in [*dialog.format_checks.values(), dialog.audit_check]:
        rect = QtCore.QRect(chip.mapTo(dialog, QtCore.QPoint()), chip.size())
        assert dialog.rect().contains(rect)
    # Moving the divider leaves cards and their wrapping chips inside the viewport.
    dialog.splitter.setSizes([410, 700])
    dialog.filter_edit.clear()
    settle(app)
    for card in dialog.group_items.values():
        assert card.width() <= dialog.left_scroll.viewport().width()
        if card.body.isVisible():
            for chip in card.chips:
                assert chip.geometry().right() < card.body.width()


def test_old_export_settings_fill_thermometry_defaults():
    settings = ExportSettings.from_dict({'a_sio2': .9, 'a_tio2': .8, 'laboratory': 'Legacy lab'})
    assert settings.pressure_gpa == 1. and settings.rutile_calibration == 'Kohn2020'


@pytest.mark.parametrize('material', ['zircon', 'titanite'])
def test_workbook_sheet_card_defaults_settings_presets_and_preview(app, make_dialog, material):
    from UPbRPy.export_table import RICH_SHEETS

    dialog = make_dialog(material)
    card = dialog.sheet_card
    assert card.checkbox.text() == 'Workbook sheets'
    assert tuple(dialog.sheet_items) == RICH_SHEETS
    assert all(chip.isChecked() for chip in dialog.sheet_items.values())
    assert not set(dialog.sheet_items) & set(dialog.column_items)
    assert card.checkbox.checkState() == QtCore.Qt.CheckState.Checked
    assert dialog.format_checks['si'].isChecked()
    assert all('<stem>.xlsx' in chip.toolTip() for chip in dialog.sheet_items.values())
    core = dialog.sections['core'][0].layout()
    assert core.itemAt(1).widget() is card
    assert any(label.text() == 'Added to the .xlsx workbook; the SI workbook always has all five sheets'
               for label in card.findChildren(QtWidgets.QLabel))

    def preview_sheets():
        return [chip.text() for chip in dialog.exported_columns_page.findChildren(QtWidgets.QToolButton)
                if not chip.isHidden() and chip.text() in {item.text() for item in dialog.sheet_items.values()}]

    assert 'README (metadata)' in preview_sheets()
    dialog.sheet_items['readme'].click()
    assert dialog.export_settings().excluded_sheets == ('readme',)
    assert dialog.preset_buttons['custom'].isChecked()
    assert card.checkbox.checkState() == QtCore.Qt.CheckState.PartiallyChecked
    settle(app)
    assert 'README (metadata)' not in preview_sheets()
    assert any(label.text() == 'Workbook sheets · 3'
               for label in dialog.exported_columns_page.findChildren(QtWidgets.QLabel))
    for preset in ('publication', 'everything', 'minimal'):
        dialog.preset_buttons[preset].click()
        assert dialog.export_settings().excluded_sheets == ()
        dialog.sheet_items['readme'].click()
    dialog._set_settings(replace(dialog.export_settings(), excluded_sheets=('qc',)))
    assert dialog.sheet_items['readme'].isChecked() and not dialog.sheet_items['qc'].isChecked()
    card.checkbox.click()  # partial -> all, then none
    assert dialog.export_settings().excluded_sheets == ()
    card.checkbox.click()
    assert dialog.export_settings().excluded_sheets == RICH_SHEETS
    dialog.filter_edit.setText('metadata')
    settle(app)
    assert not card.isHidden() and not card.body.isHidden()
    assert not dialog.sections['core'][0].isHidden()
    dialog.filter_edit.clear()
    settle(app)
    assert card.body.isHidden()
    card.expand_button.click()
    assert not card.body.isHidden()


@pytest.mark.parametrize('height', [720, 760])
def test_compact_cards_keep_thermometry_above_fold(app, make_dialog, height):
    dialog = make_dialog()
    dialog.resize(1180, height)
    app.processEvents()
    viewport = dialog.left_scroll.viewport()
    card = dialog.group_items['Ti-in-zircon']
    top_left = card.mapTo(viewport, QtCore.QPoint())
    assert viewport.rect().contains(top_left)
    assert top_left.y() + card.checkbox.geometry().bottom() < viewport.height()
    assert list(dialog.sections) == ['core', 'trace', 'thermometry', 'literature']
    for section, (_, grid, groups) in dialog.sections.items():
        columns = 2
        for index, group in enumerate(groups):
            card = dialog.group_items[group]
            assert not hasattr(card, 'collapse_button')
            collapsible = section == 'core' or len(card.columns) > 12
            assert (card.expand_button is not None) == collapsible
            assert card.body.isHidden() == collapsible
            if collapsible:
                assert card.expand_button.text() == '▸'
                assert card.expand_button.autoRaise()
                assert card.expand_button.toolTip() == 'Show columns'
                assert card.expand_button.geometry().right() < card.checkbox.geometry().left()
            position = grid.getItemPosition(grid.indexOf(card))
            span_row = section == 'thermometry' or (section == 'literature' and index == len(groups) - 1
                                                   and index % columns == 0)
            assert position == ((index // columns, 0, 1, 2) if span_row
                                else (index // columns, index % columns, 1, 1))
    core_groups = dialog.sections['core'][2]
    expanded, sibling = (dialog.group_items[group] for group in core_groups[:2])
    sibling_height = sibling.height()
    expanded.expand_button.click()
    app.processEvents()
    assert expanded.expand_button.text() == '▾'
    assert expanded.height() > sibling.height()
    assert sibling.height() == sibling_height
    expanded.expand_button.click()
    dialog.filter_edit.setText('Analysis')
    settle(app)
    dialog.filter_edit.clear()
    settle(app)
    assert viewport.rect().contains(dialog.group_items['Ti-in-zircon'].mapTo(viewport, QtCore.QPoint()))
    assert sibling.height() == sibling_height


def test_card_titles_fit_at_1180_by_720(app, make_dialog):
    dialog = make_dialog()
    dialog.resize(1180, 720)
    app.processEvents()
    for group, card in dialog.group_items.items():
        title = card.checkbox
        option = QtWidgets.QStyleOptionButton()
        title.initStyleOption(option)
        text_rect = title.style().subElementRect(QtWidgets.QStyle.SubElement.SE_CheckBoxContents,
                                                 option, title)
        drawn = title.fontMetrics().elidedText(title.text(), QtCore.Qt.TextElideMode.ElideRight,
                                               text_rect.width())
        assert drawn == title.text(), group


def test_core_grid_reflows_on_splitter_resize_and_filter(app, make_dialog):
    dialog = make_dialog()
    dialog.resize(1180, 720)
    app.processEvents()
    _, grid, groups = dialog.sections['core']
    for sizes, columns in [([520, 630], 2), ([930, 220], 3), ([520, 630], 2)]:
        dialog.splitter.setSizes(sizes)
        app.processEvents()
        for index, group in enumerate(groups):
            card = dialog.group_items[group]
            assert grid.getItemPosition(grid.indexOf(card)) == (index // columns, index % columns, 1, 1)
        dialog.filter_edit.setText('Pb')
        settle(app)
        shown = [dialog.group_items[group] for group in groups if not dialog.group_items[group].isHidden()]
        for index, card in enumerate(shown):
            assert grid.getItemPosition(grid.indexOf(card)) == (index // columns, index % columns, 1, 1)
        dialog.filter_edit.clear()
        settle(app)


def test_single_line_headers_citations_counts_and_compact_chips(app, make_dialog):
    from UPbRPy.export_citations import group_info, reference

    dialog = make_dialog()
    for group, card in dialog.group_items.items():
        info = group_info(group)
        assert not card.checkbox.hasHeightForWidth()
        assert not card.citation_label.wordWrap()
        assert not card.count_label.wordWrap()
        count = sum(chip.isChecked() for chip in card.chips)
        assert card.count_label.text() == f'{count}/{len(card.chips)}'
        assert card.count_label.toolTip() == f'{count} of {len(card.chips)} columns'
        assert card.count_label.font().pixelSize() == 11
        assert card.layout.itemAt(0).layout().spacing() == 4
        header = card.layout.itemAt(0).layout()
        assert header.stretch(header.indexOf(card.checkbox)) > header.stretch(header.indexOf(card.count_label))
        assert card.info_button.size() == QtCore.QSize(18, 18)
        if card.expand_button:
            assert card.expand_button.size() == QtCore.QSize(18, 18)
        assert card.citation_row.isHidden() == (not info.references)
        if info.references:
            expected = group if info.section == 'literature' else reference(info.references[0]).short
            assert card.citation_label.text() == expected
            assert expected in card.citation_label.toolTip()
            for key in info.references:
                assert reference(key).title in card.toolTip()
        assert card.checkbox.height() < 35
    card = dialog.group_items['Itano and Sawada (2024)']
    assert 'Sawaki' in card.citation_label.toolTip()
    assert 'Burnham' in card.citation_label.toolTip()
    card = dialog.group_items['Composition']
    chip = card.chips[0]
    chip.click()
    count = sum(chip.isChecked() for chip in card.chips)
    assert card.count_label.text() == f'{count}/{len(card.chips)}'
    assert card.count_label.toolTip() == f'{count} of {len(card.chips)} columns'
    settle(app)
    preview_chips = [chip for chip in dialog.exported_columns_page.findChildren(QtWidgets.QToolButton)
                     if not chip.isHidden()]
    assert preview_chips
    for chip in [*dialog.column_items.values(), *preview_chips]:
        assert chip.font().pixelSize() == 11
        assert not chip.font().bold()
        assert 20 <= chip.sizeHint().height() <= 24
    # Check laid-out widgets on both sides, including labels with slashes and units.
    for card in dialog.group_items.values():
        if card.expand_button:
            card.expand_button.click()
    app.processEvents()
    for chip in [*dialog.column_items.values(), *preview_chips]:
        assert chip.geometry().right() < chip.parentWidget().width()
        if chip.fontMetrics().horizontalAdvance(chip.text()) + 18 <= chip.parentWidget().width():
            assert 20 <= chip.height() <= 24
    # A single label wider than its row wraps within that row.
    long_chip = next(chip for chip in preview_chips if len(chip.text()) > 12)
    row = long_chip.parentWidget()
    row.setFixedWidth(75)
    row.layout().activate()
    app.processEvents()
    assert long_chip.width() <= row.width()
    assert long_chip.height() > 24
    assert all(spin.width() == 80 for key, spin in dialog.numeric_fields.items()
               if key in {'a_sio2', 'a_tio2', 'pressure_gpa'})
    rutile = make_dialog('rutile')
    rutile.calibration_combo.setCurrentIndex(1)
    assert rutile.group_items['Zr-in-rutile'].citation_label.text() == 'Ferry and Watson (2007)'
