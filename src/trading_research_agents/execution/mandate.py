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
ladder at its canary tier, the canary limits written down, the keys in the environment rather than
in a file, and every gate in the cycle passed before there is an intent at all.
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

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise MandateError(f"[execution].mode is one of {list(MODES)}")
        if self.max_notional is not None and self.max_notional <= 0:
            raise MandateError("[execution].max_notional is positive")
        if self.max_orders_per_cycle < 1:
            raise MandateError("[execution].max_orders_per_cycle is at least 1")
        if self.exits not in ("attached", "none"):
            raise MandateError('[execution].exits is "attached" or "none"')
        if self.limit_timeout_s <= 0:
            raise MandateError("[execution].limit_timeout_s is positive")
        if self.price_offset_bps < 0:
            raise MandateError("[execution].price_offset_bps is not negative")
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
            "adopt_open_position": self.adopt_open_position,
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
    canary_declared: bool = False,
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
    if tier != LIVE_TIER:
        stops.append(
            f"the ladder is at {tier}; venue mode needs {LIVE_TIER}, promoted with evidence"
        )
    if not canary_declared:
        stops.append("[canary] limits are not declared; a live session has no defaults")
    if missing_keys:
        stops.append(
            f"missing {', '.join(missing_keys)} in the environment; keys never come from a file"
        )
    return tuple(stops)


def require(mandate: Mandate, **checks: Any) -> None:
    """The same test, as a refusal. Used where a caller cannot carry on without the mandate."""
    stops = blocking(mandate, **checks)
    if stops:
        raise MandateError("; ".join(stops))
