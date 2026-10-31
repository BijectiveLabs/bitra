"""Every ccxt client this package builds, from one place, whatever namespace the venue lives in.

ccxt keeps crypto exchanges in `ccxt` and prediction venues in `ccxt.prediction`. The prediction
classes are async only. Everything above this module is written against the synchronous client,
so a prediction venue is handed over inside `SyncBridge`, which runs its coroutines on a loop of
its own and returns their results. The rest of the package sees one kind of client.

A venue name resolves in `ccxt` first and then in `ccxt.prediction`. Two names live in both
(`binance`, `hyperliquid`); their prediction side is named `prediction.binance` and
`prediction.hyperliquid`.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import inspect
import threading
from collections.abc import Mapping
from importlib import import_module
from typing import Any

PREDICTION = "prediction."
# Set on every client that signs with the operator's keys, under [execution].ccxt_options. ccxt's
# Hyperliquid class otherwise approves a builder fee for ccxt's own address and charges it on every
# order, on top of the venue's fee and outside the round trip the gates price. Switched off, ccxt
# still attaches its builder at zero, which costs nothing.
SIGNED_DEFAULTS: dict[str, Any] = {"builderFee": False}
# Long enough for a venue's slowest documented call; a hung socket must not hang the desk.
CALL_TIMEOUT_S = 60.0


def _namespace(name: str) -> Any:
    try:
        return import_module(name)
    except ImportError:
        return None


def exchange_class(venue: str) -> Any:
    """The ccxt class for `venue`, from either namespace, or None when ccxt has no such venue."""
    name = venue.strip().lower()
    if name.startswith(PREDICTION):
        prediction = _namespace("ccxt.prediction")
        return getattr(prediction, name[len(PREDICTION) :], None) if prediction else None
    ccxt = _namespace("ccxt")
    if ccxt is not None and name in getattr(ccxt, "exchanges", ()):
        return getattr(ccxt, name, None)
    prediction = _namespace("ccxt.prediction")
    if prediction is not None and name in getattr(prediction, "exchanges", ()):
        return getattr(prediction, name, None)
    return None


def is_prediction(venue: str) -> bool:
    found = exchange_class(venue)
    return found is not None and found.__module__.startswith("ccxt.prediction")


def venues() -> tuple[str, ...]:
    """Every venue name this package can build a client for."""
    names: list[str] = []
    ccxt = _namespace("ccxt")
    if ccxt is not None:
        names += [str(n) for n in getattr(ccxt, "exchanges", ())]
    prediction = _namespace("ccxt.prediction")
    if prediction is not None:
        for n in getattr(prediction, "exchanges", ()):
            names.append(str(n) if str(n) not in names else f"{PREDICTION}{n}")
    return tuple(names)


def market_of(client: Any, symbol: str) -> dict[str, Any]:
    """ccxt's description of `symbol`: the market itself, or, for a prediction outcome, which is
    addressed by its handle rather than a market symbol, its market with the outcome beside it.
    Empty when the client describes neither."""
    try:
        found = client.market(symbol)
    except Exception:
        found = None
    if found:
        return dict(found)
    lookup = getattr(client, "outcome", None)
    if lookup is None:
        return {}
    try:
        outcome = dict(lookup(symbol))
    except Exception:
        return {}
    for market in (getattr(client, "markets", None) or {}).values():
        handles = {
            str(o.get(key) or "")
            for o in market.get("outcomes") or []
            for key in ("outcome", "outcomeId")
        }
        if symbol in handles:
            return {**market, "symbol": symbol, "outcome": outcome}
    return {}


# The two classes ccxt raises when no answer arrived. Read in ccxt 4.5.85 `Exchange.fetch` on
# 2026-10-31: a requests Timeout, or a connection error that says "Read timed out", becomes
# RequestTimeout; a dropped or reset connection becomes NetworkError itself. Its subclasses
# (RateLimitExceeded, DDoSProtection, ExchangeNotAvailable, OnMaintenance, InvalidNonce) are the
# venue answering, and are not in this set.
UNANSWERED = ("RequestTimeout", "NetworkError")


def unanswered(exc: BaseException) -> bool:
    """Whether no answer arrived at all, as opposed to an answer the venue gave."""
    return type(exc).__name__ in UNANSWERED or isinstance(exc, TimeoutError | ConnectionError)


def fresh_connection(client: Any) -> None:
    """Drop the client's pooled connections, so the next request opens a new one.

    A connection left idle past the venue's keep-alive can hang until the client's timeout.
    Closing a requests Session closes its pools and leaves the Session usable.
    """
    close = getattr(getattr(client, "session", None), "close", None)
    if callable(close):
        close()


def build(venue: str, config: Mapping[str, Any] | None = None) -> Any:
    """A synchronous client for `venue`; None when ccxt has no such venue."""
    factory = exchange_class(venue)
    if factory is None:
        return None
    client = factory(dict(config or {}))
    if inspect.iscoroutinefunction(getattr(client, "load_markets", None)):
        return SyncBridge(client)
    return client


class SyncBridge:
    """An async ccxt client behind a synchronous face: each coroutine runs on a private loop.

    Attributes that are not coroutines (markets, has, options, the outcome cache) are the client's
    own and pass straight through, so a read of `bridge.markets` is a read of the client's dict.
    """

    def __init__(self, client: Any, *, timeout_s: float = CALL_TIMEOUT_S) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_timeout_s", timeout_s)
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        object.__setattr__(self, "_loop", loop)
        object.__setattr__(self, "_closed", False)
        # The async client holds open sockets that only its own close() releases.
        atexit.register(self._close_quietly)

    def _run(self, call: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(call, self._loop)
        return future.result(self._timeout_s)

    def __getattr__(self, name: str) -> Any:
        found = getattr(self._client, name)
        if inspect.iscoroutinefunction(found):

            def sync(*args: Any, **kwargs: Any) -> Any:
                return self._run(found(*args, **kwargs))

            return sync
        return found

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._client, name, value)

    def close(self) -> None:
        """Close the client's sessions, then stop its loop."""
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        try:
            self._run(self._client.close())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _close_quietly(self) -> None:
        # At exit there is nobody left to tell.
        with contextlib.suppress(Exception):
            self.close()
