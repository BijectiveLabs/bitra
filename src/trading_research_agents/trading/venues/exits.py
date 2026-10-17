"""Where a venue keeps the exits attached to an entry, when ccxt's plain open-order list does not.

A stop and take profit sent with an entry become orders of their own at the venue. Bybit lists
them with the other open orders. OKX keeps them as algo orders, which ccxt reads only when asked
for them by kind, and on a spot pair they hold the coins they would sell: a close that does not
take them off first is refused for want of a balance. Each entry here is the list of reads that
finds them, read from ccxt 4.5.85 and checked on the OKX demo on 2026-10-17.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# The read, and the cancel that goes with it.
_ALGO = ({"trigger": True, "ordType": "oco"}, {"trigger": True, "ordType": "conditional"})
RESTING_EXITS: dict[str, tuple[Mapping[str, Any], ...]] = {
    "okx": _ALGO,
    "myokx": _ALGO,
    "okxus": _ALGO,
}
CANCEL_PARAMS: dict[str, Mapping[str, Any]] = {
    "okx": {"trigger": True},
    "myokx": {"trigger": True},
    "okxus": {"trigger": True},
}


def exit_reads(venue: str) -> tuple[Mapping[str, Any], ...]:
    """The extra open-order reads that find this venue's attached exits; none for most venues."""
    return RESTING_EXITS.get(venue.strip().lower(), ())


def exit_cancel_params(venue: str) -> Mapping[str, Any]:
    return CANCEL_PARAMS.get(venue.strip().lower(), {})
