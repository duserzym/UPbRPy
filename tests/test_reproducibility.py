"""End-to-end export contracts using small, deterministic cached results."""
from dataclasses import replace
import json
from pathlib import Path
import zipfile

import pandas as pd
import pytest

from UPbRPy.pipeline import PipelineConfig, ReductionPipeline, PipelineStage
from UPbRPy.reproducibility import build_reproducibility_notebook, export_reduction_bundle
from UPbRPy.results import FinalizedReduction, ReductionDecision


def example_pipeline(tmp_path):
    experiment = pd.DataFrame({
        'spot_names': ["sample'''1"], 'baseline_start_times': [0.0],
        'baseline_end_times': [1.0], 'plateau_start_times': [2.0],
        'plateau_end_times': [3.0], 'signal_start_times': [1.5],
        'signal_end_times': [4.0], 'selection_error': [None],
        'selection_signal_duration_limit_seconds': [9.0],
    })
    frame = pd.DataFrame({'sample': ['sample'], 'spot_name': ["sample'''1"],
        'preferred_age': [100.0], 'preferred_age_2e': [2.0],
        'preferred_age_method': ['206Pb/238U'], 'preferred_age_pass': [True]})
    pipeline = ReductionPipeline(experiment, frame, [], pd.DataFrame(),
        PipelineConfig(workflow_key="test'''quoted"), source_directory=tmp_path)
    for stage in (PipelineStage.SPOT_MEANS, PipelineStage.DEPTH_PROFILE):
        pipeline.cache.set(stage, frame.copy())
    pipeline.cache.set(PipelineStage.FINALIZE, FinalizedReduction(
        frame.copy(), frame.copy(), pd.DataFrame(), pd.DataFrame(), ReductionDecision()))
    return pipeline


def test_bundle_writes_workbook_csv_notebook_and_version(tmp_path):
    pipeline = example_pipeline(tmp_path)
    bundle = export_reduction_bundle(pipeline, tmp_path/'export')
    assert not bundle.warnings
    assert all(path.exists() for path in bundle.files)
    assert any(path.suffix == '.xlsx' for path in bundle.files)
    manifest = json.loads((bundle.directory/'reduction_manifest.json').read_text())
    methods = pd.read_csv(bundle.directory/'UPbRPy_publication_results'/'methods.csv')
    assert methods.set_index('setting').loc['package_version', 'value'] == manifest['upbrpy_version']
    with zipfile.ZipFile(bundle.directory/'UPbRPy_publication_results.xlsx') as workbook:
        assert workbook.testzip() is None


def test_notebook_code_compiles_with_quoted_settings_and_spot_names(tmp_path):
    notebook = build_reproducibility_notebook(example_pipeline(tmp_path))
    ids = [cell['id'] for cell in notebook['cells']]
    assert len(set(ids)) == len(ids)
    for cell in notebook['cells']:
        if cell['cell_type'] == 'code':
            compile(''.join(cell['source']), '<export>', 'exec')
    restored = {}
    cell = next(c for c in notebook['cells'] if 'selection_records =' in ''.join(c['source']))
    pipeline = example_pipeline(tmp_path)
    restored.update(pd=pd, json=json, experiment=pipeline.experiment.copy())
    exec(''.join(cell['source']), restored)
    pd.testing.assert_frame_equal(restored['experiment'], pipeline.experiment)


def test_notebook_publication_cell_executes_with_pipeline_manifest(tmp_path):
    pipeline = example_pipeline(tmp_path)
    notebook = build_reproducibility_notebook(pipeline)
    from UPbRPy.results import build_publication_tables, export_publication_tables
    namespace = dict(pipeline=pipeline, config=pipeline.config,
        finalized=pipeline.run_finalize(), depth_profile=pipeline.run_depth_profile(),
        source_directory=tmp_path/'source', reference_library_path=None,
        build_publication_tables=build_publication_tables,
        export_publication_tables=export_publication_tables)
    cell = next(c for c in notebook['cells'] if 'manifest = pipeline.manifest' in ''.join(c['source']))
    exec(''.join(cell['source']), namespace)
    assert namespace['publication_workbook'].is_file()


