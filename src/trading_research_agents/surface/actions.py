"""The actions a role may take. Every one of them makes the session safer, never looser.

A risk manager may tighten a limit, step the ladder down, or arm the kill switch. It may not
widen a limit, promote, or disarm anything; a request to do so is refused here, by code, and
the refusal is what the role is told. Sending an order is not an action on this surface: it
goes through the runtime, which admits or refuses it through the same controls the CLI uses.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trading_research_agents.trading.controls import KillSwitch, Limits
from trading_research_agents.trading.ladder import Demotion, Ladder, Tier


class ActionRefusedError(ValueError):
    """The action would loosen a control, or is not one a role may take."""


TIGHTENABLE = (
    "max_risk_per_trade_bps",
    "max_position",
    "max_orders_per_minute",
    "daily_loss_limit_bps",
)


@dataclass(frozen=True)
class Tightened:
    before: Limits
    after: Limits
    changed: tuple[str, ...]


def tighten_limits(current: Limits, proposed: dict[str, Any]) -> Tightened:
    """A new `Limits` with each proposed figure applied only when it is smaller."""
    unknown = sorted(set(proposed) - set(TIGHTENABLE))
    if unknown:
        raise ActionRefusedError(f"not a limit: {', '.join(unknown)}")
    values: dict[str, Any] = {}
    changed: list[str] = []
    for key in TIGHTENABLE:
        if key not in proposed:
            continue
        have = getattr(current, key)
        want = proposed[key]
        if key == "max_orders_per_minute":
            if isinstance(want, bool) or not isinstance(want, int):
                raise ActionRefusedError("max_orders_per_minute is an integer")
            new: Any = want
        else:
            try:
                new = Decimal(str(want))
            except InvalidOperation:
                raise ActionRefusedError(f"{key} is not a number: {want!r}") from None
            if not new.is_finite():
                raise ActionRefusedError(f"{key} must be finite")
        if new <= 0:
            raise ActionRefusedError(
                f"{key} must stay positive; a zero limit is a halt, not a limit"
            )
        if have is not None and new > have:
            raise ActionRefusedError(
                f"{key}: {new} is larger than the {have} in force; only tighter is allowed"
            )
        if new != have:
            values[key] = new
            changed.append(key)
    after = replace(current, **values) if values else current
    return Tightened(before=current, after=after, changed=tuple(changed))


def demote(ladder: Ladder, *, reason: str, to: Tier | None = None) -> Demotion:
    """Down, on a reason. The ladder itself refuses anything that is not down."""
    if not reason.strip():
        raise ActionRefusedError("a demotion states its reason")
    return ladder.demote(reason=reason, to=to)


def arm_kill_switch(switch: KillSwitch, *, reason: str, touch: Path | None = None) -> str:
    """Arm it in process and, when the run declares a file, on disk for other processes."""
    if not reason.strip():
        raise ActionRefusedError("arming the kill switch states its reason")
    switch.arm()
    if touch is not None:
        touch.parent.mkdir(parents=True, exist_ok=True)
        touch.write_text(reason.strip() + "\n", encoding="utf-8")
    return reason.strip()


FORBIDDEN = ("promote", "loosen_limits", "disarm_kill_switch", "raise_cap", "place_order")


def refuse(action: str) -> None:
    """Named so that a role asking for it gets the same answer every time."""
    raise ActionRefusedError(
        f"{action}: not an action a role may take; it needs an operator and a signature"
    )
