"""The gates a desk declares: confluence, conviction, targets, sizing, the desk, the universe, the
trigger, the audits and the regime.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading_research_agents.data.sources.config import DataConfigError, parse_duration
from trading_research_agents.execution.confluence import ConfluenceError, Matrix
from trading_research_agents.execution.conviction import Conviction, ConvictionError, Tier
from trading_research_agents.execution.desk import Desk, DeskError
from trading_research_agents.execution.kelly import KellyError, SizingRule
from trading_research_agents.execution.regime import REGIME_KEYS, RegimeError, RegimeRule
from trading_research_agents.execution.targets import TargetError, Targets
from trading_research_agents.execution.trigger import Trigger, TriggerError
from trading_research_agents.execution.universe import Universe, UniverseError
from trading_research_agents.strategy.exits import ExitRule
from trading_research_agents.trading.config_agents import (
    READINGS,
    VOICES,
    _known,
    current_keys,
)
from trading_research_agents.trading.config_values import (
    ConfigError,
    _count,
    _dec,
    _names,
    _or_dec,
    _or_int,
)


@dataclass(frozen=True)
class AuditPolicy:
    """[audit]: when the auditors are asked without anyone typing `tra audit`.

    The two of them answer different questions and so they wake differently. Something breaking is
    an event - a provider that stopped answering halfway through a run is not something an operator
    should have to notice from a log - so the technical auditor wakes on an alarm. Whether the desk
    is earning what it costs is a question about a stretch of time, so the quant auditor runs on a
    count of cycles. Neither one can act on what it finds.
    """

    on_alarm: bool = True
    alarm_cooldown_s: int = 1800
    quant_every_cycles: int | None = None
    # A clock, for a review that lands daily whatever the cadence; kept on disk across restarts.
    quant_every_s: int | None = None
    # Losing round trips in a row that wake the quant auditor; 0 turns it off.
    losing_streak: int = 3
    # The risk manager is asked whenever the quant auditor is, and may only make the desk more
    # careful; false leaves the account to the client's limits alone.
    risk_manager: bool = True

    def __post_init__(self) -> None:
        if self.alarm_cooldown_s < 0:
            raise ConfigError("[audit].alarm_cooldown_s is not negative")
        if self.quant_every_cycles is not None and self.quant_every_cycles < 1:
            raise ConfigError("[audit].quant_every_cycles counts cycles, so it is at least 1")
        if self.quant_every_s is not None and self.quant_every_s < MIN_QUANT_EVERY_S:
            # Each look is a paid model call over the whole record; minutes apart it reads the
            # same trades again.
            raise ConfigError("[audit].quant_every is at least 5m")
        if self.losing_streak < 0:
            raise ConfigError("[audit].losing_streak counts round trips; 0 turns it off")

    @property
    def idle(self) -> bool:
        return not self.on_alarm and self.quant_every_cycles is None and self.quant_every_s is None


CONFLUENCE_KEYS = (
    "required",
    "long_states",
    "short_states",
    "on_missing",
    "min_support",
    "max_opposition",
)


TARGET_KEYS = (
    "min_net_bps",
    "min_edge_multiple",
    "min_reward_risk",
    "max_research_bps",
    "max_round_trips_per_day",
    "max_break_even_win_rate",
    "min_path_ev_bps",
)


TRIGGER_KEYS = ("min_move_bps", "max_spread_bps", "cooldown_s", "widen_after", "look_valid_for")


AUDIT_KEYS = (
    "on_alarm",
    "alarm_cooldown_s",
    "quant_every_cycles",
    "quant_every",
    "losing_streak",
    "risk_manager",
)


MIN_QUANT_EVERY_S = 300


UNIVERSE_KEYS = (
    "instruments",
    "max_per_cycle",
    "max_intents_per_day",
    "screener",
    "discover",
    "top",
    "min_quote_volume",
    "max_spread_bps",
    "min_listed_days",
    "crypto_only",
    "exclude",
    "quote",
    "type_field",
    "exclude_types",
)


DESK_KEYS = ("max_open_risk_bps", "max_open_positions", "correlation_above")


CONVICTION_KEYS = (
    "weights",
    "reviewer_weight",
    "min_score",
    "max_leverage",
    "ladder",
)


def _conviction(raw: dict[str, Any]) -> Conviction:
    """[conviction]: what each voice is worth, the floor, and the ladder from score to size."""
    table = raw.get("conviction")
    if table is None:
        return Conviction()
    if not isinstance(table, dict):
        raise ConfigError("[conviction] must be a table")
    table = current_keys(table, "[conviction]")
    if "reader_labels" in table:
        raise ConfigError(
            "[conviction].reader_labels: a reading no longer gives a label of its own since "
            "0.11; typed text events from the judge take its place in 0.12. Remove the key"
        )
    unknown = sorted(set(table) - set(CONVICTION_KEYS))
    if unknown:
        raise ConfigError(f"[conviction]: unknown {unknown}; have {list(CONVICTION_KEYS)}")
    weights_raw = table.get("weights") or {}
    if not isinstance(weights_raw, dict):
        raise ConfigError("[conviction].weights is a table of voice to weight")
    voices = _known(weights_raw, VOICES, "[conviction].weights")
    pairs = zip(voices, weights_raw, strict=True)
    weights = {voice: _dec(weights_raw, key) for voice, key in pairs}
    rungs = []
    for entry in table.get("ladder") or ():
        if not isinstance(entry, dict) or set(entry) != {"at_least", "size_multiple"}:
            raise ConfigError(
                "[[conviction.ladder]] entries carry at_least and size_multiple, nothing else"
            )
        rungs.append(
            Tier(at_least=_dec(entry, "at_least"), size_multiple=_dec(entry, "size_multiple"))
        )
    try:
        return Conviction(
            weights=weights,
            reviewer_weight=(
                _dec(table, "reviewer_weight") if "reviewer_weight" in table else Decimal(0)
            ),
            min_score=_dec(table, "min_score") if "min_score" in table else Decimal(0),
            ladder=tuple(rungs),
            max_leverage=_dec(table, "max_leverage") if "max_leverage" in table else None,
        )
    except ConvictionError as exc:
        raise ConfigError(str(exc)) from exc


def _confluence(raw: dict[str, Any]) -> Matrix:
    """[confluence]: which readings must agree with the direction, and how votes are counted."""
    table = raw.get("confluence")
    if table is None:
        return Matrix()
    if not isinstance(table, dict):
        raise ConfigError("[confluence] must be a table")
    unknown = sorted(set(table) - set(CONFLUENCE_KEYS))
    if unknown:
        raise ConfigError(f"[confluence]: unknown {unknown}; have {list(CONFLUENCE_KEYS)}")
    required = _known(
        _names(table.get("required"), "required"),
        (*READINGS, "flow_specialist"),
        "[confluence].required",
    )
    try:
        return Matrix(
            required=required,
            long_states=_names(table.get("long_states"), "long_states") or ("bullish",),
            short_states=_names(table.get("short_states"), "short_states") or ("bearish",),
            on_missing=str(table.get("on_missing", "refuse")),
            min_support=_count(table, "min_support"),
            max_opposition=(_count(table, "max_opposition") if "max_opposition" in table else None),
        )
    except ConfluenceError as exc:
        raise ConfigError(str(exc)) from exc


def _targets(raw: dict[str, Any], exit_rule: ExitRule) -> Targets:
    """[targets]: what a trade has to be worth, and how often this desk will take one."""
    table = raw.get("targets")
    if table is None:
        return Targets()
    if not isinstance(table, dict):
        raise ConfigError("[targets] must be a table")
    unknown = sorted(set(table) - set(TARGET_KEYS))
    if unknown:
        raise ConfigError(f"[targets]: unknown {unknown}; have {list(TARGET_KEYS)}")
    if "min_reward_risk" in table and exit_rule.take_profit_frac is None:
        # Fixed mode only: a measured take profit always carries the declared reward to risk.
        raise ConfigError(
            "[targets].min_reward_risk needs [exit].take_profit_frac; "
            "a ratio without a take profit is not a ratio"
        )
    try:
        return Targets(
            min_net_bps=_dec(table, "min_net_bps") if "min_net_bps" in table else None,
            min_edge_multiple=(
                _dec(table, "min_edge_multiple") if "min_edge_multiple" in table else None
            ),
            min_reward_risk=(
                _dec(table, "min_reward_risk") if "min_reward_risk" in table else None
            ),
            max_research_bps=(
                _dec(table, "max_research_bps") if "max_research_bps" in table else None
            ),
            max_round_trips_per_day=(
                _count(table, "max_round_trips_per_day")
                if "max_round_trips_per_day" in table
                else None
            ),
            max_break_even_win_rate=_or_dec(table, "max_break_even_win_rate", "0.5"),
            min_path_ev_bps=_dec(table, "min_path_ev_bps") if "min_path_ev_bps" in table else None,
        )
    except TargetError as exc:
        raise ConfigError(str(exc)) from exc


SIZING_KEYS = (
    "kelly_fraction",
    "min_round_trips",
    "lookback_round_trips",
    "simulate_trades",
    "simulate_paths",
    "seed",
    "max_drawdown_probability",
)


def _sizing(raw: dict[str, Any]) -> SizingRule:
    """[sizing]: the Kelly ceiling from the desk's own round trips, off until declared."""
    table = raw.get("sizing")
    if table is None:
        return SizingRule()
    if not isinstance(table, dict):
        raise ConfigError("[sizing] must be a table")
    unknown = sorted(set(table) - set(SIZING_KEYS))
    if unknown:
        raise ConfigError(f"[sizing]: unknown {unknown}; have {list(SIZING_KEYS)}")
    if "kelly_fraction" not in table:
        raise ConfigError("[sizing] is declared without kelly_fraction, which is what turns it on")
    plain = SizingRule()
    try:
        return SizingRule(
            kelly_fraction=_dec(table, "kelly_fraction"),
            min_round_trips=_or_int(table, "min_round_trips", plain.min_round_trips),
            lookback_round_trips=_or_int(table, "lookback_round_trips", plain.lookback_round_trips),
            simulate_trades=_or_int(table, "simulate_trades", plain.simulate_trades),
            simulate_paths=_or_int(table, "simulate_paths", plain.simulate_paths),
            seed=_or_int(table, "seed", plain.seed),
            max_drawdown_probability=_or_dec(
                table, "max_drawdown_probability", str(plain.max_drawdown_probability)
            ),
        )
    except KellyError as exc:
        raise ConfigError(str(exc)) from exc


