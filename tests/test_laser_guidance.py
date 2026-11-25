"""Synthetic laser guidance, default identity and project replay checks."""

from dataclasses import replace
from datetime import timedelta
import json

import numpy as np
import pandas as pd
import pytest

from UPbRPy import standard_archive
from UPbRPy.io import (
    SelectionError, autopick_signal_regions, available_selection_methods,
    read_experiment_data, select_signal_regions, selection_method_label,
    update_laser_detected_onsets,
)
import UPbRPy.io as engine_io
from UPbRPy.laser_guidance import (
    DEFAULT_LASER_QC_THRESHOLD_S,
    add_laser_timing, estimate_transport_delay, laser_selection_options, update_laser_qc,
)
from UPbRPy.pipeline import FitSettings, PipelineConfig, ReductionPipeline, pipeline_config_from_dict
from UPbRPy.project import (
    build_reduction_project, read_reduction_project, restore_project_selections,
)
from UPbRPy.reproducibility import selection_export_frame
from UPbRPy.session_manifest import read_session_manifest
import synthetic_bundles
from synthetic_bundles import manifest_toml, write_session_bundle


def import_bundle(tmp_path, *, manifest_edit=None, **options):
    bundle = write_session_bundle(tmp_path, laser=True)
    manifest = read_session_manifest(bundle / 'session_manifest.toml')
    if manifest_edit:
        manifest = manifest_edit(manifest)
    return read_experiment_data(bundle, primary_channel='44Ca', session_manifest=manifest, **options)


def pipeline(experiment, channels, total, source, config=None):
    return ReductionPipeline(
        experiment, total, channels, standard_archive,
        config or PipelineConfig(
            material='apatite', primary_channel='44Ca', primary_68_standard='MADUCSB',
            concentration_standards=('NIST612',), session=FitSettings(method='mean'),
        ), source_directory=source, build_diagnostic_plots=False,
    )


def analytical_frame(frame):
    result = frame.drop(columns=[c for c in frame if str(c).startswith('laser_')]).copy()
    result.attrs.pop('laser', None)
    return result


def test_default_identity_through_reduction_and_old_project(tmp_path, monkeypatch):
    monkeypatch.setattr(synthetic_bundles, 'CHANNELS', (
        '29Si', '44Ca', '202Hg', '204Hg', '206Pb', '207Pb', '208Pb', '232Th', '238U',
    ))
    bundle = write_session_bundle(tmp_path, laser=False)
    path = bundle / 'session_manifest.toml'
    # Use exactly the same raw data and source paths for both manifests.
    old_manifest = read_session_manifest(path)
    old, channels, total = read_experiment_data(bundle, primary_channel='44Ca')
    assert old_manifest.format_version == 1
    assert not any(c.startswith('laser_') for c in old)
    assert 'laser' not in old.attrs
    first = pipeline(old, channels, total, bundle)
    baseline = first.run_baseline()
    ratios = first.run_ratios()
    means = first.run_spot_means()
    finalized = first.run_finalize()
    project_file = tmp_path / 'old.upbr'
    project = build_reduction_project(first, project_file)
    assert 'laser_qc_threshold_s' not in json.dumps(project)
    assert 'signal_duration_limit_source' not in json.dumps(project)
    project_file.write_text(json.dumps(project))
    loaded = read_reduction_project(project_file)
    old_config = pipeline_config_from_dict(loaded['pipeline']['config'])
    replay, _, replay_data = read_experiment_data(
        bundle, primary_channel='44Ca', restored_selections=loaded['selections'],
    )
    replay = restore_project_selections(replay, loaded['selections'])
    replay_pipeline = pipeline(replay, channels, replay_data, bundle, old_config)
    pd.testing.assert_frame_equal(replay_pipeline.run_spot_means(), means, check_exact=True)

    path.write_text(manifest_toml(
        session_id=old_manifest.session_id, session_key=old_manifest.session_key,
        raw_paths=sorted((bundle / 'raw').glob('*.csv')), laser=True,
    ))
    timed, timed_channels, timed_total = read_experiment_data(bundle, primary_channel='44Ca')
    pd.testing.assert_frame_equal(analytical_frame(timed), old, check_exact=True)
    pd.testing.assert_frame_equal(timed_total, total, check_exact=True)
    second = pipeline(timed, timed_channels, timed_total, bundle)
    pd.testing.assert_frame_equal(analytical_frame(second.run_baseline()), analytical_frame(baseline), check_exact=True)
    for key, value in ratios.items():
        pd.testing.assert_frame_equal(analytical_frame(second.run_ratios()[key]), analytical_frame(value), check_exact=True)
    pd.testing.assert_frame_equal(analytical_frame(second.run_spot_means()), analytical_frame(means), check_exact=True)
    for attr in ('parallel_results', 'selected_results'):
        pd.testing.assert_frame_equal(
            analytical_frame(getattr(second.run_finalize(), attr)),
            analytical_frame(getattr(finalized, attr)), check_exact=True,
        )
    new_project = build_reduction_project(second, project_file)
    new_project.pop('saved_utc')
    project.pop('saved_utc')
    assert json.dumps(new_project) == json.dumps(project)
    # New informational columns do not enter persisted reviewed selections.
    assert selection_export_frame(timed).to_json() == selection_export_frame(old).to_json()


