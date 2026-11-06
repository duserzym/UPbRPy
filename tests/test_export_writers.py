"""Publication writers checked through portable formats and standard-library XML."""
from dataclasses import replace
import csv
import json
import re
import zipfile
from xml.etree import ElementTree as ET

import numpy as np
import pandas as pd
import pytest

from UPbRPy.export_table import (
    ExportColumn, ExportSettings, build_export_table, export_table_preview,
    format_value, update_export_settings, write_export_csv, write_export_markdown,
    write_export_xlsx,
)
from UPbRPy.reproducibility import export_reduction_bundle
from test_export_table import synthetic_pipeline


@pytest.mark.parametrize('kind,key,value,expected', [
    ('date', 'date', 123.4567, '123.5'), ('ratio_68', 'r', .12345678, '0.12346'),
    ('ratio_75', 'r', .12345678, '0.1235'), ('ratio_86', 'r', 12.34567, '12.346'),
    ('rho', 'r', .9876, '0.99'), ('pct_2s', 'error', .126, '0.13'),
    ('pct_2s', 'error', 1.26, '1.3'), ('pct_2s', 'disc_2s_pct', .126, '0.1'),
    ('conc', 'U', 9.876, '9.88'), ('conc', 'U', 98.76, '98.8'),
    ('conc', 'U', 987.6, '988'), ('ratio_el', 'Th/U', .012345, '0.0123'),
    ('int', 'cps', 123.6, '124'), ('temp', 'T', 703.6, '704'),
    ('date', 'x', np.nan, ''), ('conc', 'x', np.inf, ''), ('text', 'x', None, ''),
])
def test_format_value(kind, key, value, expected):
    col = ExportColumn(key, 'group', 'label', kind=kind)
    assert format_value(col, value) == expected
    if value is not None and np.isfinite(value):
        assert format_value(col, value, full_precision=True) == str(value)


def test_xlsx_structure_precision_notes_and_reference_views(tmp_path):
    table = build_export_table(synthetic_pipeline(), excluded_spots=('B_3',))
    table.rows.loc[0, '206Pb/238U'] = .1234567890123
    target = write_export_xlsx(table, tmp_path/'table.xlsx')
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(target) as archive:
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        styles = ET.fromstring(archive.read('xl/styles.xml'))
        shared = ET.fromstring(archive.read('xl/sharedStrings.xml'))
        texts = [''.join(si.itertext()) for si in shared]
        assert sheet.find('s:mergeCells', ns) is not None
        assert any(cell.attrib['ref'].endswith('2') for cell in sheet.findall('s:mergeCells/s:mergeCell', ns))
        assert sheet.find('.//s:pane', ns).attrib['state'] == 'frozen'
        assert sheet.find('.//s:pane', ns).attrib['xSplit'] == '1'
        assert sheet.find('.//s:pane', ns).attrib['ySplit'] == '3'
        codes = {fmt.attrib['formatCode'] for fmt in styles.findall('s:numFmts/s:numFmt', ns)}
        assert {'0.00000', '[<10]0.00;[<100]0.0;0', '0.0'} <= codes
        assert any('Dwell times per channel' in text for text in texts)
        assert any('Reference materials' == text for text in texts)
        assert 'Primary standard' in texts and 'Secondary standards' in texts and 'Reference glasses' in texts
        assert not any(re.search(r'\bnan\b', text, re.I) for text in texts)
        assert '.1234567890123' in archive.read('xl/worksheets/sheet1.xml').decode()
        assert shared.find('.//s:vertAlign[@val="superscript"]', ns) is not None
        names = ET.fromstring(archive.read('xl/workbook.xml'))
        assert [s.attrib['name'] for s in names.findall('s:sheets/s:sheet', ns)] == ['Table', 'Reference materials']
        assert '_xlnm.Print_Titles' in archive.read('xl/workbook.xml').decode()
        assert b'Per-system secondary summary' in archive.read('xl/sharedStrings.xml')


