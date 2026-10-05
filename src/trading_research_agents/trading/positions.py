"""What happens to a position after its order filled: reconciled, closed by time, or flattened.

The venue is the book. At the start of every pass this module asks it what is open, matches each
position to the order this desk filled, and then does one of three things: closes it with a
reduce-only order when it has outlived its hold limit, records the result when the venue's own
stop or take profit already closed it, or flattens everything when the kill switch is armed.
Every action is a line in `positions.jsonl`, and every finished round trip a line in
`round_trips.jsonl`, which is what the quant auditor reads.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.records import append_line

JOURNAL = "positions.jsonl"
ROUND_TRIPS = "round_trips.jsonl"
HOLD_LIMITS = ("timeout", "horizon", "none")
SIDES = {"long": "buy", "short": "sell"}
FINISHED = ("closed_by_venue", "closed_by_time", "flattened")


@dataclass(frozen=True)
class Entry:
    """One filled entry this desk sent, as its own records describe it."""

    instrument: str
    side: str
    size: Decimal
    price: Decimal
    opened_at: datetime
    order_id: str
    cycle: str
    horizon_bars: int | None = None
    stop_price: str | None = None
    take_price: str | None = None
    # False when the venue never reported a fill: such an entry is looked after only while the
    # venue actually shows a position, and never written up as a finished trade on its own.
    confirmed: bool = True


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


def filled_entries(root: Path) -> dict[str, Entry]:
    """Per instrument, the newest entry this desk sent that the venue filled."""
    found: dict[str, Entry] = {}
    root = Path(root)
    for cycle in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
        placed_path, intents_path = cycle / "placed.json", cycle / "intents.jsonl"
        if not placed_path.is_file() or not intents_path.is_file():
            continue
        try:
            placed = json.loads(placed_path.read_text(encoding="utf-8"))
            lines = intents_path.read_text(encoding="utf-8").splitlines()
            intent = json.loads([line for line in lines if line.strip()][-1])
        except (OSError, json.JSONDecodeError, IndexError):
            continue
        filled = filled_of(placed, intent)
        opened = _opened(cycle.name)
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
            cycle=cycle.name,
            horizon_bars=int(intent["horizon_bars"]) if intent.get("horizon_bars") else None,
            stop_price=placed.get("stop_price"),
            take_price=placed.get("take_price"),
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
        for name in self.instruments:
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
        trip: dict[str, Any] = {
            "at": now.isoformat(),
            "instrument": entry.instrument,
            "side": entry.side,
            "size": str(entry.size),
            "entry": str(entry.price),
            "opened_at": entry.opened_at.isoformat(),
            "order_id": entry.order_id,
            "cycle": entry.cycle,
            "closed_by": how,
        }
        exit_side = "sell" if entry.side == "buy" else "buy"
        fills: list[Mapping[str, Any]] = []
        exits: list[Mapping[str, Any]] = []
        # A close that just went out can take a moment to show in the venue's fills (seen on
        # 2026-10-05: an empty answer straight after a reduce-only close).
        for attempt in range(6):
            with suppress(Exception):
                fills = list(hand.client.fetch_my_trades(hand.symbol, since=since) or [])
            exits = [f for f in fills if f.get("side") == exit_side]
            if exits or attempt == 5:
                break
            pause = getattr(hand, "sleep", None)
            if pause is not None:
                pause(1.0)
        # Only this position's fills: its entry side before the first exit, and exits up to its
        # size. A later entry on the same instrument is the next position's, not this one's
        # (seen on 2026-10-05: the next entry's fee was counted against the trade it followed).
        ordered = sorted(fills, key=lambda f: f.get("timestamp") or 0)
        first_exit = next(
            (f.get("timestamp") or 0 for f in ordered if f.get("side") == exit_side), None
        )
        opening = [
            f
            for f in ordered
            if f.get("side") == entry.side
            and (first_exit is None or (f.get("timestamp") or 0) <= first_exit)
        ]
        exits = _up_to(exits and [f for f in ordered if f.get("side") == exit_side], entry.size)
        opening = _up_to(opening, entry.size)
        fees = sum(
            (Decimal(str((f.get("fee") or {}).get("cost") or 0)) for f in (*opening, *exits)),
            Decimal(0),
        )
        amount = sum((Decimal(str(f.get("amount") or 0)) for f in exits), Decimal(0))
        last_exit = max((f.get("timestamp") or 0 for f in exits), default=None)
        if amount > 0:
            paid = sum(
                (Decimal(str(f.get("amount") or 0)) * Decimal(str(f.get("price") or 0)))
                for f in exits
            )
            exit_price = paid / amount
            sign = Decimal(1) if entry.side == "buy" else Decimal(-1)
            gross = (exit_price - entry.price) * min(amount, entry.size) * sign
            trip.update(
                {
                    "exit": str(exit_price.quantize(Decimal("0.0001"))),
                    "gross": str(gross.quantize(Decimal("0.0001"))),
                    "fees": str(fees.quantize(Decimal("0.0001"))),
                    "net": str((gross - fees).quantize(Decimal("0.0001"))),
                }
            )
        else:
            trip["note"] = "the venue returned no exit fills for this position"
        funding = getattr(hand.client, "fetch_funding_history", None)
        if funding is not None:
            with suppress(Exception):
                rows = [
                    row
                    for row in funding(hand.symbol, since=since) or []
                    if last_exit is None or (row.get("timestamp") or 0) <= last_exit
                ]
                # Reported as the venue signs it, and kept out of `net`, which is price and fees.
                trip["funding"] = str(
                    sum((Decimal(str(row.get("amount") or 0)) for row in rows), Decimal(0))
                )
        if pending or "net" in trip:
            # An unfinished trip is written once; later passes add the finished line, not copies.
            _write(self.root, ROUND_TRIPS, trip)
        return trip


def _up_to(fills: Any, size: Decimal) -> list[Mapping[str, Any]]:
    """Fills in order until they add up to `size`; whatever comes after belongs elsewhere."""
    kept: list[Mapping[str, Any]] = []
    total = Decimal(0)
    for fill in fills or []:
        if total >= size:
            break
        kept.append(fill)
        total += Decimal(str(fill.get("amount") or 0))
    return kept


def _span(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _how(closed: Mapping[str, Any]) -> str:
    if closed.get("closed"):
        return f"{closed.get('side')} {closed.get('size')} reduce-only"
    return str(closed.get("error") or closed.get("note") or "not closed")


def _trip_line(trip: Mapping[str, Any]) -> str:
    if "net" not in trip:
        return str(trip.get("note", "no result recorded"))
    return f"exit {trip['exit']}, net {trip['net']} after {trip['fees']} of fees" + (
        f"; funding {trip['funding']} as the venue reports it" if trip.get("funding") else ""
    )
