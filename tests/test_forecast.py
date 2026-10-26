from __future__ import annotations

import numpy as np
import pytest

from trading_research_agents.forecast.calibration import Isotonic, Platt, choose_calibrator
from trading_research_agents.forecast.decision import best_side, expected_bps
from trading_research_agents.forecast.logistic import ForecastError, Logistic


def sample(n: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 3))
    logit = 1.5 * x[:, 0] - 1.0 * x[:, 1]
    y = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(float)
    return x, y


def test_a_logistic_fit_finds_the_signs_and_is_the_same_every_time() -> None:
    x, y = sample(2000, 1)
    first = Logistic.fit(x, y)
    again = Logistic.fit(x, y)
    assert np.array_equal(first.weights, again.weights)
    assert first.weights[1] > 0.8 and first.weights[2] < -0.5 and abs(first.weights[3]) < 0.2
    p = first.predict(x)
    assert p.min() > 0 and p.max() < 1
    assert p[x[:, 0] > 1].mean() > p[x[:, 0] < -1].mean()


def test_a_fit_without_both_outcomes_or_with_too_few_rows_is_refused() -> None:
    x, _ = sample(300, 2)
    with pytest.raises(ForecastError, match="both outcomes"):
        Logistic.fit(x, np.ones(300))
    with pytest.raises(ForecastError, match="rows"):
        Logistic.fit(x[:10], np.array([0.0, 1.0] * 5))
    with pytest.raises(ForecastError, match="not finite"):
        Logistic.fit(np.full((300, 3), np.nan), np.array([0.0, 1.0] * 150))


def test_a_column_that_never_moves_does_not_break_the_fit() -> None:
    x, y = sample(500, 3)
    x[:, 2] = 4.0
    model = Logistic.fit(x, y)
    assert np.all(np.isfinite(model.predict(x)))


def test_platt_and_isotonic_map_a_score_onto_the_outcome_rate() -> None:
    rng = np.random.default_rng(4)
    raw = rng.random(4000)
    y = (rng.random(4000) < 0.2 + 0.5 * raw).astype(float)
    for calibrator in (Platt.fit(raw, y), Isotonic.fit(raw, y)):
        mapped = calibrator.map(np.array([0.05, 0.5, 0.95]))
        assert np.all(np.diff(mapped) >= 0)
        assert abs(mapped[1] - 0.45) < 0.06
        assert mapped.min() > 0 and mapped.max() < 1


def test_the_calibrator_is_chosen_out_of_sample_and_deterministically() -> None:
    rng = np.random.default_rng(5)
    raw = rng.random(1000)
    y = (rng.random(1000) < raw).astype(float)
    chosen = choose_calibrator(raw, y)
    assert chosen.name in ("platt", "isotonic")
    assert choose_calibrator(raw, y).name == chosen.name


def test_expected_value_is_take_against_stop_net_of_the_round_trip() -> None:
    assert expected_bps(0.5, take_bps=60, stop_bps=30, cost_bps=10) == pytest.approx(5.0)
    assert expected_bps(0.3, take_bps=60, stop_bps=30, cost_bps=10) == pytest.approx(-13.0)


def test_the_better_side_trades_only_when_its_value_clears_the_margin() -> None:
    long_ev = expected_bps(0.5, take_bps=60, stop_bps=30, cost_bps=10)
    assert best_side(long_ev, -20.0, margin_bps=0.0) == ("long", long_ev)
    assert best_side(-1.0, 3.0, margin_bps=0.0) == ("short", 3.0)
    assert best_side(4.0, 3.0, margin_bps=5.0) is None
    assert best_side(None, None, margin_bps=0.0) is None


def test_a_calibration_on_few_outcomes_still_fits_and_none_at_all_is_refused() -> None:
    rng = np.random.default_rng(6)
    raw = rng.random(60)
    y = (rng.random(60) < raw).astype(float)
    assert choose_calibrator(raw, y).name in ("platt", "isotonic")
    # Outcomes that never hit: only the step curve fits, and it says as little as it may.
    never = choose_calibrator(raw[:20], np.zeros(20))
    assert never.name == "isotonic" and float(never.map(np.array([0.9]))[0]) == 0.001
    with pytest.raises(ForecastError, match="no calibrator"):
        choose_calibrator(np.array([]), np.array([]))
