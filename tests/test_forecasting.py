import numpy as np

from spareai.forecasting.methods import croston, moving_average, sba, ses, tsb


def test_croston_rate_matches_intermittent_mean():
    y = np.array([0, 0, 4, 0, 0, 4, 0, 0, 4, 0, 0, 4] * 3, dtype=float)
    rate = croston(y, 1)[0]
    assert abs(rate - y.mean()) < 0.35


def test_sba_is_debiased_below_croston():
    y = np.array([0, 3, 0, 0, 5, 0, 1, 0, 0, 2, 0, 4] * 2, dtype=float)
    assert sba(y, 1)[0] < croston(y, 1)[0]


def test_tsb_decays_for_parts_that_stopped_moving():
    y = np.array([2, 0, 3, 1, 0, 2] + [0] * 24, dtype=float)
    assert tsb(y, 1)[0] < 0.25 * y[:6].mean()


def test_zero_history_gives_zero_forecast():
    y = np.zeros(24)
    for f in (croston, sba, tsb, ses):
        assert f(y, 3).sum() == 0
    assert moving_average(y, 3).sum() == 0


def test_forecasts_are_non_negative_and_right_length():
    rng = np.random.default_rng(0)
    y = rng.poisson(0.4, 36).astype(float)
    for f in (croston, sba, tsb, ses):
        out = f(y, 12)
        assert len(out) == 12 and (out >= 0).all()
