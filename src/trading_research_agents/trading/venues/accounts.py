"""Account settings a venue has that ccxt does not unify, checked before the first order.

ccxt sends the same order to every venue, but some accounts refuse it because of a setting on the
account itself. Each check here reads that setting through the client the order would use and
says, in the venue's own words, what to change. A check that cannot read the setting says nothing:
not knowing is not a reason to refuse, and the venue still refuses at its end if it must.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


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


def account_refusal(venue: str, client: Any) -> str:
    """Why this account cannot take the order as configured, or nothing."""
    check = CHECKS.get(venue.strip().lower())
    return check(client) if check is not None else ""
