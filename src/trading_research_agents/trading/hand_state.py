"""The order hand's terms and state: the venue constants, the errors, the order rate, the
credentials a venue signs with, and the fields every part of the hand reads."""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from trading_research_agents.data.sources.ccxt_client import (
    exchange_class,
)
from trading_research_agents.execution.mandate import DEFAULT_MARKETS

SIDES = {"long": "buy", "short": "sell"}


# Read from ccxt 4.5 on 2026-09-29: a venue reaches its demo environment through
# `enable_demo_trading` and its testnet through `set_sandbox_mode`, and the library refuses to have
# both at once. Mainnet is the client's own default and is never switched into.
ENVIRONMENTS = ("demo", "testnet", "mainnet")


ROLES = ("maker", "taker")


EXITS = ("attached", "separate", "none")


# A venue that does not describe its markets through ccxt is let through: the size, margin and
# exit checks that follow still read the account, and refuse on what they find there.
UNDESCRIBED = "unknown"


# Order states in which the venue has finished with an order without it being fully filled.
ENDED = ("canceled", "cancelled", "rejected", "expired")


BPS = Decimal(10_000)


# The same bound a live session holds itself to. ccxt signs with a wider receive window, so a clock
# past this still works until the day it drifts a little further, mid-order.
MAX_CLOCK_SKEW_MS = 1000


# A venue's book stamped older than this is not the market an order is about to meet. Seen on
# 2026-10-20: a demo API served a book 3.2 hours old, 35 bps from where its own trades printed.
MAX_BOOK_AGE_S = 30.0


# ccxt's credential fields, and the suffix of the environment variable each is read from. Most
# venues want a key and a secret; some add a passphrase, and some sign with a wallet instead.
CREDENTIAL_ENV = {
    "apiKey": "API_KEY",
    "secret": "API_SECRET",
    "password": "API_PASSPHRASE",
    "uid": "API_UID",
    "walletAddress": "WALLET_ADDRESS",
    "privateKey": "PRIVATE_KEY",
    # Some venues sign for an account id beside the key and secret.
    "accountId": "ACCOUNT_ID",
}


PLAIN_PAIR = ("apiKey", "secret")


class DispatchError(ValueError):
    """The order cannot be sent as configured, or the venue refused it."""


@dataclass
class OrderRate:
    """The orders the desk sent in the last minute, against [controls].max_orders_per_minute."""

    per_minute: int
    sent: list[float] = field(default_factory=list)

    def admit(self, now: float) -> str:
        self.sent = [at for at in self.sent if now - at < 60]
        if len(self.sent) >= self.per_minute:
            return (
                f"{len(self.sent)} orders in the last minute and [controls].max_orders_per_minute "
                f"is {self.per_minute}"
            )
        self.sent.append(now)
        return ""


def _namespace_missing() -> bool:
    try:
        import ccxt  # noqa: F401
    except ImportError:
        return True
    return False


def credentials_for(venue: str, client: Any = None) -> dict[str, str]:
    """What this venue signs with, as ccxt field -> environment variable.

    Read from ccxt's own `requiredCredentials`, so a venue that needs a passphrase is asked for one
    by name before anything is sent rather than refused at the venue with a signature error.
    """
    prefix = venue.strip().upper()
    if not prefix:
        return {}
    required: dict[str, Any] = {}
    if client is None:
        factory = exchange_class(venue)
        client = factory() if factory is not None else None
    if client is not None:
        required = dict(getattr(client, "requiredCredentials", None) or {})
    fields = [name for name in CREDENTIAL_ENV if required.get(name)] or list(PLAIN_PAIR)
    return {name: f"{prefix}_{CREDENTIAL_ENV[name]}" for name in fields}


def key_hint(missing: Sequence[str], expected: Sequence[str], env: Mapping[str, str]) -> str:
    """Names set beside the missing ones under the same venue prefix, by name and never by value.

    Seen on 2026-10-20: an exchange's own page calls the secret a "secret key", and a `.env` held
    GATE_SECRET_KEY where GATE_API_SECRET is read.
    """
    prefixes = tuple({name.split("_", 1)[0] + "_" for name in missing})
    near = sorted(n for n in env if n.startswith(prefixes) and n not in expected and env.get(n))
    if not near:
        return ""
    return f"{', '.join(near)} is set; the names read are {', '.join(expected)}"