def test_session_axis_uses_manifest_start_and_clock_offset(tmp_path):
    def edit(manifest):
        # Offset calculation uses the manifest's aware start, not CSV's
        # rounded, naive start. Both firing endpoints stay unchanged.
        rows = tuple(replace(row, started_at=row.started_at.replace('00+00:00', '00.250000+00:00'))
                     for row in manifest.acquisitions)
        return replace(manifest, acquisitions=rows)
    experiment, _, _ = import_bundle(tmp_path, manifest_edit=edit)
    np.testing.assert_array_equal(experiment.laser_on_time, experiment.spot_starting_times + 3.75)
    np.testing.assert_array_equal(experiment.laser_off_time, experiment.spot_starting_times + 13.75)
    np.testing.assert_array_equal(experiment.laser_duration_s, 10.)
    assert experiment.attrs['laser']['transport_delay_s'] == 2.25
    assert experiment.set_index('spot_names').loc['MAD_UCSB_2', 'laser_scan_position'] == 2
    assert not any('stage' in c for c in experiment)
    json.dumps(experiment.attrs['laser'], allow_nan=False)


def test_source_independent_windows_and_no_times_are_inert():
    experiment = pd.DataFrame({'spot_names': ['A', 'B', 'C']})
    before = experiment.copy()
    add_laser_timing(experiment, {}, {})
    pd.testing.assert_frame_equal(experiment, before)
    assert experiment.attrs == before.attrs
    add_laser_timing(experiment, {'A': (3., 13.), 'B': (None, 8.)}, {'A': 20., 'B': 50.}, source='lase')
    assert experiment.laser_on_time.iloc[0] == 23.
    assert experiment.laser_off_time.iloc[1] == 58.
    assert np.isnan(experiment.laser_duration_s.iloc[1])
    assert np.isnan(experiment.laser_on_time.iloc[2])
    assert experiment.attrs['laser']['source'] == 'lase'
    assert experiment.attrs['laser']['qc_threshold_s'] == DEFAULT_LASER_QC_THRESHOLD_S


@pytest.mark.parametrize('missing', ['on', 'off', 'both'])
def test_incomplete_manifest_windows(tmp_path, missing):
    def edit(manifest):
        row = manifest.acquisitions[0]
        laser = replace(row.laser, **{
            f'laser_{endpoint}_at': None for endpoint in ('on', 'off') if endpoint == missing or missing == 'both'
        })
        return replace(manifest, acquisitions=(replace(row, laser=laser), *manifest.acquisitions[1:]))
    experiment, _, _ = import_bundle(tmp_path, manifest_edit=edit)
    spot = experiment.set_index('spot_names').loc['NIST612_1']
    assert np.isnan(spot.laser_on_time) == (missing in ('on', 'both'))
    assert np.isnan(spot.laser_off_time) == (missing in ('off', 'both'))
    assert np.isnan(spot.laser_duration_s)
    if missing == 'both':
        assert np.isnan(spot.laser_scan_position)


