"""Where a desk trades: the execution mandate, the live venue, the canary caps, funding, and the
exits and controls it holds positions to.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.execution.cost import CostError, FundingSchedule
from trading_research_agents.execution.mandate import DEFAULT_MARKETS, Mandate, MandateError
from trading_research_agents.execution.stops import MODES, StopError, VolatilityExit
from trading_research_agents.trading.canary import CanaryError, CanaryLimits
from trading_research_agents.trading.config_values import (
    SECRET_LIKE,
    ConfigError,
    _dec,
    _flag,
    _int,
    _no_secrets,
    _or_dec,
    _or_int,
)
from trading_research_agents.trading.controls import ControlError, Limits


@dataclass(frozen=True)
class LiveSettings:
    """[live]: which plugin, which environment, and the two deliberate overrides."""

    venue: str
    environment: str
    key_ip_unbound_ok: bool = False
    adopt_open_position: bool = False
    poll_interval_ms: int = 250


EXECUTION_KEYS = (
    "mode",
    "venue",
    "acknowledged_by",
    "acknowledged_on",
    "max_notional",
    "max_orders_per_cycle",
    "exits",
    "limit_timeout_s",
    "price_offset_bps",
    "max_drift_bps",
    "max_decision_age_s",
    "leverage",
    "margin_mode",
    "max_slippage_bps",
    "max_margin_share",
    "liquidation_buffer_bps",
    "hold_limit",
    "adopt_open_position",
    "params",
    "ccxt_options",
    "environment",
    "markets",
    "equity_session",
    "equity_closed",
    "equities",
)


def _mandate(raw: dict[str, Any], venue_key: str) -> Mandate:
    """[execution]: the mode, and the acknowledgement venue mode needs before it starts."""
    table = raw.get("execution")
    if table is None:
        return Mandate()
    if not isinstance(table, dict):
        raise ConfigError("[execution] must be a table")
    unknown = sorted(set(table) - set(EXECUTION_KEYS))
    _no_secrets(unknown, "[execution]")
    if unknown:
        raise ConfigError(f"[execution]: unknown {unknown}; have {list(EXECUTION_KEYS)}")
    try:
        return Mandate(
            mode=str(table.get("mode", "sandbox")),
            venue=str(table.get("venue") or venue_key.partition("/")[0]),
            acknowledged_by=str(table.get("acknowledged_by", "")),
            acknowledged_on=str(table.get("acknowledged_on", "")),
            max_notional=_dec(table, "max_notional") if "max_notional" in table else None,
            max_orders_per_cycle=int(table.get("max_orders_per_cycle", 1)),
            exits=str(table.get("exits", "attached")),
            limit_timeout_s=float(table.get("limit_timeout_s", 30.0)),
            price_offset_bps=(
                _dec(table, "price_offset_bps") if "price_offset_bps" in table else Decimal(0)
            ),
            adopt_open_position=_flag(table, "adopt_open_position"),
            params=dict(table.get("params") or {}),
            ccxt_options=_ccxt_options(table),
            environment=str(table.get("environment", "")),
            max_drift_bps=_dec(table, "max_drift_bps") if "max_drift_bps" in table else None,
            max_decision_age_s=float(table.get("max_decision_age_s", 120.0)),
            leverage=_dec(table, "leverage") if "leverage" in table else None,
            margin_mode=str(table.get("margin_mode", "isolated")),
            max_slippage_bps=(
                _dec(table, "max_slippage_bps") if "max_slippage_bps" in table else Decimal(20)
            ),
            max_margin_share=(
                _dec(table, "max_margin_share") if "max_margin_share" in table else Decimal("0.5")
            ),
            liquidation_buffer_bps=(
                _dec(table, "liquidation_buffer_bps")
                if "liquidation_buffer_bps" in table
                else Decimal(50)
            ),
            hold_limit=str(table.get("hold_limit", "timeout")),
            markets=_markets(table),
            equity_session=str(table.get("equity_session", "regular")),
            equity_closed=_execution_names(table, "equity_closed"),
            equities=tuple(n.upper() for n in _execution_names(table, "equities")),
        )
    except MandateError as exc:
        raise ConfigError(str(exc)) from exc


# The names ccxt signs with, beyond the words SECRET_LIKE already catches.
CREDENTIAL_LIKE = (*SECRET_LIKE, "wallet", "uid", "twofa", "login")


def _ccxt_options(table: Mapping[str, Any]) -> dict[str, Any]:
    options = table.get("ccxt_options", {})
    if not isinstance(options, dict):
        raise ConfigError("[execution].ccxt_options is a table, as in { builderFee = true }")
    leaked = sorted(k for k in options if any(word in k.lower() for word in CREDENTIAL_LIKE))
    if leaked:
        raise ConfigError(
            f"[execution].ccxt_options carries {leaked}; "
            "keys come from the environment, never a file"
        )
    return dict(options)


def _execution_names(table: Mapping[str, Any], key: str) -> tuple[str, ...]:
    listed = table.get(key, [])
    if not isinstance(listed, list) or not all(isinstance(n, str) for n in listed):
        raise ConfigError(f"[execution].{key} is a list of strings")
    return tuple(n.strip() for n in listed if n.strip())


def _markets(table: Mapping[str, Any]) -> tuple[str, ...]:
    if "markets" not in table:
        return DEFAULT_MARKETS
    listed = table["markets"]
    if not isinstance(listed, list) or not all(isinstance(kind, str) for kind in listed):
        raise ConfigError('[execution].markets is a list of market types: ["linear perpetual"]')
    return tuple(dict.fromkeys(kind.strip() for kind in listed))


LIVE_KEYS = (
    "venue",
    "environment",
    # A flag about how a key must be bound, which is not a key.
    "key_ip_unbound_ok",
    "adopt_open_position",
    "poll_interval_ms",
)


def _live(raw: dict[str, Any], venue_key: str) -> LiveSettings | None:
    table = raw.get("live")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[live] must be a table")
    unknown = sorted(set(table) - set(LIVE_KEYS))
    _no_secrets(unknown, "[live]")
    if unknown:
        raise ConfigError(f"[live]: unknown {unknown}; have {list(LIVE_KEYS)}")
    environment = str(table.get("environment", "demo"))
    poll = table.get("poll_interval_ms", 250)
    if isinstance(poll, bool) or not isinstance(poll, int) or poll < 0:
        raise ConfigError("[live].poll_interval_ms is a non-negative integer")
    return LiveSettings(
        venue=str(table.get("venue") or venue_key.partition("/")[0]),
        environment=environment,
        key_ip_unbound_ok=_flag(table, "key_ip_unbound_ok"),
        adopt_open_position=_flag(table, "adopt_open_position"),
        poll_interval_ms=poll,
    )


def _canary(raw: dict[str, Any]) -> CanaryLimits | None:
    table = raw.get("canary")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[canary] must be a table")
    try:
        return CanaryLimits(
            max_notional=_dec(table, "max_notional"),
            max_round_trips=_int(table, "max_round_trips"),
            session_loss_limit_bps=_dec(table, "session_loss_limit_bps"),
            max_session_minutes=_int(table, "max_session_minutes"),
        )
    except CanaryError as exc:
        raise ConfigError(f"[canary]: {exc}") from exc


def load_funding(path: Path, interval_s: int) -> FundingSchedule:
    """`funding.json` as written by `tra data funding`: settlement timestamps to rates."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read funding: {exc}") from exc
    rates = raw.get("rates") if isinstance(raw, dict) else None
    if not isinstance(rates, dict) or not rates:
        raise ConfigError(f"{path}: funding has no rates")
    parsed: dict[int, Decimal] = {}
    for ts, rate in rates.items():
        try:
            parsed[int(ts)] = Decimal(str(rate))
        except (ValueError, ArithmeticError) as exc:
            raise ConfigError(f"{path}: bad funding row {ts!r}: {rate!r}") from exc
    declared = raw.get("interval_s")
    if declared is not None and int(declared) != interval_s:
        raise ConfigError(
            f"{path}: funding interval {declared}s differs from [run].settlement_interval_s "
            f"{interval_s}s"
        )
    try:
        return FundingSchedule(interval_s=interval_s, rates=parsed)
    except CostError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


