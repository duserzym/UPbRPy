"""Exact numerical and replay checks for restore work reuse."""

from dataclasses import replace
from pathlib import Path
import pickle
import re

import numpy as np
import pandas as pd
import pytest
from scipy.stats import t

import GUI.services as services
from GUI.workflows import get_workflow_preset
from UPbRPy.calibration import fit_multiplicative_calibration_model
from UPbRPy.concentration import calc_concentration
from UPbRPy.io import read_experiment_data
from UPbRPy.pipeline import FitSettings, PipelineStage, ReductionPipeline
from UPbRPy.project import restore_project_selections, selection_records, write_reduction_project
from UPbRPy.qtegra import open_qtegra


FIXTURE = Path(__file__).parent / 'fixtures/qtegra/Zircon_04Nov26_slice'


def _original_concentration(data, calibration):
    """Reference reducer retaining the former full-spot DataFrame filtering."""
    records = []
    for name, spot in data.groupby('spot_name', sort=False):
        record = {'spot_name': name, 'sample': spot['sample'].iloc[0]}
        multiplier = t.ppf(.975, len(spot) - 1)
        for channel in calibration.index:
            factor = calibration[channel]
            mean, std = spot[channel].mean(), spot[channel].std()
            filtered = spot[(spot[channel] >= mean - multiplier * std)
                            & (spot[channel] <= mean + multiplier * std)]
            element = re.sub(r'\d+', '', channel)
            record[element] = filtered[channel].mean() * factor
            record[element + '_95CI'] = (filtered[channel].std() / np.sqrt(len(filtered))
                                         * t.ppf(.975, len(filtered) - 1)) * factor
        records.append(record)
    if not records:
        return pd.DataFrame(columns=['spot_name', 'sample'])
    result = pd.DataFrame.from_records(records).set_index('spot_name', drop=False)
    result.index.name = None
    return result