def test_settings_only_manifest_is_inert(tmp_path):
    bundle = write_session_bundle(tmp_path, laser=True)
    manifest = read_session_manifest(bundle / 'session_manifest.toml')
    manifest = replace(manifest,
        laser_logs=tuple(replace(log, has_laser_times=False, clock_offset_s=None) for log in manifest.laser_logs),
        acquisitions=tuple(replace(row, laser=replace(row.laser, laser_on_at=None, laser_off_at=None)) for row in manifest.acquisitions),
    )
    actual, _, _ = read_experiment_data(bundle, session_manifest=manifest)
    plain, _, _ = read_experiment_data(bundle, session_manifest=replace(manifest, laser_logs=(), acquisitions=()))
    pd.testing.assert_frame_equal(actual, plain, check_exact=True)
    assert actual.attrs == plain.attrs


def test_delay_residual_threshold_and_failed_spots(tmp_path):
    experiment, _, _ = import_bundle(tmp_path)
    assert DEFAULT_LASER_QC_THRESHOLD_S == 3.0
    assert experiment.attrs['laser']['qc_threshold_s'] == DEFAULT_LASER_QC_THRESHOLD_S
    assert estimate_transport_delay(experiment) == 2.
    assert experiment.attrs['laser']['n_pairs'] == 4
    assert (experiment.laser_onset_residual_s == 0.).all()
    assert experiment.laser_onset_flag.dtype == bool
    assert not experiment.laser_onset_flag.any()
    experiment.loc[0, 'laser_detected_onset_time'] += 3.
    update_laser_qc(experiment)
    assert experiment.laser_onset_residual_s.iloc[0] == 3.
    assert not experiment.laser_onset_flag.any()
    update_laser_qc(experiment, threshold_s=2.)
    assert experiment.laser_onset_flag.tolist() == [True, False, False, False]
    update_laser_qc(experiment, threshold_s=3.)
    assert not experiment.laser_onset_flag.any()  # Strictly greater.
    experiment.loc[:1, 'laser_detected_onset_time'] = np.nan
    update_laser_qc(experiment)
    assert estimate_transport_delay(experiment) is None
    assert experiment.laser_onset_residual_s.isna().all()
    assert experiment.attrs['laser']['n_pairs'] == 2
    # Direct API also works before the reference column has been added.
    direct = experiment.drop(columns=['laser_detected_onset_time'])
    direct['selection_method'] = 'laser_guided'
    assert estimate_transport_delay(direct) is None
    direct['selection_method'] = 'threshold'
    direct.loc[:1, 'selection_error'] = 'failed'
    assert estimate_transport_delay(direct) is None


@pytest.mark.parametrize('mode, end', [('sweeps', 43), ('signal', 60), ('duration', 38)])
def test_guided_windows_and_width_rules(mode, end):
    times = np.arange(100.) * .5 + 1000.
    values = np.where((np.arange(100) >= 20) & (np.arange(100) < 80), 1000., 10.)
    options = laser_selection_options({'laser_on_time': 1008., 'laser_off_time': 1028.}, 2.)
    result = select_signal_regions(
        values, time=times, method='laser_guided', baseline_sweep_n=8,
        baseline_offset=3, plateau_offset=3, peak_sweep_n=20,
        plateau_width_mode=mode, plateau_duration_seconds=7.5, **options,
    )
    assert result.method == 'laser_guided'
    assert selection_method_label(result.method) == 'Laser-guided'
    assert result.as_tuple() == (23, end, 9, 17)
    assert result.signal_start == 20
    assert result.signal_end == 60
    assert result.diagnostics['laser_delay_assumed_zero'] == 0
    assert autopick_signal_regions(values, time=times, method='laser_guided',
                                   baseline_sweep_n=8, baseline_offset=3, plateau_offset=3,
                                   peak_sweep_n=20, plateau_width_mode=mode,
                                   plateau_duration_seconds=7.5, **options) == result