def _desk(raw: dict[str, Any]) -> Desk:
    """[desk]: what this desk may be carrying across every instrument at once."""
    table = raw.get("desk")
    if table is None:
        return Desk()
    if not isinstance(table, dict):
        raise ConfigError("[desk] must be a table")
    unknown = sorted(set(table) - set(DESK_KEYS))
    if unknown:
        raise ConfigError(f"[desk]: unknown {unknown}; have {list(DESK_KEYS)}")
    try:
        return Desk(
            max_open_risk_bps=(
                _dec(table, "max_open_risk_bps") if "max_open_risk_bps" in table else None
            ),
            max_open_positions=(
                _count(table, "max_open_positions") if "max_open_positions" in table else None
            ),
            correlation_above=(
                _dec(table, "correlation_above") if "correlation_above" in table else None
            ),
        )
    except DeskError as exc:
        raise ConfigError(str(exc)) from exc


def _universe(raw: dict[str, Any]) -> Universe:
    """[universe]: the instruments a pass looks at before it spends a cycle on one of them."""
    table = raw.get("universe")
    if table is None:
        return Universe()
    if not isinstance(table, dict):
        raise ConfigError("[universe] must be a table")
    unknown = sorted(set(table) - set(UNIVERSE_KEYS))
    if unknown:
        raise ConfigError(f"[universe]: unknown {unknown}; have {list(UNIVERSE_KEYS)}")
    names = table.get("instruments") or []
    if not isinstance(names, list) or not all(isinstance(n, str) and n.strip() for n in names):
        raise ConfigError("[universe].instruments is a list of instrument names")
    flags = {}
    for key, default in (("screener", False), ("discover", False), ("crypto_only", True)):
        flags[key] = table.get(key, default)
        if not isinstance(flags[key], bool):
            raise ConfigError(f"[universe].{key} is true or false")
    excluded = table.get("exclude") or []
    if not isinstance(excluded, list) or not all(isinstance(n, str) for n in excluded):
        raise ConfigError("[universe].exclude is a list of instrument names")
    try:
        return Universe(
            instruments=tuple(str(n).strip() for n in names),
            max_per_cycle=int(table.get("max_per_cycle", 1)),
            max_intents_per_day=(
                _count(table, "max_intents_per_day") if "max_intents_per_day" in table else None
            ),
            screener=flags["screener"],
            discover=flags["discover"],
            top=_count(table, "top") if "top" in table else 20,
            min_quote_volume=(
                _dec(table, "min_quote_volume")
                if "min_quote_volume" in table
                else Decimal(10_000_000)
            ),
            max_spread_bps=(
                _dec(table, "max_spread_bps") if "max_spread_bps" in table else Decimal(5)
            ),
            min_listed_days=(
                _count(table, "min_listed_days") if "min_listed_days" in table else 30
            ),
            crypto_only=flags["crypto_only"],
            exclude=tuple(n.strip().upper() for n in excluded),
            quote=str(table.get("quote", "USDT")).upper(),
            type_field=str(table.get("type_field", "")).strip(),
            exclude_types=_universe_names(table, "exclude_types"),
        )
    except UniverseError as exc:
        raise ConfigError(str(exc)) from exc


