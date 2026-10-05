"""Regime, higher timeframes, book imbalance and open interest in the technical brief."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from test_config import agent_raw
from test_cycle import (
    MINUTE_NS,
    NOW,
    analyst_answer,
    candles,
    cycle_config,
    snapshot_with,
)
from trading_research_agents.agents.briefs import (
    book_imbalance,
    interval_name,
    open_interest_change,
    technical_brief,
)
from trading_research_agents.agents.cycle import run_cycle
from trading_research_agents.agents.technical_analyst.tools import trend_strength
from trading_research_agents.data.sources.base import BookLevel, Fetched, OrderBook
from trading_research_agents.envelope.model import Envelope
from trading_research_agents.execution.regime import RegimeError, RegimeRule, label, misfit
from trading_research_agents.trading.config import ConfigError, parse_strategy_config

RULE = RegimeRule()


def test_trend_strength_tells_a_trend_from_a_market_going_nowhere() -> None:
    line = [100.0 + i for i in range(60)]
    chop = [100.0 + (1 if i % 2 else -1) for i in range(60)]
    assert trend_strength(line) == pytest.approx(59**0.5)
    assert trend_strength(chop) == pytest.approx(2 / 118 * 59**0.5)
    assert trend_strength([100.0, 100.0, 100.0]) is None

    assert label(7.7, 120.0, RULE) == "trending up"
    assert label(7.7, -120.0, RULE) == "trending down"
    assert label(0.4, 3.0, RULE) == "ranging"
    assert label(1.5, 30.0, RULE) == "mixed"
    assert label(None, None, RULE) == "mixed"
    with pytest.raises(RegimeError):
        RegimeRule(trend_above=Decimal("1"), range_below=Decimal("1"))


@pytest.mark.parametrize(
    ("setup", "target", "regime", "refused"),
    [
        ("trend_continuation", "long", "ranging", True),
        ("pullback_in_trend", "short", "ranging", True),
        ("range_reversion", "long", "ranging", False),
        ("range_reversion", "short", "trending up", True),
        ("trend_continuation", "long", "trending up", False),
        ("trend_continuation", "short", "trending up", True),
        ("pullback_in_trend", "short", "trending down", False),
        ("momentum_exhaustion", "short", "trending up", False),
        ("momentum_exhaustion", "long", "trending up", True),
        ("range_breakout", "long", "ranging", False),
        ("volatility_expansion", "short", "trending up", False),
        ("trend_continuation", "long", "mixed", False),
    ],
)
def test_a_setup_has_to_belong_in_the_regime_measured(
    setup: str, target: str, regime: str, refused: bool
) -> None:
    assert bool(misfit(setup, target, regime)) is refused


def frame(closes: list[str], seconds: int) -> Fetched:
    rows = candles(closes)
    spaced = [
        type(c)(
            start_ns=rows[0].start_ns + i * seconds * 1_000_000_000,
            open=c.open,
            high=c.high,
            low=c.low,
            close=c.close,
            volume=c.volume,
            trades=c.trades,
        )
        for i, c in enumerate(rows)
    ]
    return Fetched(
        kind="ohlcv",
        instrument="BTCUSDT",
        value=spaced,
        source="stub:ohlcv",
        tier="free",
        fetched_at=NOW,
        as_of_ns=spaced[-1].start_ns + seconds * 1_000_000_000,
        detail={"interval_s": seconds},
    )


def test_the_brief_carries_the_regime_on_every_timeframe_it_was_given() -> None:
    snap = snapshot_with(closes=["100", "101", "99", "100", "102"])
    rising = [str(100 + i) for i in range(20)]
    brief = technical_brief(
        snap,
        mechanism="reversion",
        interval_s=60,
        lookback_bars=20,
        now=NOW,
        context={900: frame(rising, 900), 3600: frame(list(reversed(rising)), 3600)},
        readings={"macro_analyst": {"declared": "risk-off", "score": "-0.4"}},
    )
    record = brief.record()
    assert record["regime"] == "ranging"
    assert record["timeframes"]["15m"]["regime"] == "trending up"
    assert record["timeframes"]["1h"]["regime"] == "trending down"
    assert record["timeframes"]["15m"]["bars"] == 20
    assert record["readings"]["macro_analyst"]["declared"] == "risk-off"
    # The fixture's book holds 55 on each side.
    assert record["book_imbalance"] == "0.000"
    assert interval_name(900) == "15m" and interval_name(86_400) == "1d"
    assert interval_name(90) == "90s"


def test_book_imbalance_leans_to_the_side_with_more_resting() -> None:
    book = OrderBook(
        bids=(BookLevel(Decimal("99"), Decimal("30")), BookLevel(Decimal("98"), Decimal("10"))),
        asks=(BookLevel(Decimal("101"), Decimal("10")),),
    )
    assert book_imbalance(book) == "0.600"
    assert book_imbalance(book, levels=1) == "0.500"


def test_open_interest_change_is_measured_on_the_same_field_and_only_when_it_moved() -> None:
    now_ns = int(NOW * 1_000_000_000)
    current = Fetched(
        kind="open_interest",
        instrument="BTCUSDT",
        value=Decimal("110"),
        source="ccxt:bybit",
        tier="free",
        fetched_at=NOW,
        as_of_ns=now_ns,
        detail={"amount": "110", "value": None},
    )
    before = {"as_of": now_ns - 300 * 1_000_000_000, "amount": "100", "value": None}
    assert open_interest_change(current, before) == {
        "change_pct": "10.00",
        "since_s": 300,
        "measured_in": "contracts",
    }
    assert open_interest_change(current, {**before, "as_of": now_ns}) is None
    assert open_interest_change(current, {"as_of": before["as_of"], "value": "9"}) is None
    assert open_interest_change(current, {**before, "amount": "not a number"}) is None
    assert open_interest_change(current, {**before, "as_of": "yesterday"}) is None


def test_a_trend_setup_in_a_ranging_market_is_refused_by_name(
    tmp_path: Path, envelope: Envelope
) -> None:
    # The cycle fixture's closes go back and forth around 100: ranging on any reading.
    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer(setup="trend_continuation")])
    refused = run_cycle(cfg)
    assert not refused.acted
    assert any(
        r.startswith("regime: trend_continuation in a market measured as ranging")
        for r in refused.refusals
    )

    cfg, _, _ = cycle_config(
        tmp_path / "allowed",
        envelope,
        [analyst_answer(setup="trend_continuation")],
        regime=RegimeRule(setups_must_fit=False),
    )
    allowed = run_cycle(cfg)
    assert allowed.acted
    assert any("allowed, setups_must_fit is false" in r for r in allowed.refusals)


def test_the_cycle_reads_context_timeframes_and_the_last_open_interest(
    tmp_path: Path, envelope: Envelope
) -> None:
    root = tmp_path / "cycles"
    earlier = root / "2026-09-27T09-55-00Z-BTCUSDT"
    earlier.mkdir(parents=True)
    then_ns = int(NOW * 1_000_000_000) - 5 * MINUTE_NS
    (earlier / "snapshot.json").write_text(
        json.dumps({"values": {"open_interest": {"as_of": then_ns, "amount": "100"}}}), "utf-8"
    )
    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer()], context_intervals=(900,))
    result = run_cycle(cfg)
    stub = cfg.chains["ohlcv"].sources[0]
    assert any(seen.get("interval_s") == 900 for seen in stub.seen)  # type: ignore[attr-defined]
    assert "15m" in result.briefs["context"]
    brief = json.loads((Path(str(result.path)) / "briefs" / "technical.json").read_text("utf-8"))
    assert "timeframes" in brief and brief["regime"] == "ranging"


def test_the_project_declares_context_timeframes_and_the_regime_lines() -> None:
    raw = agent_raw(regime={"trend_above": Decimal("2.5"), "setups_must_fit": False})
    raw["run"]["context_intervals"] = ["1h", "15m"]
    cfg = parse_strategy_config(raw)
    assert cfg.run.context_intervals == (900, 3600)
    assert cfg.regime.trend_above == Decimal("2.5") and not cfg.regime.setups_must_fit
    assert parse_strategy_config(agent_raw()).run.context_intervals == ()

    for bad, why in (
        (["30s"], "whole multiple above"),
        (["90s"], "whole multiple above"),
        (["15m", "15m"], "twice"),
        (["5m", "15m", "1h", "4h"], "at most"),
        ("1h", "is a list"),
    ):
        raw = agent_raw()
        raw["run"]["context_intervals"] = bad
        with pytest.raises(ConfigError, match=why):
            parse_strategy_config(raw)
    with pytest.raises(ConfigError, match="unknown"):
        parse_strategy_config(agent_raw(regime={"trend": 1}))
    with pytest.raises(ConfigError, match="range_below < trend_above"):
        parse_strategy_config(agent_raw(regime={"range_below": Decimal("3")}))


def test_the_live_table_lists_capital_and_regime_where_they_happen(
    tmp_path: Path, envelope: Envelope
) -> None:
    from trading_research_agents.cli.main import _cycle_steps

    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer()], account=lambda: None)
    steps = _cycle_steps(cfg)
    # Seen live: capital was announced but missing from the table, so it was drawn last.
    assert steps.index("data") < steps.index("capital") < steps.index("regime")
    assert steps.index("regime") < steps.index("technical")
    plain, _, _ = cycle_config(tmp_path / "plain", envelope, [analyst_answer()])
    assert "capital" not in _cycle_steps(plain)