def test_guided_guards_missing_off_and_unknown_delay():
    times = np.arange(100.) * .5
    values = np.where((times >= 10.) & (times < 30.), 1000., 10.)
    options = laser_selection_options({'laser_on_time': 10.})
    selection = select_signal_regions(values, time=times, method='laser_guided', **options)
    assert selection.signal_start == 20
    assert selection.signal_end == 59  # Existing washout rule.
    assert selection.diagnostics['laser_delay_assumed_zero'] == 1
    with pytest.raises(SelectionError, match='no laser time for this spot'):
        select_signal_regions(values, time=times, method='laser_guided')
    with pytest.raises(SelectionError, match='distinguishable from baseline'):
        select_signal_regions(np.ones(100), time=times, method='laser_guided', **options)
    with pytest.raises(SelectionError):
        select_signal_regions(values, time=times, method='laser_guided',
                              laser_predicted_on_time=10., laser_predicted_off_time=10.5)
    limited = select_signal_regions(values, time=times, method='laser_guided',
                                    laser_predicted_on_time=10., laser_predicted_off_time=30.,
                                    signal_duration_limit_seconds=5., plateau_width_mode='signal')
    assert limited.signal_end == 30
    assert limited.plateau_end == 30


@pytest.mark.parametrize('fallbacks, reference', [((), 'threshold'), (('laser_guided', 'rolling_sigma', 'threshold'), 'rolling_sigma')])
def test_guided_reference_detector_and_missing_time_fallback(tmp_path, fallbacks, reference):
    def edit(manifest):
        return replace(manifest, acquisitions=(*manifest.acquisitions[:3], replace(manifest.acquisitions[3], laser=None)))
    experiment, _, _ = import_bundle(tmp_path, manifest_edit=edit,
                                     selection_method='laser_guided', selection_fallback_methods=fallbacks)
    assert experiment.attrs['laser']['reference_method'] == reference
    assert experiment.attrs['laser']['transport_delay_s'] == 2.
    indexed = experiment.set_index('spot_names')
    assert indexed.loc['NIST612_1', 'selection_method'] == 'laser_guided'
    assert indexed.loc['NIST612_1', 'signal_start_times'] == 6.
    assert indexed.loc['NIST612_1', 'signal_end_times'] == 16.
    if fallbacks:
        assert indexed.loc['KD27_4', 'selection_method'] == reference
        assert indexed.loc['KD27_4', 'selection_error'] is None
    else:
        assert 'no laser time for this spot' in indexed.loc['KD27_4', 'selection_error']
    # QC compares independently detected starts, never the guided prediction.
    np.testing.assert_allclose(experiment.laser_detected_onset_time, experiment.spot_starting_times + 6.)
    assert experiment.laser_onset_residual_s.dropna().eq(0.).all()


@pytest.mark.parametrize('fixed', [None, 7.])
def test_laser_duration_source_and_fixed_fallback(tmp_path, fixed):
    def edit(manifest):
        row = manifest.acquisitions[0]
        return replace(manifest, acquisitions=(replace(row, laser=replace(row.laser, laser_off_at=None)), *manifest.acquisitions[1:]))
    experiment, _, _ = import_bundle(tmp_path, manifest_edit=edit, signal_duration_limit_source='laser',
                                     signal_duration_limit_seconds=fixed)
    limits = experiment.set_index('spot_names').selection_signal_duration_limit_seconds
    assert limits.loc['AFB_3'] == 10.
    assert pd.isna(limits.loc['NIST612_1']) if fixed is None else limits.loc['NIST612_1'] == fixed
    assert experiment.attrs['selection_parameters']['signal_duration_limit_source'] == 'laser'
    assert experiment.set_index('spot_names').loc['AFB_3', 'selection_signal_duration_limit_applied'] == 1


