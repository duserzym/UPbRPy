"""Reference-initial common-Pb bias quantities and their Studio presentation."""

import os
from dataclasses import replace
from types import SimpleNamespace
import zipfile
from xml.etree import ElementTree as ET

import numpy as np
import pandas as pd
import pytest

from UPbRPy.bias import (
    BIAS_DATE_SYSTEMS, accepted_dates, bias_date_description, bias_summary,
    calibrated_bias_date, standard_bias_table,
)
from UPbRPy.constants import lambda_238
from UPbRPy.export_table import (
    build_export_table, secondary_summary_wide,
    summarize_reference_materials_by_system, write_export_xlsx,
)
from UPbRPy.standards import build_standard_archive


@pytest.fixture
def archive():
    return build_standard_archive()


@pytest.fixture
def primary(archive):
    """Ten primary spots on the library's mixing line, including pure radiogenic Pb."""
    fraction = np.linspace(0., .25, 10)
    material = archive.loc['MADUCSB']
    ratio_68 = material['206Pb/238U*'] / (1 - fraction)
    ratio_76 = material['207Pb/206Pb*'] * (1 - fraction) + material['207Pb/206Pb_i'] * fraction
    frame = pd.DataFrame(dict(sample='MADUCSB', spot_name=[f'MADUCSB_{i}' for i in range(10)],
                              U=100., **{'Th/U': .5, '206Pb/238U': ratio_68,
                                        '207Pb/206Pb': ratio_76, '208Pb/232Th': .03}))
    for ratio in BIAS_DATE_SYSTEMS[:3] + ('208Pb/232Th',):
        if ratio not in frame:
            frame[ratio] = .1
        for suffix, relative in (('', .04), ('_no_calibration', .01), ('_with_calibration', .03)):
            frame[ratio + '_2e' + suffix] = frame[ratio] * relative
        frame[ratio + '_age'] = 500.
        frame[ratio + '_age_2e_no_calibration'] = 5.
    frame['206Pb/238U_age'] = np.log1p(ratio_68) / lambda_238 / 1e6
    # Reported model dates deliberately differ; the reference view must not use them.
    frame['207Pb_corrected_age'] = 478.
    frame['207Pb_corrected_age_2e'] = 9.
    return frame


def test_reference_initial_corrects_primary_without_mutating_results(primary, archive):
    before = primary.copy(deep=True)
    corrected = standard_bias_table(primary, archive, date='207Pb-corrected')
    np.testing.assert_allclose(corrected.offset_percent, 0., atol=1e-6)
    assert corrected.common_pb_source.eq('initial 207Pb/206Pb 0.710 (reference)').all()
    assert corrected.date_system.eq('207Pb-corrected').all()
    assert corrected.columns[-1] == 'common_pb_source'
    total = standard_bias_table(primary, archive, date='206Pb/238U')
    assert total.offset_percent.iloc[0] == pytest.approx(0., abs=1e-10)
    assert (total.offset_percent.iloc[1:] > 0).all()
    assert (np.diff(total.offset_percent) > 0).all()
    assert total.common_pb_source.eq('').all()
    pd.testing.assert_frame_equal(before, primary)


@pytest.mark.parametrize('uncertainty,suffix', [
    ('analytical', '_no_calibration'), ('with_calibration', '_with_calibration'),
    ('analytical', ''), ('with_calibration', ''),
])
@pytest.mark.parametrize('fractionation_columns', [False, True])
def test_ratio_error_selection_and_one_call_per_material(
        primary, archive, monkeypatch, uncertainty, suffix, fractionation_columns):
    import UPbRPy.isochron as isochron

    if not suffix:
        primary = primary.drop(columns=[key for key in primary
                                        if key.endswith(('_no_calibration', '_with_calibration'))])
    ratios = ('206Pb/238U', '207Pb/206Pb', '208Pb/232Th')
    if fractionation_columns:
        # Corrected values take priority, including when errors use base-ratio aliases.
        for ratio in ratios:
            primary[ratio + '_fractionation_corrected'] = primary[ratio]
            primary[ratio] *= 1.1
    calls = []
    original = isochron.apply_common_pb_corrections

    def capture(frame, **kwargs):
        calls.append((frame.copy(), kwargs))
        return original(frame, **kwargs)

    monkeypatch.setattr(isochron, 'apply_common_pb_corrections', capture)
    table = standard_bias_table(primary, archive, date='207Pb-corrected', uncertainty=uncertainty)
    assert len(calls) == 1
    inputs, kwargs = calls[0]
    assert len(inputs) == len(primary)
    assert kwargs['common_pb_model'] is None and not kwargs['include_204_method']
    assert np.isfinite(kwargs['common_pb206_pb208'])
    for number, ratio in zip(('68', '76', '82'), ratios):
        assert kwargs[f'ratio_{number}_column'] == ratio
        assert kwargs[f'ratio_{number}_2s_column'] == ratio + '_2e'
        np.testing.assert_array_equal(inputs[ratio + '_2e'], primary[ratio + '_2e' + suffix])
    expected = original(inputs, **kwargs)
    np.testing.assert_allclose(table.date_2s, expected['207Pb_corrected_age_2e'])
    np.testing.assert_allclose(table.offset_percent, 0., atol=1e-6)


