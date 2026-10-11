"""What the whole desk may have at risk at once, as opposed to any one trade.

Every bound before this one is about a single position: how much of the account it may lose at its
stop, how large it may be, what it must be worth. A desk that watches three instruments can pass all
of them three times over and still be carrying more than anyone decided to carry, because nothing
was ever asked about the total.

Two figures answer it, both arithmetic over records this software already wrote:

  max_open_risk_bps    the sum of what every open position loses at its own stop, against equity
  max_open_positions   how many instruments may be open at once

The first is the one that matters. A per-trade limit of fifty basis points is a different promise
when one position is open and when six are, and only the sum says which promise is being kept.

What counts as open comes from the caller, because only the caller knows where to look: the venue
itself in venue mode, and the intents on disk everywhere else. Whichever it was is written into the
record, so nobody has to guess how the figure was reached.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.records import has_index, read_lines, recent

SIDES = {"long": "buy", "short": "sell"}


class DeskError(ValueError):
    """The desk's own limits cannot be applied as declared."""


@dataclass(frozen=True)
class Desk:
    """[desk]: what this desk may be carrying across every instrument at once."""

    max_open_risk_bps: Decimal | None = None
    max_open_positions: int | None = None
    correlation_above: Decimal | None = None

    def __post_init__(self) -> None:
        if self.max_open_risk_bps is not None and self.max_open_risk_bps <= 0:
            raise DeskError("[desk].max_open_risk_bps is positive when it is declared")
        if self.max_open_positions is not None and self.max_open_positions < 1:
            raise DeskError("[desk].max_open_positions is at least 1; zero is a desk that is shut")
        if self.correlation_above is not None and not (0 < self.correlation_above <= 1):
            raise DeskError("[desk].correlation_above is above 0 and at most 1")

    @property
    def idle(self) -> bool:
        return (
            self.max_open_risk_bps is None
            and self.max_open_positions is None
            and self.correlation_above is None
        )

    @property
    def bounds_a_trade(self) -> bool:
        """Whether anything here applies to one more trade, as opposed to the screen."""
        return self.max_open_risk_bps is not None or self.max_open_positions is not None

    def record(self) -> dict[str, Any]:
        return {
            "max_open_risk_bps": (
                None if self.max_open_risk_bps is None else str(self.max_open_risk_bps)
            ),
            "max_open_positions": self.max_open_positions,
            "correlation_above": (
                None if self.correlation_above is None else str(self.correlation_above)
            ),
        }


@dataclass(frozen=True)
class Open:
    """One position this desk is already carrying, and what it has at risk."""

    instrument: str
    side: str = ""
    risk_bps: Decimal | None = None
    notional: Decimal | None = None

    def record(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "side": self.side,
            "risk_bps": None if self.risk_bps is None else str(self.risk_bps),
            "notional": None if self.notional is None else str(self.notional),
        }


@dataclass(frozen=True)
class DeskVerdict:
    """Whether one more trade fits what the desk is already carrying."""

    admitted: bool
    held: tuple[Open, ...] = ()
    adding_bps: Decimal | None = None
    open_risk_bps: Decimal | None = None
    source: str = ""
    reasons: tuple[str, ...] = ()
    unknown: tuple[str, ...] = field(default=())

    def record(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "held": [row.record() for row in self.held],
            "adding_bps": None if self.adding_bps is None else str(self.adding_bps),
            "open_risk_bps": None if self.open_risk_bps is None else str(self.open_risk_bps),
            "source": self.source,
            "reasons": list(self.reasons),
            "unknown": list(self.unknown),
        }


def from_records(root: Path, *, at: datetime) -> tuple[tuple[Open, ...], str]:
    """What the intents on disk say is open, for a desk with no venue to ask.

    One intent per instrument opens a position and the next one on that instrument replaces it, so
    the newest intent of the day is what stands. This is what a desk knows about itself when nobody
    else is keeping the book - in sandbox, and in signals mode where somebody else places the
    orders. It is a floor rather than a fact, and the verdict says which it is.
    """
    day = at.astimezone(tz=at.tzinfo).date().isoformat()
    newest: dict[str, dict[str, Any]] = {}
    root = Path(root)
    for path in sorted(root.glob("*/intents.jsonl")) if root.is_dir() else []:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and str(row.get("at", ""))[:10] == day:
                newest[str(row.get("instrument", ""))] = row
    held = tuple(
        Open(
            instrument=name,
            side=SIDES.get(str(row.get("target", "")), ""),
            risk_bps=_decimal(row.get("risk_bps")),
            notional=_notional(row),
        )
        for name, row in sorted(newest.items())
        if name
    )
    return held, f"the intents written today under {root}"


