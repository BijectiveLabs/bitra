"""What the whole book may carry at once, as opposed to what any one trade may risk.

A per-trade limit of fifty basis points is a different promise when one position is open and when
six are. These tests are about the sum, about the count, and about two names that are one trade.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from test_cycle import analyst_answer, chains, cycle_config
from trading_research_agents.agents.cycle import run_cycle
from trading_research_agents.agents.screen import correlation, returns_of, sweep
from trading_research_agents.envelope import Envelope
from trading_research_agents.execution.desk import (
    Desk,
    DeskError,
    Open,
    from_records,
    within_desk,
)
from trading_research_agents.execution.trigger import Trigger
from trading_research_agents.execution.universe import Universe
from trading_research_agents.trading.config import ConfigError

NOW = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)


def held(*rows: tuple[str, str | None]) -> tuple[Open, ...]:
    return tuple(
        Open(instrument=name, side="buy", risk_bps=None if bps is None else Decimal(bps))
        for name, bps in rows
    )


def test_the_sum_of_what_is_at_risk_is_what_bounds_one_more_trade() -> None:
    desk = Desk(max_open_risk_bps=Decimal("100"))
    fits = within_desk(
        desk,
        instrument="SOLUSDT",
        adding_bps=Decimal("30"),
        held=held(("BTCUSDT", "40"), ("ETHUSDT", "25")),
        source="the venue",
    )
    assert fits.admitted and fits.open_risk_bps == Decimal("95") and fits.source == "the venue"

    over = within_desk(
        desk,
        instrument="SOLUSDT",
        adding_bps=Decimal("40"),
        held=held(("BTCUSDT", "40"), ("ETHUSDT", "25")),
    )
    assert not over.admitted
    assert "65 bps of equity is already at risk across 2 position(s)" in over.reasons[0]
    assert "105 bps is above the declared 100 bps" in over.reasons[0]


def test_a_position_on_the_same_instrument_is_a_replacement_not_an_addition() -> None:
    verdict = within_desk(
        Desk(max_open_risk_bps=Decimal("50"), max_open_positions=1),
        instrument="BTCUSDT",
        adding_bps=Decimal("40"),
        held=held(("BTCUSDT", "45")),
    )
    assert verdict.admitted and verdict.held == ()


def test_a_count_of_positions_is_its_own_bound() -> None:
    verdict = within_desk(
        Desk(max_open_positions=2),
        instrument="SOLUSDT",
        adding_bps=Decimal("1"),
        held=held(("BTCUSDT", "10"), ("ETHUSDT", "10")),
    )
    assert not verdict.admitted
    assert "2 position(s) already open (BTCUSDT, ETHUSDT)" in verdict.reasons[0]


def test_a_position_whose_risk_nobody_can_state_stops_the_sum_being_stated() -> None:
    verdict = within_desk(
        Desk(max_open_risk_bps=Decimal("100")),
        instrument="SOLUSDT",
        adding_bps=Decimal("10"),
        held=held(("BTCUSDT", "40"), ("ETHUSDT", None)),
    )
    assert not verdict.admitted and verdict.unknown == ("ETHUSDT",)
    assert "ETHUSDT has at risk is not on the record" in verdict.reasons[0]


def test_the_declarations_are_checked() -> None:
    assert Desk().idle and not Desk(max_open_positions=2).idle
    assert not Desk(correlation_above=Decimal("0.7")).bounds_a_trade
    assert Desk(max_open_risk_bps=Decimal("10")).bounds_a_trade
    with pytest.raises(DeskError, match="positive"):
        Desk(max_open_risk_bps=Decimal(0))
    with pytest.raises(DeskError, match="a desk that is shut"):
        Desk(max_open_positions=0)
    with pytest.raises(DeskError, match="above 0 and at most 1"):
        Desk(correlation_above=Decimal("1.5"))


def test_what_is_open_can_be_read_from_the_intents_on_disk(tmp_path: Path) -> None:
    root = tmp_path / "cycles"
    rows = [
        ("2026-09-27T09-00-00Z-BTCUSDT", "BTCUSDT", "long", "40", "0.01", "100000"),
        ("2026-09-27T09-30-00Z-BTCUSDT", "BTCUSDT", "short", "12", "0.02", "100000"),
        ("2026-09-26T09-00-00Z-ETHUSDT", "ETHUSDT", "long", "30", "1", "4000"),
    ]
    for name, instrument, target, risk, size, mid in rows:
        where = root / name
        where.mkdir(parents=True)
        (where / "intents.jsonl").write_text(
            json.dumps(
                {
                    "at": name,
                    "instrument": instrument,
                    "target": target,
                    "risk_bps": risk,
                    "size": size,
                    "mid": mid,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    book, source = from_records(root, at=NOW)
    # Yesterday's intent is not today's position, and the newest intent per instrument stands.
    assert [row.instrument for row in book] == ["BTCUSDT"]
    assert book[0].side == "sell" and book[0].risk_bps == Decimal("12")
    assert book[0].notional == Decimal("2000.00")
    assert "intents written today" in source
    empty, said = from_records(tmp_path / "nothing", at=NOW)
    assert empty == () and "intents written today" in said


def test_a_cycle_refuses_a_trade_the_book_cannot_carry(tmp_path: Path, envelope: Envelope) -> None:
    def book() -> tuple[tuple[Open, ...], str]:
        return held(("ETHUSDT", "48")), "a test"

    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {"technical_analyst": analyst_answer()},
        equity=Decimal("10000"),
        stop_frac=Decimal("0.01"),
        take_frac=Decimal("0.02"),
        size_step=Decimal("0.001"),
        max_risk_per_trade_bps=Decimal("10"),
        desk=Desk(max_open_risk_bps=Decimal("50")),
        held=book,
    )
    result = run_cycle(cfg)

    assert not result.acted and result.book is not None
    assert not result.book.admitted and result.book.source == "a test"
    assert any("48 bps of equity is already at risk" in r for r in result.refusals)


def test_a_book_nobody_can_read_refuses_the_trade_rather_than_the_cycle(
    tmp_path: Path, envelope: Envelope
) -> None:
    def broken() -> tuple[tuple[Open, ...], str]:
        raise TimeoutError("the venue did not answer")

    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {"technical_analyst": analyst_answer()},
        equity=Decimal("10000"),
        stop_frac=Decimal("0.01"),
        take_frac=Decimal("0.02"),
        size_step=Decimal("0.001"),
        max_risk_per_trade_bps=Decimal("100"),
        desk=Desk(max_open_risk_bps=Decimal("50")),
        held=broken,
    )
    result = run_cycle(cfg)

    assert not result.acted
    assert any("could not read what is open: TimeoutError" in r for r in result.refusals)
    assert any("is not on the record" in r for r in result.refusals)


def test_two_names_that_move_together_are_one_trade() -> None:
    up = [0.001, 0.002, -0.001, 0.003, -0.002] * 6
    same = [x * 1.4 for x in up]
    against = [-x for x in up]
    assert correlation(up, same) == Decimal("1.000")
    assert correlation(up, against) == Decimal("-1.000")
    # Too few points is no answer at all, not a weak one.
    assert correlation(up[:5], same[:5]) is None
    assert correlation(up, [0.0] * len(up)) is None


def test_the_screen_refuses_a_candidate_that_is_already_on_in_another_name(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cycles"
    root.mkdir(parents=True)

    def book() -> tuple[tuple[Open, ...], str]:
        return held(("BTCUSDT", "40")), "a test"

    # Both instruments answer from the same stubbed bars, so they move together exactly.
    # A window long enough for a correlation to mean anything; both answer from the same bars.
    window = [str(100 + (i % 7) - (i % 3)) for i in range(40)]
    screened = sweep(
        Universe(instruments=("BTCUSDT", "ETHUSDT")),
        chains(closes=window),
        trigger=Trigger(),
        root=root,
        now=NOW,
        desk=Desk(correlation_above=Decimal("0.7")),
        held=book,
    )
    eth = next(c for c in screened.candidates if c.instrument == "ETHUSDT")
    assert not eth.admitted and eth.correlated_with == "BTCUSDT"
    assert eth.correlation == Decimal("1.000")
    assert any("one trade in two names is not two trades" in r for r in screened.refusals)
    assert "ETHUSDT" not in screened.chosen


def test_the_desk_is_read_from_the_project() -> None:
    from test_config import agent_raw
    from trading_research_agents.trading.config import parse_strategy_config

    assert parse_strategy_config(agent_raw()).desk.idle
    cfg = parse_strategy_config(
        agent_raw(
            desk={
                "max_open_risk_bps": Decimal("150"),
                "max_open_positions": 3,
                "correlation_above": Decimal("0.7"),
            }
        )
    )
    assert cfg.desk.max_open_risk_bps == Decimal("150")
    assert cfg.desk.max_open_positions == 3 and cfg.desk.correlation_above == Decimal("0.7")
    with pytest.raises(ConfigError, match="a desk that is shut"):
        parse_strategy_config(agent_raw(desk={"max_open_positions": 0}))
    with pytest.raises(ConfigError, match="unknown"):
        parse_strategy_config(agent_raw(desk={"max_risk": 1}))


def test_the_record_carries_the_book_it_was_measured_against() -> None:
    verdict = within_desk(
        Desk(max_open_risk_bps=Decimal("100")),
        instrument="SOLUSDT",
        adding_bps=Decimal("10"),
        held=held(("BTCUSDT", "40")),
        source="the venue",
    )
    row: dict[str, Any] = verdict.record()
    assert row["admitted"] and row["source"] == "the venue"
    assert row["open_risk_bps"] == "50" and row["held"][0]["instrument"] == "BTCUSDT"


def test_returns_come_from_the_bars_the_pass_already_pulled() -> None:
    from test_cycle import snapshot_with

    moves = returns_of(snapshot_with(closes=["100", "101", "99"]))
    assert len(moves) == 2 and round(moves[0], 4) == 0.01
    assert returns_of(snapshot_with(closes=["100"])) == []


def test_a_venue_position_is_matched_to_the_order_that_filled_it_whatever_the_day(
    tmp_path: Path,
) -> None:
    import json

    from trading_research_agents.execution.desk import filled_from_records

    def cycle(name: str, intent: dict[str, Any], placed: dict[str, Any] | None) -> None:
        where = tmp_path / name
        where.mkdir()
        (where / "intents.jsonl").write_text(json.dumps(intent) + "\n", encoding="utf-8")
        if placed is not None:
            (where / "placed.json").write_text(json.dumps(placed), encoding="utf-8")

    # Filled yesterday, then a newer intent today that the venue cancelled unfilled.
    cycle(
        "2026-10-04T17-55-13Z-BTCUSDT",
        {
            "instrument": "BTCUSDT",
            "target": "long",
            "risk_bps": "1.71",
            "size": "0.002",
            "mid": "85299.25",
        },
        {"sent": True, "filled": "0.002"},
    )
    cycle(
        "2026-10-05T01-00-00Z-BTCUSDT",
        {
            "instrument": "BTCUSDT",
            "target": "short",
            "risk_bps": "0.90",
            "size": "0.001",
            "mid": "86000",
        },
        {"sent": True, "filled": "0", "status": "canceled"},
    )
    found = filled_from_records(tmp_path)
    assert found["BTCUSDT"].risk_bps == Decimal("1.71") and found["BTCUSDT"].side == "buy"