def test_missing_custom_reference_library_fails_instead_of_changing_calibration(tmp_path):
    pipeline = example_pipeline(tmp_path)
    pipeline.reference_library_path = tmp_path/'missing-library'
    notebook = build_reproducibility_notebook(pipeline)
    cell = next(c for c in notebook['cells'] if 'if reference_library_path is not None:' in ''.join(c['source']))
    with pytest.raises(FileNotFoundError):
        exec(''.join(cell['source']), {'reference_library_path': pipeline.reference_library_path})


def test_gui_preserves_depth_calibration_and_titanite_material(tmp_path):
    pytest.importorskip('PySide6')
    from GUI.services import pipeline_config_from_session
    from GUI.state import ReductionSession
    from GUI.workflows import get_workflow_preset
    from UPbRPy.depth import LinearDepthCalibration
    pipeline = example_pipeline(tmp_path)
    calibration = LinearDepthCalibration(depth_per_pulse_um=0.08, repetition_rate_hz=10)
    pipeline.config = replace(pipeline.config, depth_calibration=calibration)
    session = ReductionSession(pipeline=pipeline, primary_channel='29Si')
    config = pipeline_config_from_session(session, get_workflow_preset('titanite'))
    assert config.material == 'titanite'
    assert config.depth_calibration == calibration


def test_gui_notebook_export_respects_concentration_choices(tmp_path, monkeypatch):
    pytest.importorskip('PySide6')
    from PySide6 import QtWidgets
    from GUI.main import StudioWindowController
    from GUI.theme import build_application
    from GUI.icon import build_app_icon
    import GUI.main_window as main_window
    app = build_application([], theme_mode='light')
    controller = StudioWindowController(theme_mode='light', app_icon=build_app_icon())
    shell = controller.create_window(show=False)
    window = shell.active_reduction
    pipeline = example_pipeline(tmp_path)
    window.session.pipeline = pipeline
    window.session.directory = str(tmp_path)
    window.session.include_export_concentrations = False
    window.session.include_export_depth_concentrations = False
    monkeypatch.setattr(window, '_require_loaded', lambda: True)
    monkeypatch.setattr(main_window, 'ensure_shared_pipeline', lambda *args: pipeline)
    target = tmp_path/'from-app.ipynb'
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a: (str(target), ''))
    try:
        window.choose_notebook_export()
        source = ''.join(''.join(c['source']) for c in json.loads(target.read_text())['cells'])
        assert 'include_concentrations=False' in source
        assert 'include_depth_concentrations=False' in source
    finally:
        shell.close()
        app.processEvents()


def test_pipeline_qc_uses_the_same_baseline_subtracted_signal_as_notebooks(tmp_path, monkeypatch):
    import UPbRPy.pipeline as pipeline_module
    pipeline = example_pipeline(tmp_path)
    finalized = pipeline.run_finalize()
    pipeline.cache.invalidate(PipelineStage.FINALIZE)
    corrected_signal = pd.DataFrame({'spot_name': ['sample'], '238U': [10.0]})
    monkeypatch.setattr(pipeline, 'run_baseline', lambda: corrected_signal)
    monkeypatch.setattr(pipeline, 'run_session', lambda: {'plateau_corrected': pd.DataFrame()})
    seen = {}
    def capture(*args, **kwargs):
        seen.update(kwargs)
        return finalized
    monkeypatch.setattr(pipeline_module, 'finalize_reduction_results', capture)
    pipeline.run_finalize()
    assert seen['quality_signal_data'] is corrected_signal


def test_depth_table_constructor_does_not_load_notebook_plotting_stack():
    import subprocess
    import sys
    subprocess.run([sys.executable, "-c", "from UPbRPy.isoplot import construct_final_depthpro_df; import sys; assert 'bokeh' not in sys.modules; assert 'matplotlib' not in sys.modules"], check=True)