def _audit(raw: dict[str, Any]) -> AuditPolicy:
    """[audit]: when each auditor is asked, and how often an alarm may wake one."""
    table = raw.get("audit")
    if table is None:
        return AuditPolicy()
    if not isinstance(table, dict):
        raise ConfigError("[audit] must be a table")
    unknown = sorted(set(table) - set(AUDIT_KEYS))
    if unknown:
        raise ConfigError(f"[audit]: unknown {unknown}; have {list(AUDIT_KEYS)}")
    on_alarm = table.get("on_alarm", True)
    if not isinstance(on_alarm, bool):
        raise ConfigError("[audit].on_alarm is true or false")
    return AuditPolicy(
        on_alarm=on_alarm,
        alarm_cooldown_s=int(table.get("alarm_cooldown_s", 1800)),
        quant_every_cycles=(
            _count(table, "quant_every_cycles") if "quant_every_cycles" in table else None
        ),
        quant_every_s=_every(table),
        losing_streak=_count(table, "losing_streak") if "losing_streak" in table else 3,
        risk_manager=bool(table.get("risk_manager", True)),
    )


def _every(table: dict[str, Any]) -> int | None:
    if "quant_every" not in table:
        return None
    try:
        return int(parse_duration(str(table["quant_every"])))
    except DataConfigError as exc:
        raise ConfigError(f"[audit].quant_every: {exc}") from exc


