from __future__ import annotations

import math
import statistics
from datetime import date
from decimal import Decimal

import pytest

from trading_research_agents.data.schema import Direction
from trading_research_agents.evaluation.settle import Settlement, Unsettled
from trading_research_agents.evaluation.splits import walk_forward
from trading_research_agents.evaluation.stats import (
    Window,
    drawdown_r,
    independent_bets,
    rank_ic,
    row_report,
    versus,
)
from trading_research_agents.execution.cost import CostBreakdown
from trading_research_agents.market_data.days import DAY_NS, NS_PER_MINUTE, midnight_ns

HOUR = 60 * NS_PER_MINUTE
T0 = midnight_ns(date(2025, 7, 1))
ZERO = CostBreakdown(Decimal(0), Decimal(0), Decimal(0), Decimal(0))


def idea(
    day: int, net: str, instrument: str = "BTCUSDT", hour: int = 0, stop: str = "10"
) -> Settlement:
    entry = T0 + day * DAY_NS + hour * HOUR
    return Settlement(
        row="r",
        instrument=instrument,
        decided_ns=entry,
        entry_ns=entry,
        exit_ns=entry + HOUR,
        side=Direction.LONG,
        playbook="p",
        entry_price=Decimal(100),
        exit_price=Decimal(100),
        ended="time",
        stop_bps=Decimal(stop),
        take_bps=None,
        gross_bps=Decimal(net),
        cost=ZERO,
        funding_basis="none declared",
    )


def window(days: int) -> Window:
    return Window(T0, T0 + days * DAY_NS, 1)


def test_overlapping_ideas_on_one_market_are_one_bet() -> None:
    first = idea(0, "1")
    inside = idea(0, "1")
    other = idea(0, "1", instrument="ETHUSDT")
    later = idea(0, "1", hour=2)
    assert independent_bets([first, inside, other, later]) == 3


def test_the_figures_of_a_row() -> None:
    found = [idea(0, "10"), idea(1, "-20"), idea(2, "5")]
    missed = [Unsettled("r", "BTCUSDT", T0, "a minute inside the hold was not seen")]
    report = row_report("r", found, missed, window(3))
    assert report["ideas"] == 3 and report["independent_bets"] == 3
    assert report["mean_net_bps"] == "-1.67" and report["median_net_bps"] == "5.00"
    assert report["hit_rate"] == "0.667" and report["sum_net_bps"] == "-5.00"
    assert report["mean_r"] == "-0.167"
    assert report["unsettled"] == {"a minute inside the hold was not seen": 1}
    daily = [10.0, -20.0, 5.0]
    sharpe = statistics.mean(daily) / statistics.stdev(daily) * math.sqrt(365)
    assert float(report["sharpe_daily"]) == pytest.approx(sharpe, abs=1e-3)
    assert float(report["time_in_market"]) == pytest.approx(3 / 72, abs=1e-4)
    assert report["verdict"] == "sample too small"


def test_drawdown_is_measured_in_r() -> None:
    assert drawdown_r([idea(0, "10"), idea(1, "-20"), idea(2, "5")]) == pytest.approx(2.0)


def test_a_row_that_beats_the_null_by_whole_days() -> None:
    mine = [idea(d, "10") for d in range(40)]
    null = [idea(d, "-10") for d in range(40)]
    against = versus(mine, null)
    assert against is not None and Decimal(against["low_bps"]) > 0
    report = row_report("r", mine, [], window(40), null=null)
    assert report["verdict"] == "beats random"
    assert row_report("random", null, [], window(40), null_row=True)["verdict"] == "the null"
    flagged = row_report("r", mine, [], window(40), null=null, contaminated=True)
    assert flagged["verdict"].startswith("contaminated")


def test_rank_ic_by_date_with_its_t_statistic() -> None:
    pairs = [
        (f"2025-07-{d:02d}", float(k), float(k) * (1 + d % 3))
        for d in range(1, 13)
        for k in range(4)
    ]
    found = rank_ic(pairs)
    assert found["dates"] == "12" and found["mean_ic"] == "1.0000" and found["t_stat"] is None
    assert rank_ic(pairs[:8])["verdict"] == "too few dates"


def test_walk_forward_leaves_an_embargo_between_fit_and_test() -> None:
    splits = walk_forward(
        date(2025, 7, 1),
        date(2025, 7, 20),
        fit_days=10,
        test_days=5,
        embargo_bars=24,
        interval_ns=HOUR,
    )
    assert len(splits) == 1
    (only,) = splits
    assert only.fit_start_ns == midnight_ns(date(2025, 7, 1))
    assert only.test_start_ns == midnight_ns(date(2025, 7, 12))
    assert only.test_end_ns == midnight_ns(date(2025, 7, 17))
    with pytest.raises(ValueError, match="positive"):
        walk_forward(
            date(2025, 7, 1),
            date(2025, 7, 2),
            fit_days=0,
            test_days=1,
            embargo_bars=0,
            interval_ns=HOUR,
        )
