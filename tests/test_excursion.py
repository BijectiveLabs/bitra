"""MFE and MAE: how far a position went each way before it closed, and what that says."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.data.schema import Direction
from trading_research_agents.execution.cost import CostError
from trading_research_agents.execution.excursion import (
    Excursion,
    ExcursionTracker,
    excursion_of,
    summarise,
)


def bars(*pairs: tuple[str, str]) -> list[tuple[Decimal | None, Decimal | None]]:
    return [(Decimal(high), Decimal(low)) for high, low in pairs]


def test_a_long_is_helped_by_highs_and_hurt_by_lows() -> None:
    # Entry at 100. The position stands 200 bps in front on the second bar and 100 bps under
    # water on the third, and both are kept with the bar they happened on.
    walk = excursion_of(
        Decimal("100"), Direction.LONG, bars(("100.5", "99.8"), ("102", "101"), ("100.2", "99"))
    )
    assert walk.mfe_bps == Decimal(200) and walk.bars_to_mfe == 2
    assert walk.mae_bps == Decimal(-100) and walk.bars_to_mae == 3
    assert walk.bars == 3
    record = walk.record()
    assert record["mfe_bps"] == "200.00" and record["mae_bps"] == "-100.00"
    assert record["bars_to_mfe"] == 2 and record["bars"] == 3


def test_a_short_reads_the_same_figures_the_other_way_round() -> None:
    walk = excursion_of(Decimal("100"), Direction.SHORT, bars(("101", "99"), ("100", "97")))
    # Price falling is favourable for a short: 300 bps in front at the second bar's low.
    assert walk.mfe_bps == Decimal(300) and walk.bars_to_mfe == 2
    # Price rising is adverse: 100 bps under water at the first bar's high.
    assert walk.mae_bps == Decimal(-100) and walk.bars_to_mae == 1


def test_a_position_that_never_moved_has_no_excursion() -> None:
    flat = excursion_of(Decimal("100"), Direction.LONG, bars(("100", "100"), ("100", "100")))
    assert flat.mfe_bps == Decimal(0) and flat.mae_bps == Decimal(0)
    assert flat.bars_to_mfe == 0 and flat.bars_to_mae == 0 and flat.bars == 2


def test_an_empty_bar_moves_nothing_but_still_counts_as_time() -> None:
    tracker = ExcursionTracker(entry_px=Decimal("100"), direction=Direction.LONG)
    tracker.see(None, None)
    tracker.see(Decimal("101"), Decimal("100"))
    walk = tracker.close()
    assert walk.bars == 2 and walk.mfe_bps == Decimal(100) and walk.bars_to_mfe == 2


def test_the_tracker_refuses_prices_that_are_not_prices() -> None:
    with pytest.raises(CostError, match="entry_px must be positive"):
        ExcursionTracker(entry_px=Decimal(0), direction=Direction.LONG)
    with pytest.raises(CostError, match="finite Decimal"):
        ExcursionTracker(entry_px=Decimal("nan"), direction=Direction.LONG)
    tracker = ExcursionTracker(entry_px=Decimal("100"), direction=Direction.LONG)
    with pytest.raises(CostError, match="positive finite Decimals"):
        tracker.see(Decimal("-1"), Decimal("99"))


def test_the_summary_says_what_was_given_back() -> None:
    rows: list[dict[str, Any]] = [
        {"mfe_bps": "30", "mae_bps": "-5", "net_bps": "10"},
        {"mfe_bps": "10", "mae_bps": "-14", "net_bps": "2"},
        {"mfe_bps": "0", "mae_bps": "-20", "net_bps": "-20"},
    ]
    out = summarise(rows)
    assert out["round_trips"] == 3 and out["measured"] == 3
    assert out["mfe_median_bps"] == "10.00" and out["mfe_max_bps"] == "30.00"
    assert out["mae_median_bps"] == "-14.00" and out["mae_worst_bps"] == "-20.00"
    # Two of the three stood in front at some point; the median of what they did not keep.
    assert out["given_back_median_bps"] == "14.00"
    assert summarise([]) == {"round_trips": 0, "measured": 0}
    assert summarise([{"net_bps": "5"}]) == {"round_trips": 1, "measured": 0}


def test_a_replay_records_the_excursion_of_every_round_trip(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    from trading_research_agents.data import Side, Trade
    from trading_research_agents.data.parquet import write_trades
    from trading_research_agents.envelope import load_envelope
    from trading_research_agents.execution.cost import FundingSchedule
    from trading_research_agents.strategy.core import Signal, Target
    from trading_research_agents.trading import (
        Controls,
        KillSwitch,
        RecordedFeed,
        SimulatedVenue,
        TradingConfig,
        run,
    )
    from trading_research_agents.trading.controls import Limits

    minute = 60 * 1_000_000_000
    # Price walks up then falls back, so the long stands in front and gives it back.
    prices = [
        "100",
        "100",
        "100",
        "101",
        "103",
        "104",
        "102",
        "101",
        "101",
        "101",
        "101",
        "101",
    ]
    rows = [
        Trade(i * minute + 1, "bybit", "AAAUSDT", Decimal(p), Decimal("0.01"), Side.BUY, str(i))
        for i, p in enumerate(prices)
    ]
    dataset = tmp_path / "AAAUSDT.parquet"
    write_trades(rows, dataset, price_scale=2, qty_scale=3)

    class LongThenFlat:
        name = "long-then-flat"

        def __init__(self) -> None:
            self.bars = 0

        def on_bar(self, bar: Any) -> Signal | None:
            self.bars += 1
            if self.bars == 2:
                return Signal(Target.LONG, "the burst is in")
            if self.bars == 8:
                return Signal(Target.FLAT, "the window mean is back")
            return None

    envelope = load_envelope(Path(__file__).resolve().parent.parent / "examples" / "envelope.toml")
    channel = envelope.cheapest(None)
    from trading_research_agents.strategy.exits import ExitRule

    report = run(
        TradingConfig(
            feed=RecordedFeed.from_parquet(dataset, instrument="AAAUSDT"),
            venue=SimulatedVenue(
                channel=channel,
                funding=FundingSchedule(interval_s=28_800, rates={}),
                latency_ns=0,
                slippage_bps=Decimal(0),
            ),
            strategy=LongThenFlat(),
            exit_rule=ExitRule(
                stop_frac=Decimal("0.5"),
                take_profit_frac=None,
                timeout_bars=50,
                mechanism_bound=("the reversion is done when the close returns to its window mean"),
                slip_bps=Decimal(0),
            ),
            controls=Controls(
                Limits(
                    max_risk_per_trade_bps=Decimal(500),
                    max_position=Decimal(1),
                    max_orders_per_minute=10,
                    daily_loss_limit_bps=Decimal(1000),
                ),
                KillSwitch(None),
            ),
            out_dir=tmp_path / "out",
            bar_interval_ns=minute,
            size=Decimal("0.01"),
            spread_cost_bps=Decimal(0),
            equity=Decimal(10_000),
            label="replay",
        )
    )
    assert report.round_trips == 1
    trade = json.loads((tmp_path / "out" / "trades.jsonl").read_text("utf-8").splitlines()[0])
    assert Decimal(trade["mfe_bps"]) > Decimal(trade["net_bps"])
    assert Decimal(trade["mae_bps"]) <= Decimal(0)
    assert trade["bars_to_mfe"] >= 1 and trade["bars"] >= trade["bars_to_mfe"]

    # And the risk manager's brief carries the summary, so an agent sees the path, not just the net.
    from trading_research_agents.agents.risk_manager.tools import excursions

    summary = excursions(tmp_path / "out")
    assert summary["round_trips"] == 1 and summary["measured"] == 1
    assert Decimal(summary["mfe_max_bps"]) > 0


def test_an_excursion_record_is_plain_json() -> None:
    walk = Excursion(
        mfe_bps=Decimal("12.345"),
        mae_bps=Decimal("-6.789"),
        bars_to_mfe=3,
        bars_to_mae=1,
        bars=5,
    )
    # Half-even rounding, the same as every other figure this package reports.
    assert json.loads(json.dumps(walk.record())) == {
        "mfe_bps": "12.34",
        "mae_bps": "-6.79",
        "bars_to_mfe": 3,
        "bars_to_mae": 1,
        "bars": 5,
    }
