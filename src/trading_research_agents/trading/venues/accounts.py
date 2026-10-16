"""Account settings a venue has that ccxt does not unify, checked before the first order.

ccxt sends the same order to every venue, but some accounts refuse it because of a setting on the
account itself. Each check here reads that setting through the client the order would use and
says, in the venue's own words, what to change. A check that cannot read the setting says nothing:
not knowing is not a reason to refuse, and the venue still refuses at its end if it must.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

# A key that expires inside a session strands a position mid-trade; a week leaves room to rotate.
MIN_KEY_LIFE_DAYS = 7


def _okx(client: Any) -> str:
    """OKX's account mode. Spot mode (acctLv 1) cannot trade perpetuals at all.

    OKX's help centre says the mode is set on the website or app and cannot be changed through the
    API, and its API reference says the first switch to any mode has to be made there. So this
    only names the setting; it does not try to change it.

    Seen on the demo on 2026-10-08: every order came back with sCode 51010, "You can't complete
    this request under your current account mode".
    """
    read = getattr(client, "privateGetAccountConfig", None)
    if read is None:
        return ""
    try:
        config = (read().get("data") or [{}])[0]
    except Exception:
        return ""
    if str(config.get("acctLv", "")) == "1":
        return (
            "the OKX account is in Spot mode, which cannot trade perpetuals. Switch it to Futures "
            "mode in the OKX app or website (Menu, Settings, Trading mode), in Demo trading for a "
            "demo key; OKX does not let an API key make this change"
        )
    return ""


CHECKS: dict[str, Callable[[Any], str]] = {"okx": _okx, "myokx": _okx, "okxus": _okx}


def _bybit_key(client: Any, *, ip_unbound_ok: bool, now: float) -> str:
    """A trade key and nothing more: orders and positions, no wallet, bound to an IP, not expiring.

    Read through `GET /v5/user/query-api`, ccxt's `privateGetV5UserQueryApi`. A key that can move
    funds is refused even though it would trade, because the one permission a trading process never
    needs is the one that empties the account.
    """
    read = getattr(client, "privateGetV5UserQueryApi", None)
    if read is None:
        return ""
    info = (read() or {}).get("result") or {}
    permissions = info.get("permissions") or {}
    wallet = list(permissions.get("Wallet") or [])
    contract = list(permissions.get("ContractTrade") or [])
    if str(info.get("readOnly", "1")) == "1":
        return "the key is read-only; a live session needs a trade key"
    if wallet:
        return f"the key carries wallet permissions {wallet}; a trading key must carry none"
    if "Order" not in contract or "Position" not in contract:
        return f"the key needs ContractTrade Order and Position; it has {contract}"
    if not list(info.get("ips") or []) and not ip_unbound_ok:
        return (
            "the key is not bound to an IP; bind it, or set [live].key_ip_unbound_ok = true to "
            "accept that deliberately"
        )
    expires = str(info.get("expiredAt") or "")
    if expires and expires not in ("-1", "0"):
        try:
            when = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            return ""
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        days = (when.timestamp() - now) / 86400
        if days < MIN_KEY_LIFE_DAYS:
            return f"the key expires in {days:.0f} days; rotate it before a live session"
    return ""


KEY_CHECKS: dict[str, Callable[..., str]] = {"bybit": _bybit_key}


def key_refusal(
    venue: str, client: Any, *, ip_unbound_ok: bool = False, now: float | None = None
) -> str:
    """Why this key should not run a live session, or nothing when it passes or cannot be read."""
    check = KEY_CHECKS.get(venue.strip().lower())
    if check is None:
        return ""
    return check(client, ip_unbound_ok=ip_unbound_ok, now=time.time() if now is None else now)


def account_refusal(venue: str, client: Any) -> str:
    """Why this account cannot take the order as configured, or nothing."""
    check = CHECKS.get(venue.strip().lower())
    return check(client) if check is not None else ""
