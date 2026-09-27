"""Hard caps for the canary tier. Small, short, and over the moment any of them is crossed.

A canary exists to find out whether the live venue behaves the way the simulation assumed:
fills, fees, latency, reconciliation. It is not there to make money, so its caps are set by
what a defect would cost, not by what the strategy might earn. Every cap is a number the
operator wrote down before the session; none has a default the code would pick for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

NS_PER_MINUTE = 60 * 1_000_000_000


class CanaryError(ValueError):
    """A cap is missing or not a positive number."""


@dataclass(frozen=True)
class CanaryLimits:
    max_notional: Decimal
    max_round_trips: int
    session_loss_limit_bps: Decimal
    max_session_minutes: int

    def __post_init__(self) -> None:
        if not isinstance(self.max_notional, Decimal) or self.max_notional <= 0:
            raise CanaryError("max_notional must be a positive Decimal in quote currency")
        if self.max_round_trips <= 0:
            raise CanaryError("max_round_trips must be positive")
        if not isinstance(self.session_loss_limit_bps, Decimal) or self.session_loss_limit_bps <= 0:
            raise CanaryError("session_loss_limit_bps must be a positive Decimal")
        if self.max_session_minutes <= 0:
            raise CanaryError("max_session_minutes must be positive")


class CanaryGuard:
    """Consulted before every entry and on every bar. A reason means no."""

    def __init__(self, limits: CanaryLimits) -> None:
        self.limits = limits
        self._started_ns: int | None = None

    def refuse_entry(
        self, *, ts_ns: int, notional: Decimal | None, round_trips: int, net_bps: Decimal
    ) -> str | None:
        self._start(ts_ns)
        lim = self.limits
        if notional is None:
            return "canary: notional unknown before the first print; refusing"
        if notional > lim.max_notional:
            return f"canary: notional {notional} exceeds max {lim.max_notional}"
        if round_trips >= lim.max_round_trips:
            return f"canary: {round_trips} round trips reached the cap of {lim.max_round_trips}"
        if net_bps <= -lim.session_loss_limit_bps:
            return f"canary: session net {net_bps} bps at or below -{lim.session_loss_limit_bps}"
        return self.session_over(ts_ns)

    def session_over(self, ts_ns: int) -> str | None:
        elapsed = (ts_ns - self._start(ts_ns)) // NS_PER_MINUTE
        if elapsed >= self.limits.max_session_minutes:
            return f"canary: session ran {elapsed} minutes, cap {self.limits.max_session_minutes}"
        return None

    def caps_reached(self, *, round_trips: int, net_bps: Decimal) -> str | None:
        """A cap that is already met ends the session; there is nothing left to learn from it."""
        lim = self.limits
        if round_trips >= lim.max_round_trips:
            return f"canary: {round_trips} round trips reached the cap of {lim.max_round_trips}"
        if net_bps <= -lim.session_loss_limit_bps:
            return f"canary: session net {net_bps} bps at or below -{lim.session_loss_limit_bps}"
        return None

    def _start(self, ts_ns: int) -> int:
        if self._started_ns is None:
            self._started_ns = ts_ns
        return self._started_ns
