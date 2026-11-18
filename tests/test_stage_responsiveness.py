"""Regression guards for stage serialization and result installation costs."""
import os
from pathlib import Path
import subprocess
import sys


def test_pyside_session_pickle_keeps_notebook_facade_unloaded():
    pytest.importorskip("PySide6")
    code = '''
import sys, pickle
import PySide6.QtCore
from UPbRPy.qtegra import open_qtegra
session = open_qtegra('tests/fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv')
pickle.loads(pickle.dumps(session, protocol=5))
import UPbRPy
assert not hasattr(UPbRPy, '__wrapped__')
assert not hasattr(UPbRPy, '__missing_dunder__')
assert 'UPbRPy.UPbRPy' not in sys.modules
assert not any(n.split('.')[0] in {'bokeh', 'matplotlib'} for n in sys.modules)
'''
    result = subprocess.run([sys.executable, '-B', '-c', code],
                            cwd=Path(__file__).resolve().parents[1],
                            env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_dump_uses_plain_pickler_without_references(monkeypatch):
    import pickle
    from GUI import stage_process
    actual = pickle.Pickler
    classes = []
    class RecordingPickler(actual):
        def __init__(self, *args, **kwargs):
            classes.append(type(self))
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(pickle, 'Pickler', RecordingPickler)
    assert stage_process._load(stage_process._dump({'a': [1, 2]})) == {'a': [1, 2]}
    assert classes == [RecordingPickler]
    obj = [1, 2]
    restored = stage_process._load(stage_process._dump([obj], {id(obj): 'input'}), {'input': obj})
    assert restored[0] is obj
    assert classes[-1] is not RecordingPickler


import pandas as pd
import pytest
from GUI import services, stage_process
from GUI.workflows import get_workflow_preset
from UPbRPy.export_table import ExportSettings
from UPbRPy import pipeline as pipeline_module

RICH = Path(__file__).parent / 'fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv'
PRESET = get_workflow_preset('zircon')


@pytest.mark.parametrize('change', ['unchanged', 'in_place', 'replacement', 'nested'])
def test_merge_hashes_only_when_snapshot_changed(monkeypatch, change):
    session = services.load_session(str(RICH), preset=PRESET)
    task = stage_process.StageTask()
    task.prepare(session, PRESET.key, ExportSettings(), 'baseline')
    # Exercise the identical wire representation without starting a child.
    kind, arguments, references = stage_process._load(task._packet)
    child, key, settings, stage, stages, archive, path = arguments
    result = stage_process.run_stage(child, key, settings, stage, stages)
    reply = stage_process.StageReply(result, child.pipeline, child.reduction_cache)
    reply = stage_process._load(stage_process._dump(reply, {id(v): k for k, v in references.items()}), task._references)
    reply._experiment_token = task._experiment_token
    if change == 'in_place':
        session.experiment.loc[0, 'baseline_start_times'] += .1
    elif change == 'replacement':
        session.experiment = session.experiment.copy(deep=True)
    elif change == 'nested':
        session.experiment.attrs['during_run'] = {'nested': [1, 2]}
    calls = []
    original = pipeline_module._experiment_fingerprint
    def count(frame):
        calls.append(frame)
        return original(frame)
    monkeypatch.setattr(pipeline_module, '_experiment_fingerprint', count)
    reply.merge(session, PRESET)
    assert len(calls) == (0 if change == 'unchanged' else 1 if change == 'replacement' else 2)
    assert session.pipeline.matches_source_experiment(session.experiment)
    if change != 'unchanged':
        assert not session.pipeline.cache.has(pipeline_module.PipelineStage.BASELINE)


def test_spot_row_cache_is_exact_reused_and_weak():
    import gc
    import weakref
    from types import SimpleNamespace
    frame = pd.DataFrame({'spot_name': ['b', 'a', None, 'b'], 'value': pd.Series([1, 2, 3, 4], dtype='int32')}, index=[3, 3, 1, 0])
    frame.attrs['source'] = 'test'
    session = SimpleNamespace(loaded=True, total_data=frame)
    for name in ('a', 'b', 'missing'):
        pd.testing.assert_frame_equal(services.get_spot_data(session, name), frame[frame.spot_name == name].reset_index(drop=True), check_exact=True)
    positions = services._rows_by_spot(frame)
    assert services._rows_by_spot(frame) is positions
    frame.loc[:, 'value'] = 7
    assert (services.get_spot_data(session, 'b').value == 7).all()
    session.total_data = frame.iloc[::-1].copy()
    assert services._rows_by_spot(session.total_data) is not positions
    reference, key = weakref.ref(frame), id(frame)
    del frame
    gc.collect()
    assert reference() is None
    assert key not in services._spot_row_positions
