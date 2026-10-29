"""The keeper looks at every instrument whatever one of them does, and says when it is not flat."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.trading.positions import KILL_READS, Keeper

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


class Hand:
    def __init__(self, held: Decimal, *, close_fails: bool = False, reads_fail: int = 0) -> None:
        self.held = held
        self.close_fails = close_fails
        self.reads_fail = reads_fail
        self.reads = 0
        self.cancelled = 0

    def position(self) -> Decimal:
        self.reads += 1
        if self.reads <= self.reads_fail:
            raise TimeoutError("the venue did not answer in 10 s")
        return self.held

    def cancel_open_orders(self) -> int:
        self.cancelled += 1
        return 0

    def close_position(self, reason: str) -> dict[str, Any]:
        if self.close_fails:
            raise RuntimeError("cannot read the account: RequestTimeout")
        return {"closed": True, "side": "sell", "size": str(self.held)}


def keeper(root: Path, hands: dict[str, Hand]) -> Keeper:
    return Keeper(
        root=root,
        hand_for=lambda name: hands[name],
        instruments=tuple(hands),
        bar_interval_s=60,
        timeout_bars=60,
        clock=lambda: NOW,
    )


def test_one_instrument_that_cannot_be_closed_does_not_stop_the_others(tmp_path: Path) -> None:
    hands = {
        "BTCUSDT": Hand(Decimal("0.01"), close_fails=True),
        "ETHUSDT": Hand(Decimal("0.2")),
    }
    kept = keeper(tmp_path, hands)
    said = kept.run(kill=True)
    assert any("BTCUSDT could not be flattened (RuntimeError" in line for line in said)
    assert any("ETHUSDT flattened for the kill switch" in line for line in said)
    assert kept.unresolved == ["BTCUSDT: RuntimeError: cannot read the account: RequestTimeout"]


def test_a_position_is_read_again_before_the_kill_switch_gives_up_on_it(tmp_path: Path) -> None:
    slow = Hand(Decimal("0.01"), reads_fail=KILL_READS - 1)
    gone = Hand(Decimal("0.01"), reads_fail=KILL_READS)
    kept = keeper(tmp_path, {"BTCUSDT": slow, "ETHUSDT": gone})
    said = kept.run(kill=True)
    assert slow.cancelled == 1 and gone.cancelled == 0
    assert kept.unresolved == ["ETHUSDT could not be read"]
    assert any(
        "ETHUSDT could not be read (TimeoutError: the venue did not answer" in s for s in said
    )


def test_outside_the_kill_switch_a_failed_read_is_named_with_its_cause(tmp_path: Path) -> None:
    kept = keeper(tmp_path, {"BTCUSDT": Hand(Decimal(0), reads_fail=1)})
    said = kept.run()
    assert said == [
        "positions: BTCUSDT could not be read (TimeoutError: the venue did not answer in 10 s)"
    ]
    assert kept.unresolved == []