EXIT_KEYS = (
    "mode",
    "stop_frac",
    "take_profit_frac",
    "timeout_bars",
    "mechanism_bound",
    "slip_bps",
)


VOLATILITY_KEYS = ("stop_atr", "reward_to_risk", "min_stop_frac", "max_stop_frac", "atr_bars")


ANALYST_KEYS = ("min_reward_to_risk", "max_reward_to_risk", "min_stop_atr")


def _volatility_exit(exit_raw: dict[str, Any]) -> VolatilityExit | None:
    """[exit].mode and what it brings. Fixed is one fraction of price for every trade."""
    known = EXIT_KEYS + VOLATILITY_KEYS + ANALYST_KEYS
    unknown = sorted(set(exit_raw) - set(known))
    if unknown:
        raise ConfigError(f"[exit]: unknown {unknown}; have {list(known)}")
    mode = str(exit_raw.get("mode", "fixed"))
    if mode not in MODES:
        raise ConfigError(f"[exit].mode is one of {list(MODES)}, not {mode!r}")
    given = sorted(key for key in VOLATILITY_KEYS if key in exit_raw)
    if mode == "fixed":
        if given:
            raise ConfigError(f'[exit]: {given} apply with mode = "volatility" and this is fixed')
        return None
    levels = sorted(key for key in ANALYST_KEYS if key in exit_raw)
    if mode == "volatility" and levels:
        raise ConfigError(f'[exit]: {levels} apply with mode = "analyst" and this is volatility')
    fixed = sorted(key for key in ("stop_frac", "take_profit_frac") if key in exit_raw)
    if fixed:
        # Both would be read by someone and only one would be used. Say so rather than ignore it.
        raise ConfigError(
            f"[exit]: {fixed} are the fixed mode's exits; with mode = \"{mode}\" each trade's "
            "stop is placed per trade, so remove them"
        )
    plain = VolatilityExit()

    def number(key: str) -> Decimal:
        return _dec(exit_raw, key) if key in exit_raw else getattr(plain, key)

    try:
        rule = VolatilityExit(
            stop_atr=number("stop_atr"),
            reward_to_risk=number("reward_to_risk"),
            min_stop_frac=number("min_stop_frac"),
            max_stop_frac=number("max_stop_frac"),
            atr_bars=_int(exit_raw, "atr_bars") if "atr_bars" in exit_raw else plain.atr_bars,
            analyst_levels=mode == "analyst",
            min_reward_to_risk=number("min_reward_to_risk"),
            max_reward_to_risk=number("max_reward_to_risk"),
            min_stop_atr=number("min_stop_atr"),
        )
    except StopError as exc:
        raise ConfigError(str(exc)) from exc
    # Where one number is needed up front: the widest stop, and the least reward a trade may carry.
    floor = rule.min_reward_to_risk if rule.analyst_levels else rule.reward_to_risk
    exit_raw["stop_frac"] = rule.max_stop_frac
    exit_raw["take_profit_frac"] = rule.max_stop_frac * floor
    return rule


