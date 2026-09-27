from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.data import Direction, Side, Trade
from trading_research_agents.envelope import Envelope
from trading_research_agents.execution import FundingSchedule
from trading_research_agents.strategy import Bar, ExitRule, Signal, Target
from trading_research_agents.trading import (
    BarBuilder,
    ControlError,
    Controls,
    KillSwitch,
    Ladder,
    LadderError,
    Limits,
    RecordedFeed,
    SimulatedVenue,
    Tier,
    TradingConfig,
    run,
)
from trading_research_agents.trading.feed import FeedError

S = 1_000_000_000
MINUTE = 60 * S
BASE = 1_735_689_600 * S  # 2025-01-01T00:00:00Z


def trade(i: int, price: str, *, seconds: float = 0.0) -> Trade:
    return Trade(
        ts_ns=BASE + i * MINUTE + int(seconds * S),
        venue="bybit",
        instrument="BTCUSDT",
        price=Decimal(price),
        qty=Decimal("0.01"),
        side=Side.BUY,
        trade_id=f"t-{i}-{seconds}",
    )


def limits(**kw: object) -> Limits:
    base: dict[str, object] = {
        "max_risk_per_trade_bps": Decimal("50"),
        "max_position": Decimal("1"),
        "max_orders_per_minute": 10,
        "daily_loss_limit_bps": Decimal("300"),
    }
    base.update(kw)
    return Limits(**base)  # type: ignore[arg-type]


def venue(envelope: Envelope) -> SimulatedVenue:
    channel = next(c for c in envelope.channels if c.key == "bybit/taker-taker")
    return SimulatedVenue(
        channel=channel,
        funding=FundingSchedule(interval_s=8 * 3600, rates={}),
        latency_ns=int(0.5 * S),
        slippage_bps=Decimal("1"),
    )


class OneShot:
    """Goes long on the first bar it sees, then asks for nothing more."""

    name = "one-shot"

    def __init__(self) -> None:
        self.asked = False

    def on_bar(self, bar: Bar) -> Signal | None:
        if self.asked or bar.empty:
            return None
        self.asked = True
        return Signal(Target.LONG, "first bar")


# Bars


def test_bar_builder_marks_empty_intervals_instead_of_filling_them() -> None:
    builder = BarBuilder(MINUTE)
    assert builder.push(trade(0, "100")) == []
    assert builder.push(trade(0, "101", seconds=30)) == []
    closed = builder.push(trade(3, "99"))
    assert [b.trades for b in closed] == [2, 0, 0]
    assert closed[0].open == 100 and closed[0].high == 101 and closed[0].close == 101
    assert closed[1].empty and closed[1].start_ns == BASE + MINUTE
    last = builder.flush()
    assert last is not None and last.trades == 1 and last.close == 99
    disordered = BarBuilder(MINUTE)
    disordered.push(trade(5, "1"))
    with pytest.raises(FeedError, match="out of order"):
        disordered.push(trade(4, "1"))


def test_recorded_feed_refuses_disorder_and_other_instruments() -> None:
    with pytest.raises(FeedError, match="time order"):
        list(RecordedFeed("BTCUSDT", [trade(1, "1"), trade(0, "1")]))
    other = Trade(BASE, "bybit", "ETHUSDT", Decimal("1"), Decimal("1"), Side.BUY, "x")
    with pytest.raises(FeedError, match="ETHUSDT"):
        list(RecordedFeed("BTCUSDT", [other]))


# Controls


def test_controls_fail_closed() -> None:
    controls = Controls(limits(), KillSwitch())
    ok = controls.admit(
        ts_ns=BASE, target=Target.LONG, size=Decimal("1"), risk_bps=Decimal("20"), equity_known=True
    )
    assert ok.admitted
    unknown = controls.admit(
        ts_ns=BASE,
        target=Target.LONG,
        size=Decimal("1"),
        risk_bps=Decimal("20"),
        equity_known=False,
    )
    assert not unknown.admitted and "equity unknown" in unknown.reason
    risky = controls.admit(
        ts_ns=BASE, target=Target.LONG, size=Decimal("1"), risk_bps=Decimal("80"), equity_known=True
    )
    assert not risky.admitted and "exceeds max" in risky.reason
    big = controls.admit(
        ts_ns=BASE, target=Target.LONG, size=Decimal("2"), risk_bps=Decimal("1"), equity_known=True
    )
    assert not big.admitted and "max position" in big.reason
    back = controls.admit(
        ts_ns=BASE - 1,
        target=Target.LONG,
        size=Decimal("1"),
        risk_bps=Decimal("1"),
        equity_known=True,
    )
    assert not back.admitted and "clock went backwards" in back.reason
    flat = controls.admit(
        ts_ns=BASE, target=Target.FLAT, size=Decimal("1"), risk_bps=None, equity_known=False
    )
    assert flat.admitted


