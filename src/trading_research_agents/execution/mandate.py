"""What this software is allowed to do with an order, and who said so.

Three modes, and the distance between them is deliberate.

    sandbox   the intent is written into the cycle's own directory. Nothing leaves the machine.
    signals   the intent is written for execution somewhere else, by someone else's system.
    venue     the intent is sent to a venue, with real capital behind it.

The first two need nothing. The third needs a mandate: the operator has read what they are taking
on, copied it into their own project, and signed it with a name and a date. That is not a checkbox
in a config file, and it is not something this package can do on anyone's behalf - the text has to
be in a file the operator put there, word for word, or venue mode does not start.

Everything else the third mode needs is already in the project and already refuses on its own: the
ladder at its canary tier on mainnet, the keys in the environment rather than in a file, and every
gate in the cycle passed before there is an intent at all.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

MODES = ("sandbox", "signals", "venue")
MANDATE_FILE = "MANDATE.md"
LIVE_TIER = "CANARY"

DISCLAIMER = """# Execution mandate

I am the operator of this deployment.

I chose the venue, I created the key, I wrote the caps, I promoted the ladder, and I decide when
this runs. The software's job is to make each of those choices explicit, recorded and hard to make
by accident. It is not to decide any of them for me.

I understand that in venue mode this software sends orders to a venue with my capital behind them,
that an order that has been sent cannot be recalled by this software, and that a defect, a network
failure or a venue outage can cost money while it is running.

I understand that this software is published under the MIT License, without warranty of any kind,
and that no verdict, score or refusal it produces is advice.

