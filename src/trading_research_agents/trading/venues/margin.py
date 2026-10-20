"""Leverage, margin mode and position mode on accounts ccxt's unified calls do not fully reach.

Most venues take `set_margin_mode`, `set_leverage` and `fetch_leverage` as ccxt unifies them, and
the hand uses those. A venue listed here keeps the settings somewhere else, and its entry says
where. Orders, positions, balances and cancels still go through ccxt's unified calls.

Bitget's unified trading account (read on its demo API on 2026-10-20, ccxt 4.5.85): with ccxt's
`uta` option, orders, positions, balances and leverage go to the v3 API, but the margin mode change
and the leverage read still go to the classic futures API, which a unified key cannot reach (40014).
On that account the leverage is held per symbol and per margin mode, both rows can exist at once,
and each order names the margin mode it trades in, crossed when it names none. So the declared
mode is set with the leverage, read back from the matching row, and sent with every order.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

# ccxt's spelling of a margin mode, and Bitget's.
_BITGET_MODES = {"isolated": "isolated", "cross": "crossed", "crossed": "crossed"}


class AccountSettings(Protocol):
    def order_params(self, margin_mode: str) -> dict[str, Any]: ...

    def apply(self, symbol: str, leverage: Decimal, margin_mode: str) -> None: ...

    def leverage(self, symbol: str, margin_mode: str) -> Decimal | None: ...

    def hedged(self) -> bool | None: ...


class BitgetUnified:
    """Bitget's unified trading account, through the v3 settings ccxt exposes but does not read."""

    def __init__(self, client: Any) -> None:
        self.client = client

    @staticmethod
    def _mode(margin_mode: str) -> str:
        mode = _BITGET_MODES.get(margin_mode.strip().lower())
        if mode is None:
            raise ValueError(f"margin mode {margin_mode!r} is not one Bitget has")
        return mode

    def order_params(self, margin_mode: str) -> dict[str, Any]:
        return {"marginMode": self._mode(margin_mode)}

    def apply(self, symbol: str, leverage: Decimal, margin_mode: str) -> None:
        self.client.set_leverage(float(leverage), symbol, {"marginMode": self._mode(margin_mode)})

    def _settings(self) -> Mapping[str, Any]:
        return (self.client.privateUtaGetV3AccountSettings() or {}).get("data") or {}

    def leverage(self, symbol: str, margin_mode: str) -> Decimal | None:
        venue_id = str(self.client.market(symbol).get("id") or "")
        mode = self._mode(margin_mode)
        for row in self._settings().get("symbolConfigList") or []:
            if row.get("symbol") != venue_id or row.get("marginMode") != mode:
                continue
            try:
                return Decimal(str(row.get("leverage")))
            except (InvalidOperation, ValueError):
                return None
        return None

    def hedged(self) -> bool | None:
        hold = str(self._settings().get("holdMode") or "")
        return {"hedge_mode": True, "one_way_mode": False}.get(hold)


def _bitget(client: Any) -> AccountSettings | None:
    options = getattr(client, "options", None) or {}
    if not options.get("uta") or getattr(client, "privateUtaGetV3AccountSettings", None) is None:
        return None
    return BitgetUnified(client)


SETTINGS = {"bitget": _bitget}


def account_settings(venue: str, client: Any) -> AccountSettings | None:
    """Where this venue's account keeps its leverage, when ccxt's unified calls do not reach it."""
    find = SETTINGS.get(venue.strip().lower())
    return find(client) if find is not None else None


def _bitget_route(client: Any) -> None:
    """Send a unified account's key through ccxt's unified account API.

    The key's grants say which account it was made for: `GET /api/v2/spot/account/info` lists them,
    and a key made for the unified account carries only `ua` grants. An operator who set `uta` in
    [execution].ccxt_options, either way, is not overruled.
    """
    options = getattr(client, "options", None)
    read = getattr(client, "privateSpotGetV2SpotAccountInfo", None)
    # ccxt's own default is None, so a value here is the operator's.
    if not isinstance(options, dict) or options.get("uta") is not None or read is None:
        return
    try:
        grants = [str(g) for g in ((read() or {}).get("data") or {}).get("authorities") or []]
    except Exception:
        # A key that cannot be read here fails loudly on the account read that follows.
        return
    if grants and all(g.startswith("ua") for g in grants):
        options["uta"] = True


ROUTES = {"bitget": _bitget_route}


def route(venue: str, client: Any) -> None:
    """Point a signed client at the account its key belongs to, where a venue has two kinds."""
    send = ROUTES.get(venue.strip().lower())
    if send is not None:
        send(client)