def test_order_rate_daily_loss_and_kill_switch(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path / "KILL")
    controls = Controls(limits(max_orders_per_minute=2), switch)
    args = {
        "target": Target.LONG,
        "size": Decimal("1"),
        "risk_bps": Decimal("1"),
        "equity_known": True,
    }
    assert controls.admit(ts_ns=BASE, **args).admitted  # type: ignore[arg-type]
    assert controls.admit(ts_ns=BASE + S, **args).admitted  # type: ignore[arg-type]
    third = controls.admit(ts_ns=BASE + 2 * S, **args)  # type: ignore[arg-type]
    assert not third.admitted and "order rate" in third.reason
    assert controls.admit(ts_ns=BASE + MINUTE + 1, **args).admitted  # type: ignore[arg-type]

    controls.record_pnl(BASE + 2 * MINUTE, Decimal("-150"))
    controls.record_pnl(BASE + 3 * MINUTE, Decimal("-160"))
    tripped = controls.admit(ts_ns=BASE + 4 * MINUTE, **args)  # type: ignore[arg-type]
    assert not tripped.admitted and "daily loss limit" in tripped.reason
    assert controls.tripped is not None

    (tmp_path / "KILL").write_text("stop", encoding="utf-8")
    fresh = Controls(limits(), switch)
    killed = fresh.admit(ts_ns=BASE, **args)  # type: ignore[arg-type]
    assert not killed.admitted and "kill switch" in killed.reason
    with pytest.raises(ControlError, match="positive"):
        limits(max_risk_per_trade_bps=Decimal("0"))


# The ladder


def test_ladder_goes_up_with_evidence_and_down_on_a_reason(tmp_path: Path) -> None:
    ladder = Ladder(tmp_path / "ladder.json")
    assert ladder.tier == Tier.RESEARCH
    with pytest.raises(LadderError, match="names its evidence"):
        ladder.promote(evidence=" ", signed_by="operator")
    with pytest.raises(LadderError, match="signed"):
        ladder.promote(evidence="validation.json abc", signed_by="")
    first = ladder.promote(evidence="layer 5 OPEN on T0 replay, seal abc", signed_by="operator")
    second = ladder.promote(evidence="two weeks paper, layer 5 OPEN", signed_by="operator")
    assert (first.to_tier, second.to_tier) == (Tier.PAPER, Tier.SHADOW)
    assert Ladder(tmp_path / "ladder.json").tier == Tier.SHADOW
    with pytest.raises(LadderError, match="needs a live venue"):
        ladder.promote(evidence="two weeks shadow", signed_by="operator")
    demotion = ladder.demote(reason="daily loss limit tripped", to=Tier.RESEARCH)
    assert (
        demotion.to_tier == Tier.RESEARCH and Ladder(tmp_path / "ladder.json").tier == Tier.RESEARCH
    )
    with pytest.raises(LadderError, match="nothing below"):
        ladder.demote(reason="x")
    assert [h["kind"] for h in ladder.history] == ["promotion", "promotion", "demotion"]
    with pytest.raises(LadderError, match="UTC"):
        ladder.promote(evidence="e", signed_by="o", at=datetime(2026, 9, 13))


# The runtime, end to end