AGENT_EXIT_BOUND = (
    "A position closes at its stop, its take profit or its time limit, whichever comes first."
)


CONTROLS_KEYS = (
    "max_risk_per_trade_bps",
    "max_position",
    "max_position_quote",
    "max_orders_per_minute",
    "daily_loss_limit_bps",
    "max_drawdown_bps",
    "on_drawdown",
    "kill_switch",
    "pause_switch",
    "max_trades_per_day",
    "max_losing_streak",
)


def _limits(table: dict[str, Any]) -> Limits:
    """[controls], every figure with a standard default, so a desk starts protected."""
    unknown = sorted(set(table) - set(CONTROLS_KEYS))
    _no_secrets(unknown, "[controls]")
    if unknown:
        raise ConfigError(f"[controls]: unknown {unknown}; have {list(CONTROLS_KEYS)}")
    plain = Limits()

    def optional(key: str) -> Decimal | None:
        return _dec(table, key) if key in table else None

    try:
        return Limits(
            max_risk_per_trade_bps=_or_dec(
                table, "max_risk_per_trade_bps", str(plain.max_risk_per_trade_bps)
            ),
            max_position=optional("max_position"),
            max_orders_per_minute=_or_int(
                table, "max_orders_per_minute", plain.max_orders_per_minute
            ),
            daily_loss_limit_bps=_or_dec(
                table, "daily_loss_limit_bps", str(plain.daily_loss_limit_bps)
            ),
            max_position_quote=optional("max_position_quote"),
            max_drawdown_bps=optional("max_drawdown_bps"),
            on_drawdown=str(table.get("on_drawdown", "stop")),
            max_trades_per_day=(
                _int(table, "max_trades_per_day") if "max_trades_per_day" in table else None
            ),
            max_losing_streak=(
                _int(table, "max_losing_streak") if "max_losing_streak" in table else None
            ),
        )
    except ControlError as exc:
        raise ConfigError(f"[controls]: {exc}") from exc
