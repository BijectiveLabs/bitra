"""Capital controls as code. Independent of the strategy, and they fail closed.

The per-strategy tier of the circuit-breaker hierarchy: maximum risk per trade, maximum
position, maximum order rate, a daily loss limit, and a kill switch. A control that cannot be
evaluated refuses the order; equity that is unknown is not equity that is fine. The rate limit
lives here rather than in the strategy because a strategy bug that produces an order storm is
exactly the case the limiter exists for.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from trading_research_agents.strategy.core import Target

NS_PER_MINUTE = 60 * 1_000_000_000
NS_PER_DAY = 86_400 * 1_000_000_000


class ControlError(ValueError):
    """The limits themselves are not usable."""


DRAWDOWN_ACTIONS = ("stop", "flatten")


@dataclass(frozen=True)
class Limits:
    max_risk_per_trade_bps: Decimal = Decimal(50)
    # None: no cap in units; the risk per trade and the order cap still bound every size.
    max_position: Decimal | None = None
    max_orders_per_minute: int = 6
    daily_loss_limit_bps: Decimal = Decimal(300)
    # The same cap said in quote currency, for a desk whose instruments are priced too differently
    # for one count of units: converted at each cycle's own mid.
    max_position_quote: Decimal | None = None
    # From the highest equity this desk has seen, across days. The daily limit starts again each
    # morning; this one does not, so a week of daily losses cannot pass unnoticed.
    max_drawdown_bps: Decimal | None = None
    on_drawdown: str = "stop"

    def __post_init__(self) -> None:
        for label, value in (
            ("max_risk_per_trade_bps", self.max_risk_per_trade_bps),
            ("max_position", self.max_position),
            ("daily_loss_limit_bps", self.daily_loss_limit_bps),
            ("max_drawdown_bps", self.max_drawdown_bps),
        ):
            if value is None and label in ("max_position", "max_drawdown_bps"):
                continue
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ControlError(f"{label} must be a positive finite Decimal")
        if self.on_drawdown not in DRAWDOWN_ACTIONS:
            raise ControlError(f"on_drawdown is one of {list(DRAWDOWN_ACTIONS)}")
        if self.max_position_quote is not None and (
            not self.max_position_quote.is_finite() or self.max_position_quote <= 0
        ):
            raise ControlError("max_position_quote must be a positive finite Decimal")
        if self.max_orders_per_minute <= 0:
            raise ControlError("max_orders_per_minute must be positive")


class KillSwitch:
    """Armed by a file or by code. Once armed it stays armed for the life of the process."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._armed = False

    def arm(self) -> None:
        self._armed = True

    @property
    def armed(self) -> bool:
        if self._armed:
            return True
        if self.path is not None and self.path.exists():
            self._armed = True
        return self._armed


@dataclass(frozen=True)
class Admission:
    admitted: bool
    reason: str


@dataclass
class Controls:
    limits: Limits
    kill_switch: KillSwitch
    _orders: deque[int] = field(default_factory=deque)
    _day_start_ns: int | None = None
    _day_pnl_bps: Decimal = Decimal(0)
    _last_ts_ns: int | None = None
    _tripped: str | None = None

    def record_pnl(self, ts_ns: int, net_bps: Decimal) -> None:
        self._roll_day(ts_ns)
        self._day_pnl_bps += net_bps
        if self._day_pnl_bps <= -self.limits.daily_loss_limit_bps:
            self._tripped = (
                f"daily loss limit: {self._day_pnl_bps} bps against "
                f"-{self.limits.daily_loss_limit_bps} bps"
            )

    def admit(
        self,
        *,
        ts_ns: int,
        target: Target,
        size: Decimal,
        risk_bps: Decimal | None,
        equity_known: bool,
    ) -> Admission:
        """Decide one order. Every path that is not a clear yes is a no with a reason."""
        if target is Target.FLAT:
            # Reducing to flat is always allowed, kill switch included: a control that blocks
            # the exit is a trap, and the kill switch exists to get flat.
            return Admission(True, "flatten")
        if self.kill_switch.armed:
            return Admission(False, "kill switch armed")
        if self._tripped is not None:
            return Admission(False, self._tripped)
        if self._last_ts_ns is not None and ts_ns < self._last_ts_ns:
            return Admission(False, "clock went backwards; refusing to evaluate limits")
        self._last_ts_ns = ts_ns
        self._roll_day(ts_ns)
        if not equity_known:
            return Admission(False, "equity unknown; a limit that cannot be evaluated refuses")
        if risk_bps is None:
            return Admission(False, "risk per trade not computable; refusing")
        if risk_bps > self.limits.max_risk_per_trade_bps:
            return Admission(
                False,
                f"risk {risk_bps} bps exceeds max {self.limits.max_risk_per_trade_bps} bps",
            )
        if self.limits.max_position is not None and size > self.limits.max_position:
            return Admission(False, f"size {size} exceeds max position {self.limits.max_position}")
        while self._orders and ts_ns - self._orders[0] >= NS_PER_MINUTE:
            self._orders.popleft()
        if len(self._orders) >= self.limits.max_orders_per_minute:
            return Admission(
                False, f"order rate above {self.limits.max_orders_per_minute} per minute"
            )
        self._orders.append(ts_ns)
        return Admission(True, "within limits")

    @property
    def tripped(self) -> str | None:
        return self._tripped

    def _roll_day(self, ts_ns: int) -> None:
        if self._day_start_ns is None or ts_ns - self._day_start_ns >= NS_PER_DAY:
            self._day_start_ns = ts_ns - (ts_ns % NS_PER_DAY)
            self._day_pnl_bps = Decimal(0)