def test_settings_roundtrip_defaults_and_per_spot_api(tmp_path):
    assert 'laser_guided' not in available_selection_methods()
    assert 'laser_guided' in available_selection_methods(include_laser=True)
    default = PipelineConfig().to_dict()
    explicit = PipelineConfig(selection_options={'laser_qc_threshold_s': DEFAULT_LASER_QC_THRESHOLD_S, 'signal_duration_limit_source': 'fixed'}).to_dict()
    assert explicit == default
    nested = PipelineConfig(selection_options={'selection_options': {
        'laser_qc_threshold_s': DEFAULT_LASER_QC_THRESHOLD_S, 'signal_duration_limit_source': 'fixed',
    }}).to_dict()
    assert nested == default
    plain_bundle = write_session_bundle(tmp_path / 'plain')
    plain, _, _ = read_experiment_data(plain_bundle)
    explicit_defaults, _, _ = read_experiment_data(plain_bundle, selection_options={
        'laser_qc_threshold_s': DEFAULT_LASER_QC_THRESHOLD_S, 'signal_duration_limit_source': 'fixed',
    })
    pd.testing.assert_frame_equal(explicit_defaults, plain, check_exact=True)
    assert explicit_defaults.attrs == plain.attrs
    # An unused fallback remains lazy, as it was before laser guidance.
    unused, _, _ = read_experiment_data(
        plain_bundle, selection_fallback_methods=('unused_unknown_method',),
    )
    pd.testing.assert_frame_equal(unused, plain, check_exact=True)
    config = PipelineConfig(selection_method='laser_guided', selection_options={
        'laser_qc_threshold_s': .5, 'signal_duration_limit_source': 'laser',
    })
    assert pipeline_config_from_dict(config.to_dict()) == config
    experiment, channels, total = import_bundle(tmp_path, laser_qc_threshold_s=.5,
                                                signal_duration_limit_source='laser', selection_method='laser_guided')
    assert experiment.attrs['laser']['qc_threshold_s'] == .5
    spot = experiment.set_index('spot_names').loc['AFB_3']
    spot_data = total.loc[total.spot_name == 'AFB_3']
    selection = autopick_signal_regions(
        spot_data['44Ca'], time=spot_data.Time, method='laser_guided',
        **laser_selection_options(spot, experiment.attrs['laser']['transport_delay_s'],
                                  signal_duration_limit_source='laser'),
    )
    assert spot_data.Time.iloc[selection.signal_start] == spot.signal_start_times
    assert spot_data.Time.iloc[selection.signal_end] == spot.signal_end_times
    project = build_reduction_project(pipeline(experiment, channels, total, tmp_path, config), tmp_path / 'laser.upbr')
    assert project['pipeline']['config']['selection_method'] == 'laser_guided'
    assert project['pipeline']['config']['selection_options']['signal_duration_limit_source'] == 'laser'


@pytest.mark.parametrize('threshold', [-1., float('nan'), float('inf')])
def test_invalid_qc_threshold(tmp_path, threshold):
    with pytest.raises(ValueError, match='laser_qc_threshold_s'):
        import_bundle(tmp_path, laser_qc_threshold_s=threshold)


def test_guided_qc_is_independent_of_predicted_onset(tmp_path):
    def edit(manifest):
        row = manifest.acquisitions[0]
        laser = replace(row.laser, laser_on_at=row.laser.laser_on_at - timedelta(seconds=3))
        return replace(manifest, acquisitions=(replace(row, laser=laser), *manifest.acquisitions[1:]))
    experiment, _, _ = import_bundle(tmp_path, manifest_edit=edit, selection_method='laser_guided',
                                     laser_qc_threshold_s=2.)
    spot = experiment.set_index('spot_names').loc['NIST612_1']
    assert experiment.attrs['laser']['transport_delay_s'] == 2.
    assert spot.selection_error is None
    assert spot.signal_start_times == 3.
    assert spot.laser_detected_onset_time == 6.
    assert spot.laser_onset_residual_s == 3.
    assert spot.laser_onset_flag


