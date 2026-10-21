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

from trading_research_agents.data.schema import Direction
from trading_research_agents.execution.excursion import excursion_of
from trading_research_agents.records import append_line, has_index, read_lines, recent
from trading_research_agents.trading.venues.fills import charged

JOURNAL = "positions.jsonl"
ROUND_TRIPS = "round_trips.jsonl"
HOLD_LIMITS = ("timeout", "horizon", "none")
SIDES = {"long": "buy", "short": "sell"}
FINISHED = ("closed_by_venue", "closed_by_time", "flattened")
# The finest candles that cover a hold in at most this many bars; a longer hold reads coarser ones.
FRAMES = (("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600))
MAX_BARS = 1000


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
    # What the cycle planned to lose at the stop, in bps of the entry notional, round trip included.
    loss_at_stop_bps: str | None = None
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
            **{
                key: value
                for key, value in (
                    ("stop", entry.stop_price),
                    ("take", entry.take_price),
                    ("loss_at_stop_bps", entry.loss_at_stop_bps),
                )
                if value is not None
            },
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
        try:
            unit = _contract_unit(hand)
        except Exception as exc:
            # Fills in contracts cannot be turned into a result without the contract size, and a
            # guess would write a wrong figure into the record the auditors read.
            trip["note"] = f"the contract size is unknown ({exc.__class__.__name__}), so no result"
            if pending:
                _write(self.root, ROUND_TRIPS, trip)
            return trip
        # The venue's fills are in contracts; the entry and the record are in the base.
        fills = [{**f, "amount": Decimal(str(f.get("amount") or 0)) * unit} for f in fills]
        exits = [f for f in fills if f.get("side") == exit_side]
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
            paid = sum(
                (Decimal(str(f.get("amount") or 0)) * Decimal(str(f.get("price") or 0)))
                for f in exits
            )
            exit_price = paid / amount
            sign = Decimal(1) if entry.side == "buy" else Decimal(-1)
            gross = (exit_price - entry.price) * min(amount, entry.size) * sign
            notional = entry.price * min(amount, entry.size)
            trip.update(
                {
                    "quote": _settlement(hand),
                    "net_bps": str(((gross - fees) / notional * 10_000).quantize(Decimal("0.1"))),
                    "exit": str(exit_price.quantize(Decimal("0.0001"))),
                    "gross": str(gross.quantize(Decimal("0.0001"))),
                    "fees": str(fees.quantize(Decimal("0.0001"))),
                    "net": str((gross - fees).quantize(Decimal("0.0001"))),
                }
            )
            net_bps = (gross - fees) / notional * 10_000
            planned = _positive(entry.loss_at_stop_bps)
            if planned is not None:
                # The result in units of the loss the cycle planned at the stop: -1 is a stop hit
                # as priced, and the figure the sizing reads, whatever the size was.
                trip["r"] = str((net_bps / planned).quantize(Decimal("0.001")))
            try:
                excursion = _excursion(hand, entry, last_exit)
            except Exception as exc:
                # The result stands without it; the record says why the shape is missing.
                excursion = {"note": f"the candles could not be read ({exc.__class__.__name__})"}
            if excursion is not None:
                trip["excursion"] = excursion
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


def _positive(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return number if number.is_finite() and number > 0 else None


def _excursion(hand: Any, entry: Entry, until_ms: int | None) -> dict[str, Any] | None:
    """How far the position went for and against it, from the venue's candles over the hold.

    The first and the last bar can hold prices from just before the entry and just after the exit,
    which is why the finest candles that cover the hold are used.
    """
    fetch = getattr(hand.client, "fetch_ohlcv", None)
    since = int(entry.opened_at.timestamp() * 1000)
    if fetch is None or until_ms is None or until_ms <= since or entry.side not in SIDES.values():
        return None
    frame, step = next(
        ((f, s) for f, s in FRAMES if (until_ms - since) / (s * 1000) <= MAX_BARS), FRAMES[-1]
    )
    start = since - since % (step * 1000)
    rows: dict[int, tuple[Decimal, Decimal]] = {}
    cursor = start
    while cursor <= until_ms and len(rows) < MAX_BARS:
        got = [r for r in fetch(hand.symbol, frame, cursor, MAX_BARS) or [] if int(r[0]) >= cursor]
        if not got:
            break
        for row in got:
            if int(row[0]) <= until_ms:
                rows[int(row[0])] = (Decimal(str(row[2])), Decimal(str(row[3])))
        cursor = max(int(r[0]) for r in got) + step * 1000
    if not rows:
        return None
    direction = Direction.LONG if entry.side == "buy" else Direction.SHORT
    seen = excursion_of(entry.price, direction, [rows[t] for t in sorted(rows)])
    out: dict[str, Any] = {
        **seen.record(),
        "timeframe": frame,
        "source": "the venue's candles over the hold",
        "complete": max(rows) + step * 1000 > until_ms,
    }
    for name, price, side in (
        ("mae_of_stop", entry.stop_price, -1),
        ("mfe_of_take", entry.take_price, 1),
    ):
        level = _positive(price)
        if level is None:
            continue
        distance = abs(level - entry.price) / entry.price * 10_000
        if distance > 0:
            moved = seen.mae_bps if side < 0 else seen.mfe_bps
            out[name] = str((abs(moved) / distance).quantize(Decimal("0.001")))
    return out


def _span(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _how(closed: Mapping[str, Any]) -> str:
    if closed.get("closed"):
        return f"{closed.get('side')} {closed.get('size')} reduce-only"
    return str(closed.get("error") or closed.get("note") or "not closed")


def _contract_unit(hand: Any) -> Decimal:
    """How much of the base one of the venue's contracts is; 1 for a hand that does not say."""
    size = getattr(hand, "contract_size", None)
    return Decimal(1) if size is None else Decimal(size())


def _settled(hand: Any, since: int) -> dict[str, Any] | None:
    """A prediction outcome's settlement since `since`, as an exit fill at its payout price."""
    read = getattr(hand, "settlement", None)
    if read is None:
        return None
    with suppress(Exception):
        found = read(since)
        if found is not None:
            return {
                "side": "sell",
                "amount": found["amount"],
                "price": found["price"],
                "fee": {"cost": 0},
                "timestamp": found.get("timestamp") or 0,
            }
    return None


def _base(hand: Any) -> str:
    with suppress(Exception):
        return str(hand.market().base)
    return str(getattr(hand, "symbol", "")).partition("/")[0]


def _fee_in_quote(fill: Mapping[str, Any], base: str, venue: str = "") -> Decimal:
    """A fill's fee in the quote currency. A spot buy is often charged in the coin it bought."""
    fee = fill.get("fee") or {}
    said = charged(venue, fill) if venue else None
    cost = Decimal(str(fee.get("cost") or 0)) if said is None else said
    if cost < 0 and fill.get("takerOrMaker") == "taker":
        # No venue pays a taker to take; a negative taker fee is a sign read the wrong way.
        cost = -cost
    if base and str(fee.get("currency") or "") == base:
        return cost * Decimal(str(fill.get("price") or 0))
    return cost


def _settlement(hand: Any) -> str:
    """The currency the result is counted in, as the venue describes the market."""
    with suppress(Exception):
        found = str(hand.market().settlement)
        if found:
            return found
    # A hand that cannot describe its market: the quote half of a ccxt symbol, `XRP/USDT:USDT`.
    return str(getattr(hand, "symbol", "")).partition("/")[2].partition(":")[0]


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