@pytest.mark.parametrize('dtype', ['float64', 'float32', 'int64', 'Float64'])
def test_concentration_cached_statistics_match_original_exactly(dtype):
    rng = np.random.default_rng(91)
    data = pd.DataFrame({
        'spot_name': ['B_2', 'A_1'] * 25 + ['single_3'],
        'sample': ['B', 'A'] * 25 + ['single'],
        '238U': pd.Series(rng.integers(-100, 1000, 51), dtype=dtype),
        '208Pb': pd.Series(rng.integers(-20, 100, 51), dtype=dtype),
    })
    if dtype != 'int64':
        data.loc[2, '238U'] = np.nan
    data.index = [i // 2 for i in range(len(data))]
    calibration = pd.Series({'238U': 1.23456789, '208Pb': .0333})
    cache = {}
    original = data.copy(deep=True)
    for factors in (calibration, calibration * 3):
        actual = calc_concentration(data, factors, statistics_cache=cache)
        pd.testing.assert_frame_equal(actual, _original_concentration(data, factors), check_exact=True)
    pd.testing.assert_frame_equal(data, original, check_exact=True)
    # A new row index or channel name does not change a channel's statistics.
    renamed = data.reset_index(drop=True).rename(columns={'238U': 'UMass', '208Pb': 'PbMass'})
    factors = pd.Series({'UMass': 2., 'PbMass': 4.})
    size = len(cache)
    pd.testing.assert_frame_equal(
        calc_concentration(renamed, factors, statistics_cache=cache),
        _original_concentration(renamed, factors), check_exact=True,
    )
    assert len(cache) == size
    data.iloc[0, data.columns.get_loc('238U')] = 99999
    pd.testing.assert_frame_equal(
        calc_concentration(data, calibration, statistics_cache=cache),
        _original_concentration(data, calibration), check_exact=True,
    )


@pytest.mark.parametrize('incomplete', [None, 'selection_manual', 'selection_detection_index',
                                      'selection_error', 'spot_names'])
def test_restore_selection_shortcut_preserves_full_frame(monkeypatch, incomplete):
    import UPbRPy.io as io

    source = open_qtegra(FIXTURE)
    normal, channels, data = read_experiment_data(source)
    normal['selection_manual'] = False
    records = selection_records(normal)
    if incomplete:
        if incomplete == 'selection_manual':
            for record in records:
                record.pop(incomplete)
        else:
            records[0].pop(incomplete)
    sizes = []
    original = io.auto_select_baseline_peak

    def tracked(experiment, total, *args, **kwargs):
        sizes.append(total['spot_name'].nunique())
        return original(experiment, total, *args, **kwargs)

    monkeypatch.setattr(io, 'auto_select_baseline_peak', tracked)
    imported, new_channels, new_data = read_experiment_data(source, restored_selections=records)
    assert sizes == [1 if incomplete is None else len(normal)]
    imported['selection_manual'] = False
    if incomplete != 'spot_names':
        actual = restore_project_selections(imported, records)
        expected = restore_project_selections(normal, records)
        pd.testing.assert_frame_equal(actual, expected, check_exact=True)
    pd.testing.assert_frame_equal(data, new_data, check_exact=True)
    assert channels == new_channels


def test_export_concentration_reuses_statistics_without_changing_results(monkeypatch):
    import UPbRPy.export_table as export
    from test_export_table import synthetic_pipeline

    pipeline = synthetic_pipeline()
    concentration = pipeline.run_concentration()

    class TrackedCache(dict):
        hits = 0

        def __getitem__(self, key):
            self.hits += 1
            return super().__getitem__(key)

    cache = TrackedCache(pipeline._concentration_statistics_cache)
    pipeline._concentration_statistics_cache = cache
    cached = export.build_export_table(pipeline)
    assert cache.hits > 0
    assert pipeline.run_concentration() is concentration
    original = export.calc_concentration

    def uncached(*args, **kwargs):
        kwargs.pop('statistics_cache', None)
        return original(*args, **kwargs)

    monkeypatch.setattr(export, 'calc_concentration', uncached)
    uncached_table = export.build_export_table(pipeline)
    pd.testing.assert_frame_equal(cached.rows, uncached_table.rows, check_exact=True)
    pd.testing.assert_frame_equal(cached.sample_summaries, uncached_table.sample_summaries, check_exact=True)


def test_restore_selection_failed_template_uses_full_selection(monkeypatch):
    import UPbRPy.io as io

    source = open_qtegra(FIXTURE)
    normal, _, _ = read_experiment_data(source)
    normal['selection_manual'] = False
    records = selection_records(normal)
    calls = []
    original = io.auto_select_baseline_peak

    def pick(experiment, data, *args, **kwargs):
        calls.append(data['spot_name'].nunique())
        if calls[-1] == 1:
            experiment['selection_error'] = 'failed template'
            return experiment
        return original(experiment, data, *args, **kwargs)

    monkeypatch.setattr(io, 'auto_select_baseline_peak', pick)
    imported, _, _ = read_experiment_data(source, restored_selections=records)
    imported['selection_manual'] = False
    assert calls == [1, len(normal)]
    pd.testing.assert_frame_equal(
        restore_project_selections(imported, records), normal, check_exact=True,
    )


@pytest.mark.parametrize('change', ['missing_channel', 'invalid_time', 'repeated_time'])
def test_restore_selection_invalid_inputs_keep_full_validation(change):
    from UPbRPy.project import _restore_selection_inputs_valid

    data = pd.DataFrame({'spot_name': ['A', 'A', 'B', 'B'],
                         'Time': [0., 1., 2., 3.], '29Si': [1., 2., 3., 4.]})
    assert _restore_selection_inputs_valid(data, '29Si', None)
    overrides = None
    if change == 'missing_channel':
        overrides = {'B': 'missing'}
    elif change == 'invalid_time':
        data.loc[3, 'Time'] = np.nan
    else:
        data.loc[3, 'Time'] = 2.
    assert not _restore_selection_inputs_valid(data, '29Si', overrides)


def _custom_model(values, intercept, slope):
    return intercept + slope * values


@pytest.mark.parametrize('method, options', [
    ('mean', {}), ('linear', {}), ('polynomial', {'degree': 2}),
    ('exponential', {}), ('interpolate', {}), ('spline', {}),
    ('orthogonal_polynomial', {'degree': 2}), ('running_mean', {}),
    (_custom_model, {'p0': (1., .1)}),
])
def test_calibration_predictors_pickle_with_identical_evaluation(method, options):
    x = np.linspace(0, 5, 12)
    y = 1.1 - .12 * np.exp(-.7 * x)
    spaces = ['linear'] if callable(method) else ['linear', 'logratio']
    for space in spaces:
        model = fit_multiplicative_calibration_model(x, y, method, fit_space=space, **options)
        restored = pickle.loads(pickle.dumps(model))
        np.testing.assert_array_equal(restored(x), model(x), strict=True)
        # Check the replaced expressions independently of serialization.
        if method == 'mean':
            expected = np.full_like(x, model.parameters[0], dtype=float)
        elif method in ('linear', 'polynomial'):
            expected = np.polyval(model.parameters, x)
        elif method == 'exponential':
            a, b, c = model.parameters
            expected = a + b * np.exp(-c * (x - model.settings['x_origin']))
        elif callable(method):
            expected = method(x, *model.parameters)
        else:
            continue
        if space == 'logratio':
            expected = np.exp(expected)
        np.testing.assert_array_equal(model(x), expected, strict=True)


def test_restored_fixture_session_can_be_pickled(tmp_path, monkeypatch):
    """Replay saved fixture windows and session fits, then pickle the session."""
    preset = replace(get_workflow_preset('zircon'), plateau_filter_method='none')
    session = services.load_session(str(FIXTURE), preset=preset)
    # This fixture omits 202Hg. Supply it in memory on both imports so the
    # normal ratio/session stages can run, without changing the fixture files.
    session.total_data['202Hg'] = 1.
    session.channels.append('202Hg')
    session.primary_68_standard = session.primary_76_standard = session.primary_82_standard = 'S513'
    config = replace(services.pipeline_config_from_session(session, preset),
                     session=FitSettings(method='mean'))
    pipeline = ReductionPipeline(
        session.experiment, session.total_data, session.channels,
        services.current_standard_archive(), config,
        source_directory=FIXTURE, build_diagnostic_plots=False,
    )
    pipeline.run_session()
    project = write_reduction_project(pipeline, tmp_path / 'fixture.upbr')
    real_load = services.load_session

    def load(*args, **kwargs):
        loaded = real_load(*args, **kwargs)
        loaded.total_data['202Hg'] = 1.
        loaded.channels.append('202Hg')
        return loaded

    monkeypatch.setattr(services, 'load_session', load)
    restored = services.restore_session_from_project(project)
    assert PipelineStage.SESSION in restored.completed_stages
    unpickled = pickle.loads(pickle.dumps(restored.session))
    pd.testing.assert_frame_equal(unpickled.experiment, restored.session.experiment, check_exact=True)
    for key, result in restored.session.reduction_cache['session_correction_results']['correction_results'].items():
        original = result['predictor']
        copied = unpickled.reduction_cache['session_correction_results']['correction_results'][key]['predictor']
        np.testing.assert_array_equal(copied(original.x), original(original.x), strict=True)