def test_missing_unused_208_inputs(primary, archive):
    sparse = primary.drop(columns=[key for key in primary if '208Pb/232Th' in key] + ['Th/U'])
    table = standard_bias_table(sparse, archive, date='207Pb-corrected')
    np.testing.assert_allclose(table.offset_percent, 0., atol=1e-6)
    assert np.isfinite(table.date_2s).all()


@pytest.mark.parametrize('uncertainty,suffix,error', [
    ('analytical', '_no_calibration', 3.), ('with_calibration', '_with_calibration', 6.),
    ('analytical', '', 9.), ('with_calibration', '', 9.),
])
def test_missing_initial_uses_reported_stacey_kramers(primary, archive, uncertainty, suffix, error):
    archive.loc['MADUCSB', '207Pb/206Pb_i'] = np.nan
    if suffix:
        primary['207Pb_corrected_age_2e' + suffix] = error
    table = standard_bias_table(primary, archive, date='207Pb-corrected', uncertainty=uncertainty)
    np.testing.assert_array_equal(table.date, primary['207Pb_corrected_age'])
    np.testing.assert_array_equal(table.date_2s, error)
    assert table.common_pb_source.eq('Stacey–Kramers model').all()


def test_date_mapping_and_descriptions():
    assert BIAS_DATE_SYSTEMS[:3] == ('206Pb/238U', '207Pb/235U', '207Pb/206Pb')
    assert BIAS_DATE_SYSTEMS[3:] == ('preferred', '207Pb-corrected')
    assert calibrated_bias_date('tera_wasserburg') == '207Pb-corrected'
    for mode in (None, 'radiogenic', 'other'):
        assert calibrated_bias_date(mode) == '206Pb/238U'
    corrected = bias_date_description('calibrated', 'tera_wasserburg')
    assert '207Pb-corrected' in corrected['axis']
    for text in ('initial 207Pb/206Pb', 'Stacey–Kramers', 'primary should sit at 0 %'):
        assert text in corrected['tooltip']
    for date in BIAS_DATE_SYSTEMS[:4]:
        text = bias_date_description(date, 'tera_wasserburg')['tooltip']
        assert 'common-Pb-corrected 206Pb/238U' in text and 'above their accepted dates' in text
    assert bias_date_description('calibrated', None) == bias_date_description('206Pb/238U', None)


def test_summary_adds_only_tw_corrected_rows(primary, archive):
    names = ('MADUCSB',)
    ordinary = summarize_reference_materials_by_system(primary, archive, names)
    radiogenic = summarize_reference_materials_by_system(primary, archive, names,
                                                         common_pb_calibration='radiogenic')
    pd.testing.assert_frame_equal(ordinary, radiogenic)
    tw = summarize_reference_materials_by_system(primary, archive, names,
                                                 common_pb_calibration='tera_wasserburg')
    assert len(tw) == len(ordinary) + 1
    pd.testing.assert_frame_equal(ordinary, tw.iloc[:-1].reset_index(drop=True))
    corrected = tw.iloc[-1]
    assert corrected['system'] == '207Pb-corrected 206Pb/238U'
    assert corrected['n'] == 10 and corrected['offset_percent'] == pytest.approx(0., abs=1e-6)
    assert corrected['accepted_date'] == accepted_dates(archive, names).loc['MADUCSB', '206Pb/238U']
    pd.testing.assert_frame_equal(secondary_summary_wide(ordinary), secondary_summary_wide(tw))
    excluded = summarize_reference_materials_by_system(primary, archive, names,
        excluded_spots=primary.spot_name, common_pb_calibration='tera_wasserburg')
    assert excluded['n'].eq(0).all()