I accept that the losses are mine.
"""


MARGIN_MODES = ("isolated", "cross")
HOLD_LIMITS = ("timeout", "horizon", "none")


class MandateError(ValueError):
    """The mandate as written does not authorise what was asked for."""


def digest(text: str = DISCLAIMER) -> str:
    """A stable fingerprint of the text, with line endings normalised so Windows is not special."""
    return hashlib.sha256("\n".join(text.splitlines()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Mandate:
    """[execution]: the mode, and the acknowledgement that venue mode needs."""

    mode: str = "sandbox"
    venue: str = ""
    acknowledged_by: str = ""
    acknowledged_on: str = ""
    max_notional: Decimal | None = None
    max_orders_per_cycle: int = 1
    # A resting order is only worth resting for so long, and how far off the touch it sits is the
    # operator's call. `params` is handed to the venue verbatim, the way data.toml already does.
    exits: str = "attached"
    limit_timeout_s: float = 30.0
    price_offset_bps: Decimal = Decimal(0)
    adopt_open_position: bool = False
    params: Mapping[str, Any] = field(default_factory=dict)
    # How far the mid may have moved against the trade between the snapshot it was priced on and
    # the moment it is sent. Undeclared means no bound, and the move is still recorded.
    max_drift_bps: Decimal | None = None
    # How old a decision may be, from the start of its cycle to the moment of sending. The drift
    # check above catches a market that moved; this catches one that did not move but whose figures
    # are no longer the ones the decision was made on.
    max_decision_age_s: float = 120.0
    # The capital side of an order. Leverage is the operator's to declare and code's to apply and
    # verify; it never decides the size, only the margin a size ties up and where liquidation sits.
    leverage: Decimal | None = None
    margin_mode: str = "isolated"
    # A taker entry goes out as a limit IOC this far through the touch, never as a naked market
    # order. Exits are never price-limited: a close that fails is worse than one that slips.
    max_slippage_bps: Decimal = Decimal(20)
    # At most this share of the free margin goes into any one order.
    max_margin_share: Decimal = Decimal("0.5")
    # Liquidation, estimated conservatively, must sit at least this far beyond the stop.
    liquidation_buffer_bps: Decimal = Decimal(50)
    # When an open position is closed by time: [exit].timeout_bars, the analyst's horizon, or never.
    hold_limit: str = "timeout"
    # Where orders go: demo, testnet or mainnet. Empty falls back to [live].environment, then demo.
    environment: str = ""

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise MandateError(f"[execution].mode is one of {list(MODES)}")
        if self.max_notional is not None and self.max_notional <= 0:
            raise MandateError("[execution].max_notional is positive")
        if self.max_orders_per_cycle != 1:
            # One decision, at most one order, is a guarantee, and the venue hand holds to it
            # whatever is written here; a value that suggests otherwise would be a promise unkept.
            raise MandateError(
                "[execution].max_orders_per_cycle is 1: one decision sends at most one order"
            )
        if self.exits not in ("attached", "none"):
            raise MandateError('[execution].exits is "attached" or "none"')
        if self.limit_timeout_s <= 0:
            raise MandateError("[execution].limit_timeout_s is positive")
        if self.price_offset_bps < 0:
            raise MandateError("[execution].price_offset_bps is not negative")
        if self.max_drift_bps is not None and self.max_drift_bps < 0:
            raise MandateError("[execution].max_drift_bps is not negative")
        if self.max_decision_age_s <= 0:
            raise MandateError("[execution].max_decision_age_s is positive")
        if self.leverage is not None and self.leverage < 1:
            raise MandateError("[execution].leverage is at least 1")
        if self.margin_mode not in MARGIN_MODES:
            raise MandateError(f"[execution].margin_mode is one of {list(MARGIN_MODES)}")
        if self.max_slippage_bps <= 0:
            raise MandateError("[execution].max_slippage_bps is positive")
        if not Decimal(0) < self.max_margin_share <= Decimal(1):
            raise MandateError("[execution].max_margin_share is above 0 and at most 1")
        if self.liquidation_buffer_bps < 0:
            raise MandateError("[execution].liquidation_buffer_bps is not negative")
        if self.hold_limit not in HOLD_LIMITS:
            raise MandateError(f"[execution].hold_limit is one of {list(HOLD_LIMITS)}")
        if self.environment not in ("", "demo", "testnet", "mainnet"):
            raise MandateError('[execution].environment is "demo", "testnet" or "mainnet"')
        if self.acknowledged_on:
            try:
                signed = date.fromisoformat(self.acknowledged_on)
            except ValueError as exc:
                raise MandateError("[execution].acknowledged_on is a date, YYYY-MM-DD") from exc
            if signed > date.today():
                raise MandateError("[execution].acknowledged_on is not in the future")

    @property
    def places_orders(self) -> bool:
        return self.mode == "venue"

    def record(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "venue": self.venue,
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_on": self.acknowledged_on,
            "max_notional": None if self.max_notional is None else str(self.max_notional),
            "max_orders_per_cycle": self.max_orders_per_cycle,
            "exits": self.exits,
            "limit_timeout_s": self.limit_timeout_s,
            "price_offset_bps": str(self.price_offset_bps),
            "max_drift_bps": None if self.max_drift_bps is None else str(self.max_drift_bps),
            "max_decision_age_s": self.max_decision_age_s,
            "leverage": None if self.leverage is None else str(self.leverage),
            "margin_mode": self.margin_mode,
            "max_slippage_bps": str(self.max_slippage_bps),
            "max_margin_share": str(self.max_margin_share),
            "liquidation_buffer_bps": str(self.liquidation_buffer_bps),
            "hold_limit": self.hold_limit,
            "adopt_open_position": self.adopt_open_position,
            "environment": self.environment,
            "params": {str(k): str(v) for k, v in sorted(self.params.items())},
        }


def signed_in(project: Path) -> bool:
    """Whether the project carries the mandate text, word for word, in its own file."""
    where = project / MANDATE_FILE
    if not where.is_file():
        return False
    try:
        text = where.read_text(encoding="utf-8")
    except OSError:
        return False
    return "\n".join(DISCLAIMER.splitlines()) in "\n".join(text.splitlines())


def blocking(
    mandate: Mandate,
    *,
    project: Path,
    tier: str,
    missing_keys: Sequence[str] = (),
    practice: bool = False,
) -> tuple[str, ...]:
    """Everything standing between this mandate and a live order, each named in full.

    An empty tuple means venue mode may run. Every other mode returns an empty tuple as well,
    because nothing here stands between a sandbox intent and a file.
    """
    if not mandate.places_orders:
        return ()
    stops: list[str] = []
    if not signed_in(project):
        stops.append(
            f"{MANDATE_FILE} is not in the project, or does not carry the mandate text word for "
            f"word: write it with `tra mandate > {MANDATE_FILE}` and read it before you do"
        )
    if not mandate.acknowledged_by.strip():
        stops.append("[execution].acknowledged_by names the person taking this on")
    if not mandate.acknowledged_on.strip():
        stops.append("[execution].acknowledged_on is the date they signed it")
    if not mandate.venue.strip():
        stops.append("[execution].venue names the live venue plugin to send through")
    if mandate.max_notional is None:
        stops.append("[execution].max_notional caps one order; venue mode will not run without it")
    if mandate.leverage is None:
        stops.append(
            "[execution].leverage is yours to declare (3 is a common start); it is applied at the "
            "venue before the first order and checked against the stop on every order"
        )
    if tier != LIVE_TIER and not practice:
        # A demo or testnet account holds no capital, so it is where the evidence is earned.
        stops.append(
            f"the ladder is at {tier}; venue mode on mainnet needs {LIVE_TIER}, promoted with "
            "evidence; a demo or testnet account does not"
        )
    if missing_keys:
        stops.append(
            f"missing {', '.join(missing_keys)}: not in the shell and not in the .env beside "
            "strategy.toml, the only two places keys are read from"
        )
    return tuple(stops)
