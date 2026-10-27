"""What happens to a position after its order filled: reconciled, closed by time, or flattened.

The venue is the book. At the start of every pass this module asks it what is open, matches each
position to the order this desk filled, and then does one of three things: closes it with a
reduce-only order when it has outlived its hold limit, records the result when the venue's own
stop or take profit already closed it, or flattens everything when the kill switch is armed.
Every action is a line in `positions.jsonl`, and every finished round trip a line in
`round_trips.jsonl`, which is what the quant auditor reads.

A finished round trip also carries its excursion, read from the venue's own candles over the hold,
and its result as a multiple of the loss the cycle planned at the stop, which is what the sizing
reads.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.records import append_line, has_index, read_lines, recent
from trading_research_agents.trading.entry import SIDES, Entry
from trading_research_agents.trading.round_trip import (
    _base,
    _contract_unit,
    _fee_in_quote,
    _fills_since,
    _funding,
    _result,
    _settled,
    _span,
    _this_position,
    _trip_head,
)

JOURNAL = "positions.jsonl"
ROUND_TRIPS = "round_trips.jsonl"
HOLD_LIMITS = ("timeout", "horizon", "none")
FINISHED = ("closed_by_venue", "closed_by_time", "flattened")


def _opened(cycle: str) -> datetime | None:
    try:
        return datetime.strptime(cycle[:20], "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def filled_of(placed: Mapping[str, Any], intent: Mapping[str, Any]) -> Decimal:
    """How much of an entry filled, counting an unconfirmed send as filled in full.

    A send the venue never reported on may well be a position. Counting it keeps its risk on the
    book and lets the keeper look after it; the venue's own position read decides the rest.
    """
    if placed.get("filled") not in (None, ""):
        return Decimal(str(placed["filled"]))
    ended = placed.get("cancelled") or placed.get("status") in ("canceled", "cancelled", "rejected")
    if placed.get("sent") and not ended:
        return Decimal(str(intent.get("size") or 0))
    return Decimal(0)


def _sent(root: Path) -> Iterator[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Newest first: each cycle that reached the venue, what it sent, and its intent."""
    if not root.is_dir():
        return
    if has_index(root):
        for row in recent(root, sent=True):
            placed, intent = row.get("placed"), row.get("intent")
            if isinstance(placed, dict) and isinstance(intent, dict):
                yield str(row.get("cycle_id")), placed, intent
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
            yield cycle.name, placed, rows[-1]


def filled_entries(root: Path) -> dict[str, Entry]:
    """Per instrument, the newest entry this desk sent that the venue filled."""
    found: dict[str, Entry] = {}
    for cycle, placed, intent in _sent(Path(root)):
        filled = filled_of(placed, intent)
        opened = _opened(cycle)
        name = str(intent.get("instrument", ""))
        if not placed.get("sent") or filled <= 0 or opened is None or not name or name in found:
            continue
        price = placed.get("average") or placed.get("price") or intent.get("mid")
        found[name] = Entry(
            instrument=name,
            side=SIDES.get(str(intent.get("target", "")), ""),
            size=filled,
            price=Decimal(str(price)),
            opened_at=opened,
            order_id=str(placed.get("order_id", "")),
            cycle=cycle,
            horizon_bars=int(intent["horizon_bars"]) if intent.get("horizon_bars") else None,
            stop_price=placed.get("stop_price") or intent.get("stop_price"),
            take_price=placed.get("take_price") or intent.get("take_price"),
            loss_at_stop_bps=intent.get("loss_at_stop_bps"),
            priced_entry=_touch_of(intent),
            priced_fee_bps=intent.get("priced_fee_bps"),
            confirmed=placed.get("filled") not in (None, ""),
        )
    return found


def finished_orders(root: Path) -> set[str]:
    """Entry order ids whose positions this journal has already seen end."""
    path = Path(root) / JOURNAL
    done: set[str] = set()
    if not path.is_file():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        with suppress(json.JSONDecodeError):
            row = json.loads(line)
            if isinstance(row, Mapping) and row.get("event") in FINISHED:
                done.add(str(row.get("order_id", "")))
    return done


def _write(root: Path, name: str, row: Mapping[str, Any]) -> None:
    append_line(Path(root) / name, row, durable=True)