def test_workbook_corrected_rows_and_note_leave_spot_exports_unchanged(primary, archive, tmp_path):
    from test_export_table import synthetic_pipeline

    pipeline = synthetic_pipeline()
    ordinary = build_export_table(pipeline)
    pipeline.config = replace(pipeline.config, common_pb_calibration='tera_wasserburg')
    tw = build_export_table(pipeline)
    pd.testing.assert_frame_equal(ordinary.rows, tw.rows)
    pd.testing.assert_frame_equal(secondary_summary_wide(ordinary.secondary_summary),
                                  secondary_summary_wide(tw.secondary_summary))
    assert tw.secondary_summary.system.eq('207Pb-corrected 206Pb/238U').sum() == 2
    tw.secondary_summary = summarize_reference_materials_by_system(primary, archive, ('MADUCSB',),
        common_pb_calibration='tera_wasserburg')
    path = write_export_xlsx(tw, tmp_path / 'tw.xlsx')
    with zipfile.ZipFile(path) as workbook:
        strings = [''.join(item.itertext()) for item in ET.fromstring(workbook.read('xl/sharedStrings.xml'))]
        assert '207Pb-corrected 206Pb/238U' in strings
        assert any('library initial 207Pb/206Pb' in text and 'Stacey–Kramers' in text
                   and 'Uncorrected rows include common Pb' in text for text in strings)
        assert workbook.read('xl/worksheets/sheet2.xml')


@pytest.fixture(scope='module')
def app():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    from PySide6 import QtWidgets
    from GUI.theme import build_application

    return QtWidgets.QApplication.instance() or build_application([], theme_mode='light')


def test_controls_state_migration_and_tooltip(app):
    from GUI.widgets import StandardBiasControls

    controls = StandardBiasControls()
    try:
        assert controls.date_combo.itemText(0) == 'As calibrated'
        assert controls.date_combo.itemData(0) == 'calibrated'
        assert controls.state()['version'] == 2
        assert controls.date_combo.itemText(controls.date_combo.count() - 1) == '207Pb-corrected 206Pb/238U'
        assert controls.date_combo.findText('Preferred') >= 0
        emissions = []
        controls.changed.connect(emissions.append)
        controls.set_state({'date': '206Pb/238U'})
        assert controls.state()['date'] == 'calibrated'
        controls.set_state({'date': '206Pb/238U', 'version': 2})
        assert controls.state()['date'] == '206Pb/238U'
        for date in ('preferred', '207Pb/235U', '207Pb-corrected'):
            controls.set_state({'date': date})
            assert controls.state()['date'] == date
        assert not emissions
        controls.set_date_tooltip('Reference initial Pb')
        assert controls.date_combo.toolTip() == 'Reference initial Pb'
    finally:
        controls.close()
        controls.deleteLater()


def test_show_bias_accepts_quantity_tooltip(app, primary, archive):
    from GUI.theme import THEMES
    from GUI.widgets import StandardBiasWidget

    table = standard_bias_table(primary, archive, date='207Pb-corrected')
    view = StandardBiasWidget()
    try:
        view.show_bias(table, bias_summary(table), theme=THEMES['light'],
                       x_label='U', y_label='Offset (%)', y_tooltip='Primary should sit at 0 %')
        assert view._plot_item.getAxis('left').toolTip() == 'Primary should sit at 0 %'
        view.apply_theme(THEMES['dark'])
        assert view._plot_item.getAxis('left').toolTip() == 'Primary should sit at 0 %'
        view.show_bias(table, bias_summary(table), theme=THEMES['light'],
                       x_label='U', y_label='Offset (%)')
        assert view._plot_item.getAxis('left').toolTip() == ''
    finally:
        view.close()
        view.deleteLater()


@pytest.mark.parametrize('mode,date', [('tera_wasserburg', '207Pb-corrected'),
                                       ('radiogenic', '206Pb/238U')])
def test_service_resolves_calibrated_default(primary, archive, monkeypatch, mode, date):
    from GUI import services

    pipeline = SimpleNamespace(config=SimpleNamespace(common_pb_calibration=mode),
        standard_archive=archive, experiment=pd.DataFrame(), run_baseline=lambda: pd.DataFrame(),
        run_finalize=lambda: SimpleNamespace(parallel_results=primary))
    monkeypatch.setattr(services, 'ensure_shared_pipeline', lambda *args: pipeline)
    monkeypatch.setattr('UPbRPy.bias.spot_signal_quantities', lambda *args: pd.DataFrame())
    session = SimpleNamespace(loaded=True, reduction_cache={'finalized_results': True},
                              include_calibration_uncertainty=False)
    data = services.get_standard_bias_data(session, None)
    assert data['date'] == date
    description = bias_date_description(date, mode)
    assert data['y_label'] == description['axis'] and data['date_tooltip'] == description['tooltip']
    explicit = services.get_standard_bias_data(session, None, {'date': '206Pb/238U'})
    assert explicit['date'] == '206Pb/238U'
