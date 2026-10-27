"""An open position's entry as this desk sent it: side, size, price, exits and what the cycle
planned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

SIDES = {"long": "buy", "short": "sell"}


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
    # The price the entry was priced at (the touch it would cross) and the fees the cost gate
    # counted, so what the venue charged can be set against what the trade expected.
    priced_entry: str | None = None
    priced_fee_bps: str | None = None
    # False when the venue never reported a fill: such an entry is looked after only while the
    # venue actually shows a position, and never written up as a finished trade on its own.
    confirmed: bool = True