def _trigger(raw: dict[str, Any]) -> Trigger:
    """[trigger]: what has to be true before a cycle pays anyone to look at the instrument."""
    table = raw.get("trigger")
    if table is None:
        return Trigger()
    if not isinstance(table, dict):
        raise ConfigError("[trigger] must be a table")
    unknown = sorted(set(table) - set(TRIGGER_KEYS))
    if unknown:
        raise ConfigError(f"[trigger]: unknown {unknown}; have {list(TRIGGER_KEYS)}")
    try:
        return Trigger(
            min_move_bps=_dec(table, "min_move_bps") if "min_move_bps" in table else None,
            max_spread_bps=(_dec(table, "max_spread_bps") if "max_spread_bps" in table else None),
            cooldown_s=_count(table, "cooldown_s") if "cooldown_s" in table else None,
            widen_after=_count(table, "widen_after") if "widen_after" in table else None,
            look_valid_s=(
                int(parse_duration(str(table["look_valid_for"])))
                if "look_valid_for" in table
                else Trigger().look_valid_s
            ),
        )
    except (TriggerError, DataConfigError) as exc:
        raise ConfigError(str(exc)) from exc


MAX_CONTEXT_INTERVALS = 3


def _context_intervals(raw: Any, decision_s: int) -> tuple[int, ...]:
    """[run].context_intervals: "15m", "1h" or seconds, each above the decision timeframe."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError('[run].context_intervals is a list, such as ["15m", "1h"]')
    out: list[int] = []
    for item in raw:
        try:
            seconds = int(parse_duration(str(item)))
        except DataConfigError as exc:
            raise ConfigError(f"[run].context_intervals: {exc}") from exc
        if seconds <= decision_s or seconds % decision_s:
            raise ConfigError(
                f"[run].context_intervals: {item} is not a whole multiple above "
                f"bar_interval_s {decision_s}; context is read on a higher timeframe"
            )
        if seconds in out:
            raise ConfigError(f"[run].context_intervals names {item} twice")
        out.append(seconds)
    if len(out) > MAX_CONTEXT_INTERVALS:
        # Each one is a venue call and a section of the brief on every cycle.
        raise ConfigError(f"[run].context_intervals takes at most {MAX_CONTEXT_INTERVALS}")
    return tuple(sorted(out))


def _universe_names(table: dict[str, Any], key: str) -> tuple[str, ...]:
    value = table.get(key) or []
    if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
        raise ConfigError(f"[universe].{key} is a list of names")
    return tuple(n.strip() for n in value)


def _regime(raw: dict[str, Any]) -> RegimeRule:
    """[regime]: the trend-strength lines between ranging, mixed and trending."""
    table = raw.get("regime")
    if table is None:
        return RegimeRule()
    if not isinstance(table, dict):
        raise ConfigError("[regime] must be a table")
    unknown = sorted(set(table) - set(REGIME_KEYS))
    if unknown:
        raise ConfigError(f"[regime]: unknown {unknown}; have {list(REGIME_KEYS)}")
    fit = table.get("setups_must_fit", True)
    if not isinstance(fit, bool):
        raise ConfigError("[regime].setups_must_fit is true or false")
    try:
        return RegimeRule(
            trend_above=_dec(table, "trend_above") if "trend_above" in table else Decimal("2.0"),
            range_below=_dec(table, "range_below") if "range_below" in table else Decimal("1.0"),
            setups_must_fit=fit,
            fade_below_variance_ratio=(
                _dec(table, "fade_below_variance_ratio")
                if "fade_below_variance_ratio" in table
                else Decimal("1.0")
            ),
        )
    except RegimeError as exc:
        raise ConfigError(str(exc)) from exc