def leverage_number(leverage: Decimal) -> int | float:
    """A whole leverage as an integer: one venue refused 3.0 as malformed (2026-10-20)."""
    whole = leverage.to_integral_value()
    return int(whole) if leverage == whole else float(leverage)


def _drift_bps(
    priced: Decimal, bid: Decimal | None, ask: Decimal | None, side: str
) -> Decimal | None:
    """How far the mid moved against the trade since it was priced; negative is in its favour."""
    if not priced or bid is None or ask is None:
        return None
    now = (bid + ask) / 2
    moved = (now - priced) / priced * BPS
    return moved if side == "buy" else -moved


def _decision_age_s(intent: Mapping[str, Any], now: float) -> float | None:
    """Seconds since the cycle that made this decision began, read from its id."""
    from datetime import UTC, datetime

    stamp = str(intent.get("at", ""))[:20]
    try:
        began = datetime.strptime(stamp, "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return now - began.timestamp()


def _slippage_bps(average: Any, mid: Decimal, side: str) -> str | None:
    """What the fill paid against the mid the trade was priced at; positive is worse."""
    if average in (None, "") or mid <= 0:
        return None
    try:
        paid = Decimal(str(average))
    except ArithmeticError:
        return None
    moved = (paid - mid) / mid * BPS
    return str((moved if side == "buy" else -moved).quantize(Decimal("0.01")))


def _client_id(intent: Mapping[str, Any], longest: int = 32, prefix: str = "") -> str:
    """One id per cycle and instrument, so a cycle run twice cannot open two positions.

    Letters and digits only, 32 at most, behind a prefix the venue requires. A venue that takes
    fewer gets a digest of the same id, which is just as fixed for the cycle and cannot lose the
    instrument the way a cut would.
    """
    cycle = re.sub(r"[^0-9A-Za-z]", "", str(intent.get("at", "")) or "unknown")
    instrument = re.sub(r"[^0-9A-Za-z]", "", str(intent.get("instrument", "")))
    body = cycle if instrument and cycle.endswith(instrument) else cycle + instrument
    full = f"{prefix}tra{body}"
    if len(full) <= longest:
        return full
    if longest >= 32 and not prefix:
        return full[:32]
    digest = hashlib.sha256(full.encode("ascii")).hexdigest()
    return f"{prefix}tra{digest[: longest - len(prefix) - 3]}"


@dataclass
class HandState:
    """The fields of a venue hand. Its behaviour is layered on top: terms, reads, upkeep,
    then the sending itself in `dispatch.VenueHand`."""

    venue: str
    symbol: str
    max_notional: Decimal
    # ccxt field -> environment variable. A plain (key, secret) pair is accepted and read as such.
    keys: Mapping[str, str] | tuple[str, ...]
    # The instrument this hand's symbol stands for. A hand is built for one instrument, and an
    # intent about another one is refused rather than sent under this symbol.
    instrument: str = ""
    role: str = "taker"
    exits: str = "attached"
    environment: str = "demo"
    limit_timeout_s: float = 30.0
    price_offset_bps: Decimal = Decimal(0)
    adopt_open_position: bool = False
    params: Mapping[str, Any] = field(default_factory=dict)
    max_drift_bps: Decimal | None = None
    max_decision_age_s: float = 120.0
    leverage: Decimal | None = None
    margin_mode: str = "isolated"
    max_slippage_bps: Decimal = Decimal(20)
    max_margin_share: Decimal = Decimal("0.5")
    liquidation_buffer_bps: Decimal = Decimal(50)
    markets: tuple[str, ...] = DEFAULT_MARKETS
    # Shared by every hand in a run: [controls].max_orders_per_minute is the desk's, not a hand's.
    rate: OrderRate | None = None
    client: Any = None
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    options: Mapping[str, Any] = field(default_factory=dict)
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    skew_checked: bool = False
    account_checked: bool = False
    leverage_set: bool = False
    # Set when the venue applied the leverage but ccxt has no way to read it back.
    leverage_by_reply: bool = False
    sent_count: int = 0
    book_age_s: float | None = None
