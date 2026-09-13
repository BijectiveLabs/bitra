from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from trading_research_agent.data import Direction
from trading_research_agent.envelope import Envelope
from trading_research_agent.strategy import (
    Bar,
    BracketOutcome,
    ExitError,
    ExitRule,
    PrecomputedParameterError,
    Signal,
    Target,
    ols,
    simulate_with_bracket,
    simulate_with_stop,
    stop_price,
    walk_forward,
)

S = 1_000_000_000
EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "strategies"


def example(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PAIR = example("pair_reversion")


def bar(i: int, o: str, h: str, lo: str, c: str) -> Bar:
    return Bar(i * S, (i + 1) * S, Decimal(o), Decimal(h), Decimal(lo), Decimal(c), Decimal("1"), 1)


def empty(i: int) -> Bar:
    return Bar(i * S, (i + 1) * S, None, None, None, None, Decimal("0"), 0)


# Bars and signals


def test_a_bar_is_consistent_or_it_is_refused() -> None:
    assert not bar(0, "100", "101", "99", "100.5").empty
    assert empty(0).empty
    with pytest.raises(ValueError, match="inconsistent"):
        bar(0, "100", "99", "98", "100")
    with pytest.raises(ValueError, match="no prices and no volume"):
        Bar(0, S, Decimal("1"), None, None, None, Decimal("0"), 0)
    with pytest.raises(ValueError, match="all four prices"):
        Bar(0, S, Decimal("1"), Decimal("1"), None, Decimal("1"), Decimal("1"), 1)
    with pytest.raises(ValueError, match="ends after it starts"):
        Bar(S, S, None, None, None, None, Decimal("0"), 0)
    with pytest.raises(ValueError, match="no prices"):
        empty(0).ohl()
    with pytest.raises(ValueError, match="states its reason"):
        Signal(Target.LONG, " ")


# Layer 6: the causal stop, pinned to the adverse assumptions


def test_stop_sits_on_the_adverse_side() -> None:
    assert stop_price(Decimal("100"), Direction.LONG, Decimal("0.01")) == Decimal("99.00")
    assert stop_price(Decimal("100"), Direction.SHORT, Decimal("0.01")) == Decimal("101.00")
    with pytest.raises(ExitError, match="positive"):
        stop_price(Decimal("0"), Direction.LONG, Decimal("0.01"))
    with pytest.raises(ExitError, match="positive"):
        stop_price(Decimal("100"), Direction.LONG, Decimal("0"))


def test_a_touch_is_a_fire_and_a_gap_fills_at_the_worse_open() -> None:
    bars = [bar(0, "100", "101", "99.5", "100"), bar(1, "100", "100.5", "99", "100.2")]
    out = simulate_with_stop(Direction.LONG, Decimal("99"), bars, Decimal("100.2"), Decimal("0"))
    assert out.stopped and out.bars_held == 2 and out.exit_px == Decimal("99")

    gapped = [bar(0, "98", "98.5", "97", "98")]
    out = simulate_with_stop(Direction.LONG, Decimal("99"), gapped, Decimal("98"), Decimal("5.0"))
    assert out.stopped and out.exit_px == Decimal("98") * (1 - Decimal("0.0005"))

    short = [bar(0, "100", "101.2", "99.8", "100")]
    out = simulate_with_stop(Direction.SHORT, Decimal("101"), short, Decimal("100"), Decimal("0"))
    assert out.stopped and out.exit_px == Decimal("101")


def test_a_bar_that_closes_favourably_after_touching_still_fired() -> None:
    bars = [bar(0, "100", "100.4", "98.9", "100.3")]
    out = simulate_with_stop(Direction.LONG, Decimal("99"), bars, Decimal("100.3"), Decimal("0"))
    assert out.stopped


def test_no_touch_exits_on_schedule_without_slippage_and_empty_bars_do_not_fire() -> None:
    bars = [bar(0, "100", "101", "99.5", "100.5"), empty(1), bar(2, "100.5", "101", "100", "100.8")]
    out = simulate_with_stop(Direction.LONG, Decimal("99"), bars, Decimal("100.8"), Decimal("5.0"))
    assert not out.stopped and out.bars_held == 3 and out.exit_px == Decimal("100.8")
    with pytest.raises(ExitError, match="non-negative"):
        simulate_with_stop(Direction.LONG, Decimal("99"), bars, Decimal("100"), Decimal("-1"))


def test_bracket_scores_a_two_sided_bar_as_the_stop_and_fills_tp_at_its_limit() -> None:
    two_sided = [bar(0, "100", "103", "97", "100")]
    out = simulate_with_bracket(
        Decimal("100"),
        Direction.LONG,
        Decimal("102"),
        Decimal("98"),
        two_sided,
        Decimal("100"),
        Decimal("0"),
    )
    assert out.cause is BracketOutcome.STOP_LOSS and out.exit_px == Decimal("98")

    tp_gap = [bar(0, "104", "105", "103.5", "104")]
    out = simulate_with_bracket(
        Decimal("100"),
        Direction.LONG,
        Decimal("102"),
        Decimal("98"),
        tp_gap,
        Decimal("104"),
        Decimal("5"),
    )
    assert out.cause is BracketOutcome.TAKE_PROFIT and out.exit_px == Decimal("102")

    calm = [bar(0, "100", "101", "99", "100.5")]
    out = simulate_with_bracket(
        Decimal("100"),
        Direction.LONG,
        Decimal("102"),
        Decimal("98"),
        calm,
        Decimal("100.5"),
        Decimal("5"),
    )
    assert out.cause is BracketOutcome.SCHEDULED and out.exit_px == Decimal("100.5")

    with pytest.raises(ExitError, match="LONG requires"):
        simulate_with_bracket(
            Decimal("100"),
            Direction.LONG,
            Decimal("98"),
            Decimal("102"),
            calm,
            Decimal("100"),
            Decimal("0"),
        )
    with pytest.raises(ExitError, match="SHORT requires"):
        simulate_with_bracket(
            Decimal("100"),
            Direction.SHORT,
            Decimal("102"),
            Decimal("98"),
            calm,
            Decimal("100"),
            Decimal("0"),
        )


def test_an_exit_rule_is_bound_to_its_mechanism() -> None:
    rule = ExitRule(
        Decimal("0.01"),
        timeout_bars=24,
        mechanism_bound="Funding settles every eight hours; then the forced reduction is over.",
        take_profit_sigma_frac=Decimal("0.02"),
    )
    assert rule.stop_for(Decimal("100"), Direction.SHORT) == Decimal("101.00")
    assert rule.take_profit_for(Decimal("100"), Direction.SHORT) == Decimal("98.00")
    assert (
        ExitRule(Decimal("0.01"), 1, "one two three four five six").take_profit_for(
            Decimal("1"), Direction.LONG
        )
        is None
    )
    with pytest.raises(ExitError, match="why it is where the mechanism ends"):
        ExitRule(Decimal("0.01"), 24, "looked good")
    with pytest.raises(ExitError, match="timeout_bars"):
        ExitRule(Decimal("0.01"), 0, "one two three four five six seven")


# The walk-forward harness, pinned to the defect it exists to refuse


def pair(n: int = 400, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    x = np.cumsum(rng.normal(0, 0.01, n)) + 5.0
    spread = np.zeros(n)
    for i in range(1, n):
        spread[i] = 0.8 * spread[i - 1] + rng.normal(0, 0.005)
    y = 1.4 * x + 0.3 + spread
    return y, x


def cost(envelope: Envelope) -> Any:
    channel = next(c for c in envelope.channels if c.key == "bybit/taker-taker")
    return PAIR.PairCost(channel, spread_per_leg_bps=Decimal("3.0"))


def core(envelope: Envelope, **kw: float) -> Any:
    args: dict[str, Any] = {"entry_z": 2.0, "exit_z": 0.5, "stop_z": 4.0, "max_hold": 50}
    args.update(kw)
    return PAIR.PairReversion(cost=cost(envelope), **args)


def panel(y: np.ndarray, x: np.ndarray) -> np.ndarray:
    return np.column_stack([y, x])


def test_a_precomputed_parameter_is_refused(envelope: Envelope) -> None:
    y, x = pair()
    with pytest.raises(PrecomputedParameterError):
        walk_forward(panel(y, x), core(envelope), lookback=60, precomputed={"beta": 1.4})
    with pytest.raises(ValueError, match="lookback"):
        walk_forward(panel(y[:50], x[:50]), core(envelope), lookback=60)
    with pytest.raises(ValueError, match="train_bound"):
        walk_forward(panel(y, x), core(envelope), lookback=60, train_bound=10)
    with pytest.raises(ValueError, match="thresholds"):
        core(envelope, entry_z=1, exit_z=2)


def test_parameters_are_trailing_and_frozen_at_entry(envelope: Envelope) -> None:
    y, x = pair()
    result = walk_forward(
        panel(y, x), core(envelope, entry_z=1.5, exit_z=0.3, max_hold=40), lookback=60
    )
    assert result.trades, "the constructed pair should produce trades"
    assert set(result.fits) == set(range(60, 400))
    for trade in result.trades:
        assert result.fits[trade.entry_t] == trade.params
        beta_expected, _ = ols(
            y[trade.entry_t - 60 : trade.entry_t], x[trade.entry_t - 60 : trade.entry_t]
        )
        assert trade.params.beta == pytest.approx(beta_expected)
        expected_cost = float(cost(envelope).round_trip_bps(trade.params.beta))
        assert trade.cost_bps == pytest.approx(expected_cost)
        assert trade.cost_bps > 11.0 + 6.0  # the (1 + beta) factor, never two plain legs


def test_the_timeout_fires_even_when_every_later_window_is_flat(envelope: Envelope) -> None:
    # After the entry every later price is flat, so the current trailing fit degenerates to
    # (0, 0) and fails the beta band on every bar. The frozen entry parameters still govern the
    # open position and the timeout fires on schedule. Under the ordering defect the research
    # programme found, this returned no trade at all.
    n, lookback = 200, 30
    rng = np.random.default_rng(3)
    x = np.cumsum(rng.normal(0, 0.01, n)) + 5.0
    y = x + rng.normal(0, 0.002, n)
    y[100] += 0.2
    x[101:] = x[100]
    y[101:] = y[100]
    result = walk_forward(
        panel(y, x),
        core(envelope, entry_z=5.0, exit_z=0.5, stop_z=500.0, max_hold=10),
        lookback=lookback,
    )
    assert [(t.entry_t, t.exit_t) for t in result.trades] == [(100, 110)]
    assert (result.fits[150].beta, result.fits[150].alpha) == (0.0, 0.0)


def test_walk_forward_does_not_pass_the_full_sample_fit(envelope: Envelope) -> None:
    y, x = pair(seed=7)
    full_beta, _ = ols(y, x)
    result = walk_forward(
        panel(y, x), core(envelope, entry_z=1.5, exit_z=0.3, max_hold=40), lookback=60
    )
    assert any(abs(t.params.beta - full_beta) > 1e-9 for t in result.trades)