def test_replay_fills_after_latency_stops_on_the_bar_and_writes_trades(
    envelope: Envelope, tmp_path: Path
) -> None:
    # Minute 0: two prints (the first bar). Minute 1: the signal bar closes; the entry fills on
    # the first print >= 0.5 s after the bar end. Minute 2: the price gaps below the stop.
    prints = [
        trade(0, "100"),
        trade(0, "100", seconds=30),
        trade(1, "100.2"),
        trade(1, "100.3", seconds=1),  # entry fills here: 100.3 plus 1 bp slippage
        trade(2, "98.0"),  # bar 2 low 98.0 touches the 1 pct stop at ~99.297
        trade(2, "98.5", seconds=30),
        trade(3, "98.5"),
    ]
    config = TradingConfig(
        feed=RecordedFeed("BTCUSDT", prints),
        venue=venue(envelope),
        strategy=OneShot(),
        exit_rule=ExitRule(Decimal("0.01"), 10, "the mechanism resolves within ten minutes here"),
        controls=Controls(limits(max_risk_per_trade_bps=Decimal("200")), KillSwitch()),
        out_dir=tmp_path,
        bar_interval_ns=MINUTE,
        size=Decimal("0.5"),
        spread_cost_bps=Decimal("1.5"),
        equity=Decimal("1000"),
    )
    report = run(config)
    assert report.intents == 1 and report.refused == 0 and report.fills == 1
    assert report.round_trips == 1 and report.open_position is None
    rows = [json.loads(line) for line in (tmp_path / "trades.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["direction"] == "long" and row["cause"] == "sl"
    entry = Decimal("100.3") * (1 + Decimal("0.0001"))
    assert Decimal(row["entry_px"]) == entry
    # A gap through the stop fills at the worse open (98.0), less 5 bps adverse slippage.
    assert Decimal(row["exit_px"]) == Decimal("98.0") * (1 - Decimal("0.0005"))
    assert Decimal(row["cost_bps"]) == Decimal("11.0") + Decimal("1.5") + Decimal("1")
    assert Decimal(row["net_bps"]) < 0
    intents = [json.loads(line) for line in (tmp_path / "intents.jsonl").read_text().splitlines()]
    assert intents[0]["admitted"] is True and intents[0]["risk_bps"] is not None
    summary = json.loads((tmp_path / "run.json").read_text())
    assert summary["round_trips"] == 1 and summary["venue"] == "simulated"


def test_unknown_equity_refuses_the_entry_and_records_why(
    envelope: Envelope, tmp_path: Path
) -> None:
    config = TradingConfig(
        feed=RecordedFeed("BTCUSDT", [trade(0, "100"), trade(1, "100"), trade(2, "100")]),
        venue=venue(envelope),
        strategy=OneShot(),
        exit_rule=ExitRule(Decimal("0.01"), 10, "the mechanism resolves within ten minutes here"),
        controls=Controls(limits(), KillSwitch()),
        out_dir=tmp_path,
        bar_interval_ns=MINUTE,
        size=Decimal("0.5"),
        spread_cost_bps=Decimal("1.5"),
        equity=None,
    )
    report = run(config)
    assert report.intents == 1 and report.refused == 1 and report.fills == 0
    assert "equity unknown" in report.refusals[0]


def test_timeout_exits_through_the_venue_and_kill_switch_flattens(
    envelope: Envelope, tmp_path: Path
) -> None:
    prints = [trade(i, "100") for i in range(8)]
    switch = KillSwitch()
    config = TradingConfig(
        feed=RecordedFeed("BTCUSDT", prints),
        venue=venue(envelope),
        strategy=OneShot(),
        exit_rule=ExitRule(Decimal("0.05"), 2, "two bars is the whole life of this mechanism"),
        controls=Controls(limits(max_risk_per_trade_bps=Decimal("500")), switch),
        out_dir=tmp_path / "timeout",
        bar_interval_ns=MINUTE,
        size=Decimal("0.1"),
        spread_cost_bps=Decimal("1"),
        equity=Decimal("1000"),
    )
    report = run(config)
    assert report.round_trips == 1
    row = json.loads((tmp_path / "timeout" / "trades.jsonl").read_text().splitlines()[0])
    assert row["cause"] == "intent" and row["bars_held"] == 2

    class Armed(OneShot):
        name = "armed"

        def on_bar(self, bar: Bar) -> Signal | None:
            signal = super().on_bar(bar)
            if signal is None and bar.start_ns >= BASE + 3 * MINUTE:
                switch.arm()
            return signal

    config2 = TradingConfig(
        feed=RecordedFeed("BTCUSDT", prints),
        venue=venue(envelope),
        strategy=Armed(),
        exit_rule=ExitRule(Decimal("0.05"), 50, "the mechanism outlives this replay on purpose"),
        controls=Controls(limits(max_risk_per_trade_bps=Decimal("500")), switch),
        out_dir=tmp_path / "kill",
        bar_interval_ns=MINUTE,
        size=Decimal("0.1"),
        spread_cost_bps=Decimal("1"),
        equity=Decimal("1000"),
    )
    report2 = run(config2)
    assert report2.stopped_by == "kill switch" and report2.round_trips == 1
    assert report2.open_position is None


def test_direction_sign_and_config_guards(envelope: Envelope, tmp_path: Path) -> None:
    assert Direction.SHORT.sign == -1
    with pytest.raises(ValueError, match="size"):
        TradingConfig(
            feed=RecordedFeed("BTCUSDT", []),
            venue=venue(envelope),
            strategy=OneShot(),
            exit_rule=ExitRule(Decimal("0.01"), 1, "one two three four five six"),
            controls=Controls(limits(), KillSwitch()),
            out_dir=tmp_path,
            bar_interval_ns=MINUTE,
            size=Decimal("0"),
            spread_cost_bps=Decimal("1"),
        )
    assert (
        run(
            TradingConfig(
                feed=RecordedFeed("BTCUSDT", []),
                venue=venue(envelope),
                strategy=OneShot(),
                exit_rule=ExitRule(Decimal("0.01"), 1, "one two three four five six"),
                controls=Controls(limits(), KillSwitch()),
                out_dir=tmp_path / "empty",
                bar_interval_ns=MINUTE,
                size=Decimal("1"),
                spread_cost_bps=Decimal("1"),
            )
        ).prints
        == 0
    )
    now = datetime.now(UTC)
    assert now.tzinfo is UTC