def test_guided_fallback_uses_delay_from_other_spots(tmp_path, monkeypatch):
    def fail_one(values, **options):
        # NIST612 uses 29Si (one channel unit); other spots use 44Ca (two).
        if max(values) < 2000.:
            raise SelectionError('synthetic detector failure')
        return engine_io.threshold_onset(values, **options)
    monkeypatch.setitem(engine_io._SELECTORS, 'threshold', fail_one)
    experiment, _, _ = import_bundle(
        tmp_path, selection_fallback_methods=('laser_guided', 'rolling_sigma'),
    )
    spot = experiment.set_index('spot_names').loc['NIST612_1']
    assert experiment.attrs['laser']['transport_delay_s'] == 2.
    assert experiment.attrs['laser']['reference_method'] == 'rolling_sigma'
    assert spot.selection_method == 'laser_guided'
    assert spot.signal_start_times == 6.
    assert spot.laser_detected_onset_time == 6.
    assert spot.laser_onset_residual_s == 0.


@pytest.mark.parametrize('method, fallbacks, detector', [
    ('rolling_sigma', ('laser_guided', 'threshold'), 'rolling_sigma'),
    ('laser_guided', ('laser_guided', 'running_5sigma', 'threshold'), 'rolling_sigma'),
    ('laser_guided', (), 'threshold'),
])
def test_reference_onsets_use_configured_data_detector(tmp_path, monkeypatch, method, fallbacks, detector):
    experiment, _, total = import_bundle(tmp_path)
    experiment['selection_manual'] = True
    experiment['signal_start_times'] += 20.
    before = analytical_frame(experiment)
    calls = []
    original = engine_io._SELECTORS[detector]

    def record(values, **options):
        calls.append((np.asarray(values).copy(), options))
        return original(values, **options)

    monkeypatch.setitem(engine_io._SELECTORS, detector, record)
    monkeypatch.setitem(engine_io._SELECTORS, 'laser_guided',
                        lambda *a, **k: pytest.fail('Laser prediction used for reference'))
    update_laser_detected_onsets(
        experiment, total, '44Ca', selection_method=method,
        selection_fallback_methods=fallbacks, baseline_sweep_n=8,
        selection_channels={'NIST612': '44Ca'},
        selection_options={'rolling_window': 5, 'sigma_threshold': 4., 'consecutive': 2},
    )
    assert len(calls) == len(experiment)
    assert all(options['baseline_sweep_n'] == 8 and options['sigma_threshold'] == 4.
               for _, options in calls)
    np.testing.assert_array_equal(calls[0][0], total.loc[total.spot_name == 'NIST612_1', '44Ca'])
    np.testing.assert_allclose(experiment.laser_detected_onset_time, experiment.spot_starting_times + 6.)
    pd.testing.assert_frame_equal(analytical_frame(experiment), before, check_exact=True)


def test_failed_reference_pick_never_reuses_reviewed_onset(tmp_path, monkeypatch):
    experiment, _, total = import_bundle(tmp_path)
    experiment['selection_manual'] = True
    before = analytical_frame(experiment)

    def fail(*args, **kwargs):
        raise SelectionError('synthetic data-only failure')

    monkeypatch.setitem(engine_io._SELECTORS, 'threshold', fail)
    update_laser_detected_onsets(experiment, total, '44Ca')
    update_laser_qc(experiment)
    assert experiment.laser_detected_onset_time.isna().all()
    assert experiment.attrs['laser']['n_pairs'] == 0
    assert experiment.attrs['laser']['transport_delay_s'] is None
    pd.testing.assert_frame_equal(analytical_frame(experiment), before, check_exact=True)
