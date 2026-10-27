"""The gate's checks. Each answers one question about one intent and returns why not, or empty.

Prices and sizes are read from the intent as written: `size` in base units, `mid`, `stop_price`
and `take_price` in the quote. Nothing here reads a file, a clock or a venue.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from trading_research_agents.execution.risk import loss_per_unit
from trading_research_agents.risk.limits import RiskLimits, RiskState

BPS = Decimal(10_000)
# The precision the cycle reports risk at. A recomputed figure this close to the limit is the same
# figure rounded, not a breach.
RISK_TOLERANCE_BPS = Decimal("0.01")
SIDES = ("long", "short")

Check = Callable[[Mapping[str, Any], RiskLimits, RiskState], str]


def _dec(intent: Mapping[str, Any], key: str) -> Decimal | None:
    value = intent.get(key)
    return None if value in (None, "") else Decimal(str(value))


def switches(intent: Mapping[str, Any], limits: RiskLimits, state: RiskState) -> str:
    return f"held by {state.held_by}" if state.held_by else ""


def unknown_orders(intent: Mapping[str, Any], limits: RiskLimits, state: RiskState) -> str:
    return state.unknown_orders


def well_formed(intent: Mapping[str, Any], limits: RiskLimits, state: RiskState) -> str:
    target = str(intent.get("target", ""))
    if target not in SIDES:
        return f"target {target!r} is not an order"
    size, mid = _dec(intent, "size"), _dec(intent, "mid")
    if size is None or size <= 0:
        return "the size is not positive"
    if mid is None or mid <= 0:
        return "the mid it was priced at is not positive"
    return ""


def exits_bracket_the_entry(intent: Mapping[str, Any], limits: RiskLimits, state: RiskState) -> str:
    """A stop on the wrong side of the entry closes the trade at once, at a loss."""
    mid = _dec(intent, "mid")
    stop, take = _dec(intent, "stop_price"), _dec(intent, "take_price")
    below = intent.get("target") == "long"
    if mid is None:
        return ""
    if stop is not None and (stop >= mid if below else stop <= mid):
        return f"the stop {stop} is not on the losing side of the entry {mid}"
    if take is not None and (take <= mid if below else take >= mid):
        return f"the take profit {take} is not on the winning side of the entry {mid}"
    return ""


def order_caps(intent: Mapping[str, Any], limits: RiskLimits, state: RiskState) -> str:
    size, mid = _dec(intent, "size") or Decimal(0), _dec(intent, "mid") or Decimal(0)
    notional = size * mid
    if limits.max_notional is not None and notional > limits.max_notional:
        return f"notional {notional} is above [execution].max_notional {limits.max_notional}"
    if limits.max_position is not None and size > limits.max_position:
        return f"size {size} is above [controls].max_position {limits.max_position}"
    if limits.max_position_quote is not None and notional > limits.max_position_quote:
        cap = limits.max_position_quote
        return f"notional {notional} is above [controls].max_position_quote {cap}"
    return ""


def risk_per_trade(intent: Mapping[str, Any], limits: RiskLimits, state: RiskState) -> str:
    """The loss at the stop, round trip included, recomputed from the intent's own numbers."""
    allowed = limits.max_risk_per_trade_bps
    if allowed is None:
        return ""
    stop = _dec(intent, "stop_price")
    if stop is None:
        return "[controls].max_risk_per_trade_bps is declared and the intent has no stop"
    if state.equity is None:
        return (
            "[controls].max_risk_per_trade_bps is declared and no equity was read to check it"
            if state.places_orders
            else ""
        )
    size, mid = _dec(intent, "size") or Decimal(0), _dec(intent, "mid") or Decimal(0)
    cost = max(_dec(intent, "priced_round_trip_bps") or Decimal(0), Decimal(0))
    at_risk = loss_per_unit(entry=mid, stop=stop, cost_bps=cost) * size / state.equity * BPS
    if at_risk > allowed + RISK_TOLERANCE_BPS:
        return (
            f"{at_risk.quantize(Decimal('0.01'))} bps of equity at the stop is above "
            f"[controls].max_risk_per_trade_bps {allowed}"
        )
    return ""


def trades_today(intent: Mapping[str, Any], limits: RiskLimits, state: RiskState) -> str:
    cap = limits.max_trades_per_day
    if cap is None or state.trades_today < cap:
        return ""
    return f"{state.trades_today} trade(s) today; [controls].max_trades_per_day is {cap}"


def day_loss(intent: Mapping[str, Any], limits: RiskLimits, state: RiskState) -> str:
    cap = limits.daily_loss_limit_bps
    if cap is None:
        return ""
    if state.day_loss_bps is None:
        return "[controls].daily_loss_limit_bps is declared and the day loss was not read"
    if state.day_loss_bps >= cap:
        return f"down {state.day_loss_bps} bps today; [controls].daily_loss_limit_bps is {cap}"
    return ""


def drawdown(intent: Mapping[str, Any], limits: RiskLimits, state: RiskState) -> str:
    cap = limits.max_drawdown_bps
    if cap is None:
        return ""
    if state.drawdown_bps is None:
        return "[controls].max_drawdown_bps is declared and the drawdown was not read"
    if state.drawdown_bps >= cap:
        return f"a drawdown of {state.drawdown_bps} bps; [controls].max_drawdown_bps is {cap}"
    return ""


CHECKS: tuple[tuple[str, Check], ...] = (
    ("switches", switches),
    ("unknown orders", unknown_orders),
    ("well formed", well_formed),
    ("exits bracket the entry", exits_bracket_the_entry),
    ("order caps", order_caps),
    ("risk per trade", risk_per_trade),
    ("trades today", trades_today),
    ("day loss", day_loss),
    ("drawdown", drawdown),
)