def test_csv_markdown_and_preview(tmp_path):
    table = build_export_table(synthetic_pipeline(), excluded_spots=('B_3',))
    table.rows.loc[0, 'notes'] = 'text | with a pipe'
    paths = write_export_csv(table, tmp_path/'table.csv')
    assert len(paths) == 3 and all(p.exists() for p in paths)
    assert all(p.read_bytes().startswith(b'\xef\xbb\xbf') for p in paths)
    with paths[0].open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.reader(stream))
    assert rows[0][0] == 'Sample'
    assert rows[0][1].startswith('Analysis: Analysis [')
    assert len(rows) == len(table.rows) + 1
    assert all(len(r) == len(rows[0]) for r in rows)
    assert 'Dwell times' in paths[-1].read_text(encoding='utf-8-sig')
    md = write_export_markdown(table, tmp_path/'table.md').read_text()
    lines = [line for line in md.splitlines() if line.startswith('|')]
    counts = [len(re.split(r'(?<!\\)\|', line)) for line in lines]
    assert len(set(counts)) == 1
    assert r'text \| with a pipe' in md
    assert '<sup>' in md and '| **B (n = 2, 1 excluded)' in md
    preview = export_table_preview(table)
    assert len(preview['rows']) == 8
    assert preview['row_kinds'][0] == 'sample'
    assert len(preview['headings']) == len(table.visible_columns())
    assert all(len(r) == len(preview['headings']) for r in preview['rows'])
    precise = replace(table, settings=replace(table.settings, csv_full_precision=True))
    write_export_csv(precise, tmp_path/'precise.csv')
    col = next(c for c in table.visible_columns() if c.kind == 'conc')
    with (tmp_path/'precise.csv').open(encoding='utf-8-sig') as stream:
        full = list(csv.reader(stream))
    index = list(table.visible_columns()).index(col) + 1
    assert full[1][index] == str(table.rows.iloc[0][col.key])


def test_settings_refresh_matches_new_table_without_engine_calls(monkeypatch):
    pipeline = synthetic_pipeline()
    settings = ExportSettings(discordance_definition='7/6-6/8', discordance_threshold_percent=.1,
                              a_tio2=.9, a_sio2=.8)
    base = build_export_table(pipeline)
    expected = build_export_table(pipeline, settings)
    monkeypatch.setattr(pipeline, 'run_finalize', lambda: pytest.fail('engine called during preview'))
    actual = update_export_settings(base, settings)
    pd.testing.assert_frame_equal(actual.rows, expected.rows)
    assert actual.settings == settings
    assert actual.context['manifest']['metadata']['export_settings'] == settings.to_dict()
    assert 'aTiO2 = 0.9' in export_table_preview(actual)['footnotes']


def test_bundle_preserves_audit_outputs_and_records_settings(tmp_path):
    pipeline = synthetic_pipeline()
    pipeline.source_directory = tmp_path/'session'
    # Audit export uses spot means/depth; the publication builder uses cached final results.
    monkey_frame = pipeline.run_finalize().parallel_results
    pipeline.run_spot_means = lambda: monkey_frame.copy()
    pipeline.run_depth_profile = lambda: monkey_frame.copy()
    settings = ExportSettings(operator='Operator', optional_columns=('204Pb',))
    bundle = export_reduction_bundle(pipeline, tmp_path/'out', export_settings=settings, excluded_spots=('B_3',))
    names = {p.name for p in bundle.files}
    assert {'session_results_table.xlsx', 'session_results_table.csv', 'session_results_table.md',
            'session_results_table_samples.csv', 'session_results_table_footnotes.txt',
            'UPbRPy_publication_results.xlsx', 'UPbRPy_reproducible_workflow.ipynb',
            'selection_windows.csv', 'spot_mean_results.csv', 'depth_profile_results.csv'} <= names
    manifest = json.loads((bundle.directory/'reduction_manifest.json').read_text())
    assert manifest['metadata']['export_settings'] == settings.to_dict()
    assert manifest['metadata']['excluded_spots'] == ['B_3']
    table_only = export_reduction_bundle(pipeline, tmp_path/'table-only', formats=('md',),
        include_audit_files=False, file_stem='chosen', table=build_export_table(pipeline, settings))
    assert [p.name for p in table_only.files] == ['chosen.md']
    with pytest.raises(ValueError, match='formats'):
        export_reduction_bundle(pipeline, tmp_path/'bad', formats=('unknown',))