@dataclass
class Keeper:
    """Runs once at the start of every pass in venue mode. Decides nothing a model could."""

    root: Path
    hand_for: Callable[[str], Any]
    instruments: Sequence[str]
    bar_interval_s: int
    timeout_bars: int
    hold_limit: str = "timeout"
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    # Also look after every instrument with an entry on record, listed or not.
    follow_records: bool = False

    def limit_for(self, entry: Entry) -> timedelta | None:
        """How long this entry may be held, or None when time never closes it."""
        if self.hold_limit == "none":
            return None
        bars = self.timeout_bars
        if self.hold_limit == "horizon" and entry.horizon_bars:
            bars = min(entry.horizon_bars, self.timeout_bars)
        return timedelta(seconds=bars * self.bar_interval_s)

    def run(self, *, kill: bool = False) -> list[str]:
        """One look at every instrument. Returns the lines a person reads; the journal keeps all."""
        now = self.clock()
        entries = filled_entries(self.root)
        done = finished_orders(self.root)
        said: list[str] = []
        names = list(self.instruments)
        if self.follow_records:
            names = list(dict.fromkeys([*names, *entries]))
        for name in names:
            try:
                hand = self.hand_for(name)
                held = hand.position()
            except Exception as exc:
                said.append(f"positions: {name} could not be read ({exc.__class__.__name__})")
                continue
            entry = entries.get(name)
            if kill:
                cancelled = hand.cancel_open_orders()
                if held:
                    closed = hand.close_position("the kill switch is armed")
                    self._record(now, name, "flattened", entry, closed, cancelled=cancelled)
                    said.append(f"positions: {name} flattened for the kill switch: {_how(closed)}")
                    if closed.get("closed") and entry is not None and entry.order_id not in done:
                        self._round_trip(now, hand, entry, "kill switch")
                continue
            if entry is None or entry.order_id in done:
                continue
            if held:
                limit = self.limit_for(entry)
                age = now - entry.opened_at
                if limit is not None and age > limit:
                    reason = (
                        f"held {_span(age)}, past its {self.hold_limit} limit of {_span(limit)}"
                    )
                    closed = hand.close_position(reason)
                    event = "closed_by_time" if closed.get("closed") else "close_failed"
                    self._record(now, name, event, entry, closed)
                    said.append(f"positions: {name} {reason}: {_how(closed)}")
                    if closed.get("closed"):
                        self._round_trip(now, hand, entry, "time")
                continue
            if not entry.confirmed:
                # Never confirmed as filled, and nothing is held: there is no trade to write up.
                continue
            # Open by our record, flat at the venue: its stop or take profit closed it.
            self._record(now, name, "closed_by_venue", entry, {})
            trip = self._round_trip(now, hand, entry, "venue exit")
            said.append(f"positions: {name} was closed at the venue; {_trip_line(trip)}")
        if not kill:
            said.extend(self._complete(now, entries))
        return said

    def _complete(self, now: datetime, entries: Mapping[str, Entry]) -> list[str]:
        """Finish a round trip written before the venue had reported its exit fills."""
        path = Path(self.root) / ROUND_TRIPS
        if not path.is_file():
            return []
        pending: dict[str, Mapping[str, Any]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            with suppress(json.JSONDecodeError):
                row = json.loads(line)
                key = str(row.get("order_id", ""))
                if "net" in row:
                    pending.pop(key, None)
                else:
                    pending[key] = row
        said = []
        for row in pending.values():
            # A venue that has not shown the exit within an hour is not going to; stop asking.
            with suppress(ValueError, TypeError):
                if now - datetime.fromisoformat(str(row.get("at"))) > timedelta(hours=1):
                    continue
            name = str(row.get("instrument", ""))
            opened = datetime.fromisoformat(str(row.get("opened_at")))
            entry = Entry(
                instrument=name,
                side=str(row.get("side", "")),
                size=Decimal(str(row.get("size"))),
                price=Decimal(str(row.get("entry"))),
                opened_at=opened,
                order_id=str(row.get("order_id", "")),
                cycle=str(row.get("cycle", "")),
                stop_price=row.get("stop"),
                take_price=row.get("take"),
                loss_at_stop_bps=row.get("loss_at_stop_bps"),
                priced_entry=row.get("priced_entry"),
                priced_fee_bps=row.get("priced_fee_bps"),
            )
            try:
                trip = self._round_trip(
                    now, self.hand_for(name), entry, str(row.get("closed_by")), pending=False
                )
            except Exception as exc:
                said.append(f"positions: {name} round trip still open ({exc.__class__.__name__})")
                continue
            if "net" in trip:
                said.append(f"positions: {name} round trip completed; {_trip_line(trip)}")
        return said

    def _record(
        self,
        now: datetime,
        name: str,
        event: str,
        entry: Entry | None,
        detail: Mapping[str, Any],
        **extra: Any,
    ) -> None:
        _write(
            self.root,
            JOURNAL,
            {
                "at": now.isoformat(),
                "instrument": name,
                "event": event,
                # The entry's order id identifies the position; the close's own id sits apart.
                **{("close_order_id" if k == "order_id" else k): v for k, v in detail.items()},
                "order_id": None if entry is None else entry.order_id,
                "cycle": None if entry is None else entry.cycle,
                **extra,
            },
        )

    def _round_trip(
        self, now: datetime, hand: Any, entry: Entry, how: str, *, pending: bool = True
    ) -> dict[str, Any]:
        """The finished trade as the venue's own fills describe it, written for the auditors."""
        since = int(entry.opened_at.timestamp() * 1000)
        trip = _trip_head(now, entry, how)
        exit_side = "sell" if entry.side == "buy" else "buy"
        fills = _fills_since(hand, since, exit_side)
        try:
            unit = _contract_unit(hand)
        except Exception as exc:
            # Fills in contracts cannot be turned into a result without the contract size, and a
            # guess would write a wrong figure into the record the auditors read.
            trip["note"] = f"the contract size is unknown ({exc.__class__.__name__}), so no result"
            if pending:
                _write(self.root, ROUND_TRIPS, trip)
            return trip
        opening, exits = _this_position(fills, entry, exit_side, unit)
        base = _base(hand)
        venue = str(getattr(hand, "venue", ""))
        fees = sum((_fee_in_quote(f, base, venue) for f in (*opening, *exits)), Decimal(0))
        amount = sum((Decimal(str(f.get("amount") or 0)) for f in exits), Decimal(0))
        if amount == 0:
            # Held to resolution: no sale, the venue settled the shares at 1 or 0 instead.
            settled = _settled(hand, since)
            if settled is not None:
                exits = [settled]
                amount = Decimal(str(settled["amount"]))
                trip["settled"] = "won" if settled["price"] == 1 else "lost"
        last_exit = max((f.get("timestamp") or 0 for f in exits), default=None)
        if amount > 0:
            trip.update(_result(hand, entry, exits, amount, fees, last_exit))
        else:
            trip["note"] = "the venue returned no exit fills for this position"
        funding = _funding(hand, since, last_exit)
        if funding is not None:
            trip["funding"] = funding
        if pending or "net" in trip:
            # An unfinished trip is written once; later passes add the finished line, not copies.
            _write(self.root, ROUND_TRIPS, trip)
        return trip


def _touch_of(intent: Mapping[str, Any]) -> str | None:
    """The side of the book an entry crosses: the ask for a long, the bid for a short."""
    side = intent.get("ask") if intent.get("target") == "long" else intent.get("bid")
    found = side if side not in (None, "") else intent.get("mid")
    return None if found in (None, "") else str(found)


def _how(closed: Mapping[str, Any]) -> str:
    if closed.get("closed"):
        return f"{closed.get('side')} {closed.get('size')} reduce-only"
    return str(closed.get("error") or closed.get("note") or "not closed")


def _trip_line(trip: Mapping[str, Any]) -> str:
    if "net" not in trip:
        return str(trip.get("note", "no result recorded"))
    unit = f" {trip['quote']}" if trip.get("quote") else ""
    share = f" ({trip['net_bps']} bps of the entry notional)" if trip.get("net_bps") else ""
    return (
        f"exit {trip['exit']}, net {trip['net']}{unit}{share} after {trip['fees']}{unit} fees"
        + (
            f"; funding {trip['funding']}{unit} as the venue reports it"
            if trip.get("funding")
            else ""
        )
    )
