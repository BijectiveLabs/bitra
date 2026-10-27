"""A closed position as the venue's fills describe it: this position's fills, the result in quote
and basis points, funding, and the price path while it was held.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from trading_research_agents.data.schema import Direction
from trading_research_agents.execution.excursion import excursion_of
from trading_research_agents.trading.entry import SIDES, Entry
from trading_research_agents.trading.venues.fills import charged

# The finest candles that cover a hold in at most this many bars; a longer hold reads coarser ones.
FRAMES = (("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600))


MAX_BARS = 1000


def _trip_head(now: datetime, entry: Entry, how: str) -> dict[str, Any]:
    """What the record says about the trade before any fill is read."""
    return {
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
                ("priced_entry", entry.priced_entry),
                ("priced_fee_bps", entry.priced_fee_bps),
            )
            if value is not None
        },
    }


def _fills_since(hand: Any, since: int, exit_side: str) -> list[Mapping[str, Any]]:
    """The account's fills on the instrument since the entry, read until an exit shows."""
    fills: list[Mapping[str, Any]] = []
    # A close that just went out can take a moment to show in the venue's fills (seen on
    # 2026-10-05: an empty answer straight after a reduce-only close).
    for attempt in range(6):
        with suppress(Exception):
            fills = list(hand.client.fetch_my_trades(hand.symbol, since=since) or [])
        if any(f.get("side") == exit_side for f in fills) or attempt == 5:
            break
        pause = getattr(hand, "sleep", None)
        if pause is not None:
            pause(1.0)
    return fills


def _this_position(
    fills: list[Mapping[str, Any]], entry: Entry, exit_side: str, unit: Decimal
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Only this position's fills, in the base: its entry side before the first exit, and exits up
    to its size. A later entry on the same instrument is the next position's, not this one's (seen
    on 2026-10-05: the next entry's fee was counted against the trade it followed)."""
    in_base = [{**f, "amount": Decimal(str(f.get("amount") or 0)) * unit} for f in fills]
    ordered = sorted(in_base, key=lambda f: f.get("timestamp") or 0)
    first_exit = next(
        (f.get("timestamp") or 0 for f in ordered if f.get("side") == exit_side), None
    )
    opening = [
        f
        for f in ordered
        if f.get("side") == entry.side
        and (first_exit is None or (f.get("timestamp") or 0) <= first_exit)
    ]
    exits = [f for f in ordered if f.get("side") == exit_side]
    return _up_to(opening, entry.size), _up_to(exits, entry.size)


def _result(
    hand: Any,
    entry: Entry,
    exits: list[Mapping[str, Any]],
    amount: Decimal,
    fees: Decimal,
    last_exit: Any,
) -> dict[str, Any]:
    """The trade's result in the quote and in basis points, and how it compares to its plan."""
    paid = sum(
        (Decimal(str(f.get("amount") or 0)) * Decimal(str(f.get("price") or 0))) for f in exits
    )
    exit_price = paid / amount
    sign = Decimal(1) if entry.side == "buy" else Decimal(-1)
    gross = (exit_price - entry.price) * min(amount, entry.size) * sign
    notional = entry.price * min(amount, entry.size)
    net_bps = (gross - fees) / notional * 10_000
    result: dict[str, Any] = {
        "quote": _settlement(hand),
        "net_bps": str(net_bps.quantize(Decimal("0.1"))),
        "exit": str(exit_price.quantize(Decimal("0.0001"))),
        "gross": str(gross.quantize(Decimal("0.0001"))),
        "fees": str(fees.quantize(Decimal("0.0001"))),
        "net": str((gross - fees).quantize(Decimal("0.0001"))),
        "fees_bps": str((fees / notional * 10_000).quantize(Decimal("0.01"))),
    }
    priced = _positive(entry.priced_entry)
    if priced is not None:
        # Positive is worse than priced: a long that filled above the ask it was priced at, a
        # short below the bid.
        result["entry_slippage_bps"] = str(
            (sign * (entry.price - priced) / priced * 10_000).quantize(Decimal("0.01"))
        )
    planned = _positive(entry.loss_at_stop_bps)
    if planned is not None:
        # The result in units of the loss the cycle planned at the stop: -1 is a stop hit as
        # priced, and the figure the sizing reads, whatever the size was.
        result["r"] = str((net_bps / planned).quantize(Decimal("0.001")))
    try:
        excursion = _excursion(hand, entry, last_exit)
    except Exception as exc:
        # The result stands without it; the record says why the shape is missing.
        excursion = {"note": f"the candles could not be read ({exc.__class__.__name__})"}
    if excursion is not None:
        result["excursion"] = excursion
    return result


def _funding(hand: Any, since: int, last_exit: Any) -> str | None:
    """Funding over the position's life, as the venue signs it, or nothing when it cannot say.
    It is kept out of `net`, which is price and fees."""
    history = getattr(hand.client, "fetch_funding_history", None)
    if history is None:
        return None
    with suppress(Exception):
        rows = [
            row
            for row in history(hand.symbol, since=since) or []
            if last_exit is None or (row.get("timestamp") or 0) <= last_exit
        ]
        return str(sum((Decimal(str(row.get("amount") or 0)) for row in rows), Decimal(0)))
    return None


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