def filled_from_records(root: Path) -> dict[str, Open]:
    """Per instrument, the newest intent this desk sent that the venue actually filled.

    In venue mode the venue says a position exists; only the desk's own record says what it risks
    at its stop. The matching record is the order that filled, not simply the newest intent - a
    later intent may have been refused or cancelled - and not only today's: a position carried past
    midnight still has the stop it was opened with (seen on 2026-10-05, when the day rolled over and
    the desk could no longer state its own book).
    """
    found: dict[str, Open] = {}
    for placed, rows in _sent(Path(root)):
        filled = _decimal(placed.get("filled"))
        ended = placed.get("cancelled") or placed.get("status") in (
            "canceled",
            "cancelled",
            "rejected",
        )
        if filled is None and placed.get("sent") and not ended:
            # Never confirmed either way: counted in full, so its risk stays on the book.
            filled = _decimal(rows[-1].get("size"))
        if not placed.get("sent") or not filled:
            continue
        row = rows[-1]
        name = str(row.get("instrument", ""))
        if name and name not in found:
            # A partial fill carries that part of the risk, not the whole of what was asked for.
            asked = _decimal(row.get("size"))
            share = filled / asked if asked else Decimal(1)
            risk = _decimal(row.get("risk_bps"))
            notional = _notional(row)
            found[name] = Open(
                instrument=name,
                side=SIDES.get(str(row.get("target", "")), ""),
                risk_bps=None if risk is None else (risk * min(share, Decimal(1))),
                notional=None if notional is None else notional * min(share, Decimal(1)),
            )
    return found


def _sent(root: Path) -> Iterator[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Newest first: what each cycle that reached the venue sent, and the intents behind it."""
    if not root.is_dir():
        return
    if has_index(root):
        for row in recent(root, sent=True):
            if isinstance(row.get("placed"), dict) and isinstance(row.get("intent"), dict):
                yield row["placed"], [row["intent"]]
        return
    for cycle in sorted(root.iterdir(), reverse=True):
        placed_path, intents_path = cycle / "placed.json", cycle / "intents.jsonl"
        if not placed_path.is_file() or not intents_path.is_file():
            continue
        try:
            placed = json.loads(placed_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = read_lines(intents_path)
        if isinstance(placed, dict) and rows:
            yield placed, rows


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _notional(row: dict[str, Any]) -> Decimal | None:
    size, mid = _decimal(row.get("size")), _decimal(row.get("mid"))
    return None if size is None or mid is None else size * mid


def within_desk(
    desk: Desk,
    *,
    instrument: str,
    adding_bps: Decimal | None,
    held: Sequence[Open],
    source: str = "",
) -> DeskVerdict:
    """Does one more trade fit? Every refusal names the figure and the positions behind it.

    A position this desk is already carrying on the same instrument is not counted twice: the entry
    gate upstream refuses a second entry on an instrument that already has one, so reaching here
    with the same name means the caller meant to replace it, not to add to it.
    """
    others = [row for row in held if row.instrument != instrument]
    known = [row.risk_bps for row in others if row.risk_bps is not None]
    unknown = tuple(sorted(row.instrument for row in others if row.risk_bps is None))
    carried = sum(known, Decimal(0)) if known else Decimal(0)
    total = carried + (adding_bps or Decimal(0))
    reasons: list[str] = []

    if desk.max_open_positions is not None and len(others) + 1 > desk.max_open_positions:
        names = ", ".join(sorted(row.instrument for row in others))
        reasons.append(
            f"{len(others)} position(s) already open ({names}) and the desk declared at most "
            f"{desk.max_open_positions} at once"
        )
    if desk.max_open_risk_bps is not None and total > desk.max_open_risk_bps:
        reasons.append(
            f"{carried} bps of equity is already at risk across {len(others)} position(s) and this "
            f"trade adds {adding_bps or 0}; {total} bps is above the declared "
            f"{desk.max_open_risk_bps} bps"
        )
    if desk.max_open_risk_bps is not None and unknown:
        # A position whose risk nobody can state is a position this sum cannot include, and a sum
        # that quietly leaves something out is worse than no sum at all.
        reasons.append(
            f"what {', '.join(unknown)} has at risk is not on the record, so the desk's total "
            "cannot be stated; close it, or record its stop, before adding to the book"
        )
    return DeskVerdict(
        admitted=not reasons,
        held=tuple(others),
        adding_bps=adding_bps,
        open_risk_bps=total if known or adding_bps is not None else None,
        source=source,
        reasons=tuple(reasons),
        unknown=unknown,
    )
