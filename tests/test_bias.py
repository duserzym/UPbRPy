"""Reference-material dates, offsets, plateau quantities and weighted trends."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from UPbRPy.bias import (
    BIAS_DATE_SYSTEMS, BIAS_QUANTITIES, accepted_dates, bias_summary,
    spot_signal_quantities, standard_bias_table,
)
from UPbRPy.standards import build_standard_archive


@pytest.fixture
def archive():
    return build_standard_archive()


@pytest.fixture
def results(archive):
    dates = accepted_dates(archive, ["91500", "S513"])
    samples = ["91500"] * 3 + ["S513"] * 3 + ["NIST610", "unknown"]
    frame = pd.DataFrame({"sample": samples, "spot_name": [f"spot_{i}" for i in range(8)],
                          "U": np.arange(1., 9.), "Th": np.arange(1., 9.) * .5,
                          "Pb": 12., "La": 1., "Ce": 2., "Y": 1000.})
    for system in BIAS_DATE_SYSTEMS[:3]:
        accepted = frame['sample'].map(dates[system])
        frame[system + '_age'] = accepted * (1 + frame.U * .01)
        frame[system + '_age_2e'] = accepted * .04
        frame[system + '_age_2e_no_calibration'] = accepted * .02
        frame[system + '_age_2e_with_calibration'] = accepted * .03
    frame['preferred_age_method'] = ["206Pb/238U", "207Pb/235U", "207Pb/206Pb",
                                    "204Pb-corrected 207Pb/206Pb", "207Pb-corrected",
                                    "208Pb-corrected", "206Pb/238U", "206Pb/238U"]
    systems = ["206Pb/238U", "207Pb/235U", "207Pb/206Pb", "207Pb/206Pb", "206Pb/238U", "206Pb/238U"]
    frame['preferred_age'] = [dates.loc[sample, system] * 1.05 for sample, system in zip(samples, systems)] + [1., 1.]
    frame['preferred_age_2e'] = [dates.loc[sample, system] * .01 for sample, system in zip(samples, systems)] + [1., 1.]
    return frame


def test_accepted_dates_are_system_specific(archive):
    dates = accepted_dates(archive, ["91500", "S513", "NIST610", "absent"])
    np.testing.assert_allclose(dates.loc['91500'], [1062.44, 1063.50, 1065.68], atol=.01, rtol=0)
    np.testing.assert_allclose(dates.loc['S513'], [561.18, 561.66, 563.62], atol=.01, rtol=0)
    assert dates.loc['absent'].isna().all()


def test_offsets_uncertainties_and_nonplottable_rows(results, archive):
    table = standard_bias_table(results, archive)
    assert table.spot_name.tolist() == [f'spot_{i}' for i in range(6)]
    np.testing.assert_allclose(table.offset_percent, np.arange(1., 7.), atol=1e-12)
    np.testing.assert_allclose(table.offset_2s_percent, 2.)
    np.testing.assert_allclose(table.date_2s, table.accepted * .02)
    calibrated = standard_bias_table(results, archive, uncertainty='with_calibration')
    np.testing.assert_allclose(calibrated.offset_2s_percent, 3.)
    fallback = results.drop(columns=[col for col in results if col.endswith(('_no_calibration', '_with_calibration'))])
    np.testing.assert_allclose(standard_bias_table(fallback, archive).offset_2s_percent, 4.)
    results.loc[0, 'U'] = np.nan
    results.loc[1, '206Pb/238U_age'] = np.inf
    table = standard_bias_table(results, archive)
    assert len(table) == 6 and table.plottable.tolist() == [False, False, True, True, True, True]
    assert len(standard_bias_table(results, archive, standards=['S513'])) == 3
    assert standard_bias_table(results, archive, standards=[]).empty


def test_preferred_uses_each_spots_system(results, archive):
    table = standard_bias_table(results, archive, date='preferred')
    np.testing.assert_allclose(table.offset_percent, 5.)
    np.testing.assert_allclose(table.offset_2s_percent, 1.)
    assert table.date_system.tolist() == ["206Pb/238U", "207Pb/235U", "207Pb/206Pb",
                                        "207Pb/206Pb", "206Pb/238U", "206Pb/238U"]


def test_quantities_and_signal_join(results, archive):
    np.testing.assert_allclose(standard_bias_table(results, archive, quantity='Th/U').x, .5)
    np.testing.assert_allclose(standard_bias_table(results, archive, quantity='REE').x, 3.)
    missing = standard_bias_table(results.drop(columns=['La', 'Ce']), archive, quantity='REE')
    assert missing.x.isna().all() and not missing.plottable.any()
    assert '208Pb-based' in BIAS_QUANTITIES['Pb']
    signals = pd.DataFrame({'spot_name': ['spot_1', 'spot_0'], '238U_cps': [200., 100.]})
    joined = standard_bias_table(results, archive, quantity='238U_cps', signal_quantities=signals)
    np.testing.assert_allclose(joined.x.iloc[:2], [100., 200.])
    assert not joined.plottable.iloc[2:].any()
    assert not standard_bias_table(results, archive, quantity='session_time').plottable.any()
    with pytest.raises(ValueError):
        standard_bias_table(results, archive, quantity='Y')
    with pytest.raises(ValueError):
        standard_bias_table(results, archive, date='bad')
    with pytest.raises(ValueError):
        standard_bias_table(results, archive, uncertainty='bad')


def test_plateau_uses_session_seconds_and_exclusive_upper_bound():
    experiment = pd.DataFrame({'spot_names': ['B', 'A', 'empty'], 'spot_starting_times': [160., 100., 220.],
                               'plateau_start_times': [162., 102., 222.], 'plateau_end_times': [164., 104., 224.]})
    sweeps = pd.DataFrame({'spot_name': ['A'] * 4 + ['B'] * 3,
                           'Time': [101., 102., 103., 104., 162., 163., 164.],
                           '206Pb': [100., 10., 20., 100., 30., 50., 100.],
                           '238U': [1000., 100., 200., 1000., 300., 500., 1000.]})
    quantities = spot_signal_quantities(experiment, sweeps)
    assert quantities.spot_name.tolist() == ['B', 'A', 'empty']
    np.testing.assert_allclose(quantities.session_time, [1., 0., 2.])
    np.testing.assert_allclose(quantities['238U_cps'].iloc[:2], [400., 150.])
    np.testing.assert_allclose(quantities['206Pb_cps'].iloc[:2], [40., 15.])
    assert quantities.iloc[2][['206Pb_cps', '238U_cps']].isna().all()
    assert spot_signal_quantities(experiment, sweeps, channels=('missing',)).missing_cps.isna().all()


def trend_table(x, y, errors=2.):
    return pd.DataFrame({'x': x, 'offset_percent': y, 'offset_2s_percent': errors,
                         'sample': ['A'] * 3 + ['B'] * 3, 'plottable': True, 'quantity': 'U'})


@pytest.mark.parametrize('log_x', [False, True])
def test_weighted_trend_and_means(log_x):
    x = np.arange(1., 7.)
    table = trend_table(10**x if log_x else x, 2. + .3 * x)
    summary = bias_summary(table, log_x=log_x)
    fit = summary['trend']
    assert fit['slope'] == pytest.approx(.3)
    assert fit['intercept'] == pytest.approx(2.)
    assert fit['slope_2s'] == pytest.approx(2 / np.sqrt(17.5))
    assert fit['n'] == 6 and not fit['overdispersed']
    assert fit['units'] == ('percent per decade' if log_x else 'percent per ug/g')
    assert set(summary['trend_by_sample']) == {'A', 'B'}
    assert summary['samples']['A']['weighted_mean'] == pytest.approx(2.6)
    assert summary['samples']['A']['weighted_mean_2e'] == pytest.approx(2 / np.sqrt(3))
    assert summary['samples']['A']['mswd'] == pytest.approx(.09)
    table.loc[0, 'x'] = 0.
    if log_x:
        assert bias_summary(table, log_x=True)['trend']['n'] == 5


def test_overdispersion_and_insufficient_trends():
    from scipy.stats import t
    table = trend_table(np.arange(6.), [0., 1., 8., 3., 4., 5.], .2)
    fit = bias_summary(table)['trend']
    assert fit['mswd'] > 1 and fit['overdispersed']
    assert fit['slope_2s'] == pytest.approx(2 * np.sqrt(fit['mswd'] * .1**2 / 17.5))
    assert fit['p_value'] == pytest.approx(2 * t.sf(abs(fit['slope']) / (fit['slope_2s'] / 2), 4))
    assert bias_summary(table.iloc[:2])['trend'] is None
    table.x = 1.
    assert bias_summary(table)['trend'] is None
    table.plottable = False
    assert bias_summary(table) == {'samples': {}, 'trend': None, 'trend_by_sample': {}}


def test_unequal_errors_determine_means_and_regression_weights():
    table = trend_table(np.arange(6.), [1., 1.5, 3., 3.2, 4., 6.],
                        [.2, .4, .8, 1., 2., 4.])
    summary = bias_summary(table)
    group = table.iloc[:3]
    weights = 1 / (group.offset_2s_percent.to_numpy() / 2)**2
    mean = np.average(group.offset_percent, weights=weights)
    stats = summary['samples']['A']
    assert stats['weighted_mean'] == pytest.approx(mean)
    assert stats['weighted_mean_2e'] == pytest.approx(2 / np.sqrt(weights.sum()))
    assert stats['mswd'] == pytest.approx(np.sum(weights * (group.offset_percent - mean)**2) / 2)
    design = np.column_stack([table.x, np.ones(len(table))])
    root_weights = 2 / table.offset_2s_percent.to_numpy()
    expected = np.linalg.lstsq(design * root_weights[:, None],
                               table.offset_percent * root_weights, rcond=None)[0]
    assert summary['trend']['slope'] == pytest.approx(expected[0])
    assert summary['trend']['intercept'] == pytest.approx(expected[1])
    assert bias_summary(table.assign(x=.1))['trend'] is None
    table.loc[0, 'offset_2s_percent'] = 0.
    table.loc[1, 'offset_2s_percent'] = np.nan
    assert bias_summary(table)['trend']['n'] == 4


def test_demo_session_shared_pipeline(archive):
    from UPbRPy.io import read_experiment_data
    from UPbRPy.pipeline import FitSettings, PipelineConfig, ReductionPipeline
    fixture = Path(__file__).parent / 'fixtures/qtegra/Zircon_demo'
    experiment, channels, sweeps = read_experiment_data(fixture)
    # The trimmed fixture omits 202Hg, which calc_raw_ratios requires even
    # for uncorrected dates. Supply a constant background only in memory;
    # this test makes no claim about common-Pb-corrected dates or mass 204.
    sweeps = sweeps.assign(**{'202Hg': 1.})
    channels = [*channels, '202Hg']
    config = PipelineConfig(primary_68_standard='S513', primary_76_standard='S513',
                            primary_82_standard='S513', concentration_standards=('NIST612', 'NIST610'),
                            session=FitSettings(method='mean'))
    pipeline = ReductionPipeline(experiment, sweeps, channels, archive, config,
                                 build_diagnostic_plots=False)
    signals = spot_signal_quantities(experiment, pipeline.run_baseline())
    finalized = pipeline.run_finalize()
    table = standard_bias_table(finalized.parallel_results, archive, quantity='238U_cps',
                                signal_quantities=signals)
    assert set(table.spot_name) == {'S513_628', 'S513_629', '91500_738', '91500_739', 'PL_836', 'PL_837'}
    assert table['sample'].tolist() == ['91500', '91500', 'PL', 'PL', 'S513', 'S513']
    assert np.isfinite(table.offset_percent).all()
    assert (table.offset_percent.abs() < 5).all()
    assert (table.x > 0).all()

    # These secondary RMs exercise the sample path without unpublished data.
    dates = finalized.parallel_results.set_index('spot_name')['206Pb/238U_age']
    np.testing.assert_allclose(dates.loc[['91500_738', '91500_739']],
                               [1048.856015, 1051.801173], rtol=0, atol=1e-6)
    np.testing.assert_allclose(dates.loc[['PL_836', 'PL_837']],
                               [323.475338, 322.856025], rtol=0, atol=1e-6)
