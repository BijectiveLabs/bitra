"""What an operator does once, at the exchange, before a venue's first order: the steps no API call
can take for them. Each line was learned on that venue's practice API; docs Exchange accounts and
keys has the longer version.
"""

from __future__ import annotations

NOTES: dict[str, tuple[str, ...]] = {
    "bybit": ("make the key in Demo Trading mode; a live key does not reach the demo",),
    "okx": (
        "make the key inside Demo trading",
        "set the account mode to Futures in the app or website; a key cannot change it",
    ),
    "bitget": ("make the key in Demo trading mode; on a unified account give it manage and trade",),
    "binance": ("make the key on Binance's Demo Trading site, with futures trading allowed",),
    "bingx": (
        "the same key reaches the demo (VST) and your live account: allow trading only, "
        "no withdrawals or transfers, and bind it to your IP",
    ),
    "gate": (
        "use a key issued for Gate's testnet; add wallet read so the desk can read your fees",
    ),
}
ONE_WAY = (
    "the position mode must be one-way; `tra keys --probe --prepare` sets it on a demo or testnet"
)


def notes_for(venue: str) -> tuple[str, ...]:
    """The one-time steps at the exchange for this venue, then the one every venue shares."""
    return (*NOTES.get(venue.strip().lower(), ()), ONE_WAY)
