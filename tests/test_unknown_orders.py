from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.execution.unknown import (
    SETTLE_S,
    blocking,
    record_unknown,
    resolutions,
    resolve,
    unresolved,
)
from trading_research_agents.trading.positions import Keeper, filled_entries

AT = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
CYCLE = "2026-10-27T09-00-00Z"


def noted(root: Path, cid: str = "tra1", instrument: str = "BTCUSDT") -> None:
    record_unknown(
        root, cycle=CYCLE, instrument=instrument, client_order_id=cid, said="timeout", at=AT
    )


def test_an_unknown_order_holds_back_its_instrument_until_it_is_resolved(tmp_path: Path) -> None:
    noted(tmp_path)
    assert "tra1" in unresolved(tmp_path)
    assert "tra1" in blocking(tmp_path, "BTCUSDT") and blocking(tmp_path, "ETHUSDT") == ""
    resolve(tmp_path, client_order_id="tra1", outcome="not_found", order=None, at=AT)
    assert unresolved(tmp_path) == {} and blocking(tmp_path, "BTCUSDT") == ""


class Hand:
    def __init__(self, found: dict[str, Any] | None, unread: list[str] | None = None) -> None:
        self.found = found
        self.unread = unread or []
        self.asked: list[str] = []

    def find_sent(self, client_id: str) -> tuple[dict[str, Any] | None, list[str]]:
        self.asked.append(client_id)
        return self.found, self.unread

    def position(self) -> Decimal:
        return Decimal(0)


def keeper(root: Path, hand: Hand, now: datetime) -> Keeper:
    return Keeper(
        root=root,
        hand_for=lambda name: hand,
        instruments=(),
        bar_interval_s=60,
        timeout_bars=60,
        clock=lambda: now,
    )


def test_the_keeper_finds_an_unknown_order_by_its_client_id(tmp_path: Path) -> None:
    noted(tmp_path)
    order = {"id": "o-7", "status": "closed", "filled": 0.01, "average": 100000.5}
    said = keeper(tmp_path, Hand(order), AT + timedelta(seconds=30)).run()
    assert any("found at the venue" in line for line in said)
    assert unresolved(tmp_path) == {} and resolutions(tmp_path)["tra1"]["order"]["id"] == "o-7"


def test_an_order_not_listed_stays_unknown_until_the_settling_time_has_passed(
    tmp_path: Path,
) -> None:
    noted(tmp_path)
    hand = Hand(None)
    keeper(tmp_path, hand, AT + timedelta(seconds=SETTLE_S - 1)).run()
    assert "tra1" in unresolved(tmp_path)
    keeper(tmp_path, hand, AT + timedelta(seconds=SETTLE_S + 1)).run()
    assert unresolved(tmp_path) == {}
    assert resolutions(tmp_path)["tra1"]["outcome"] == "not_found"


def test_a_venue_that_will_not_list_its_orders_never_resolves_an_order_as_absent(
    tmp_path: Path,
) -> None:
    noted(tmp_path)
    hand = Hand(None, unread=["fetch_open_orders (RequestTimeout)"])
    said = keeper(tmp_path, hand, AT + timedelta(seconds=SETTLE_S * 3)).run()
    assert "tra1" in unresolved(tmp_path)
    assert any("still UNKNOWN" in line for line in said)


def test_a_found_unknown_order_is_an_entry_the_keeper_looks_after(tmp_path: Path) -> None:
    from trading_research_agents.records import append_line, write_json

    cycle = tmp_path / CYCLE
    cycle.mkdir()
    write_json(
        cycle / "placed.json",
        {"sent": None, "status": "UNKNOWN", "client_order_id": "tra1", "order_id": ""},
    )
    append_line(
        cycle / "intents.jsonl", {"instrument": "BTCUSDT", "target": "long", "size": "0.01"}
    )
    assert filled_entries(tmp_path) == {}
    noted(tmp_path)
    order = {"id": "o-7", "status": "closed", "filled": "0.01", "average": "100000.5"}
    resolve(tmp_path, client_order_id="tra1", outcome="found", order=order, at=AT)
    entry = filled_entries(tmp_path)["BTCUSDT"]
    assert entry.order_id == "o-7" and entry.price == Decimal("100000.5") and entry.confirmed
