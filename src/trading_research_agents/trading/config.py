"""`strategy.toml`: everything a run needs, declared once, none of it in the logic."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.execution.confluence import STANCES, ConfluenceError, Matrix
from trading_research_agents.execution.conviction import Conviction, ConvictionError, Tier
from trading_research_agents.execution.cost import CostError, FundingSchedule
from trading_research_agents.execution.mandate import Mandate, MandateError
from trading_research_agents.execution.targets import TargetError, Targets
from trading_research_agents.strategy.core import Strategy
from trading_research_agents.strategy.exits import ExitRule
from trading_research_agents.trading.canary import CanaryError, CanaryLimits
from trading_research_agents.trading.controls import Limits

# Keys never live in a config file. Any of these under [live] is refused by name.
SECRET_LIKE = ("api_key", "api_secret", "secret", "key", "token", "password", "passphrase")


class ConfigError(ValueError):
    """The run configuration is incomplete or contradicts itself."""


@dataclass(frozen=True)
class RunSettings:
    instrument: str
    venue_key: str
    bar_interval_s: int
    size: Decimal
    spread_cost_bps: Decimal
    equity: Decimal | None
    latency_ms: Decimal
    slippage_bps: Decimal
    settlement_interval_s: int
    # The venue's own size increment. Sizes are always rounded down to it, never up.
    size_step: Decimal = Decimal("0.000001")


@dataclass(frozen=True)
class RoleModel:
    """One role's model, after the defaults in [agents] and its own overrides are settled."""

    role: str
    provider: str
    model: str | None
    base_url: str | None
    max_output_tokens: int
    call_timeout_s: int
    max_retries: int


@dataclass(frozen=True)
class AgentSettings:
    """[agents]: which model decides, how often it may be asked, and what that may cost."""

    role: str
    provider: str
    model: str | None
    base_url: str | None
    mechanism: str
    token_budget: int
    max_calls_per_hour: int
    min_interval_s: int
    call_timeout_s: int
    lookback_bars: int
    min_confidence: float
    pricing: dict[str, tuple[Decimal, Decimal]]
    decide_every_bars: int = 1
    max_output_tokens: int = 1024
    max_retries: int = 2
    magnitude_bps: Decimal | None = None
    screen: dict[str, float] = field(default_factory=dict)
    # [agents.roles.<role>]: a cheaper model for the roles that only read and label, a stronger
    # one where the reasoning is on numbers. Anything a role does not override it inherits.
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    # [agents].analysts: the roster, when the operator wants to say it outright. Empty means the
    # cycle asks the technical analyst plus whichever reading analysts have a declared source.
    analysts: tuple[str, ...] = ()
    # [agents].validators: the stances asked about a proposal once there is one. Each stance is one
    # more call, so this is empty until an operator asks for it.
    validators: tuple[str, ...] = ()

    def for_role(self, role: str) -> RoleModel:
        """What this role is asked with: its own model where declared, otherwise the default."""
        table = self.roles.get(role, {})
        return RoleModel(
            role=role,
            provider=str(table.get("provider", self.provider)),
            model=(str(table["model"]) if table.get("model") else self.model),
            base_url=(str(table["base_url"]) if table.get("base_url") else self.base_url),
            max_output_tokens=int(table.get("max_output_tokens", self.max_output_tokens)),
            call_timeout_s=int(table.get("call_timeout_s", self.call_timeout_s)),
            max_retries=int(table.get("max_retries", self.max_retries)),
        )


@dataclass(frozen=True)
class LiveSettings:
    """[live]: which plugin, which environment, and the two deliberate overrides."""

    venue: str
    environment: str
    key_ip_unbound_ok: bool = False
    adopt_open_position: bool = False
    poll_interval_ms: int = 250


@dataclass(frozen=True)
class StrategyConfig:
    strategy: Strategy
    exit_rule: ExitRule
    limits: Limits
    kill_switch: Path | None
    run: RunSettings
    registration_seal: str | None
    live: LiveSettings | None = None
    canary: CanaryLimits | None = None
    funding: FundingSchedule | None = None
    agents: AgentSettings | None = None
    # Two gates the operator declares and code applies: whether the context agrees with the
    # direction, and whether the trade is the shape this desk said it wanted.
    confluence: Matrix = field(default_factory=Matrix)
    targets: Targets = field(default_factory=Targets)
    # How much capital goes behind an idea, from the same declarations the matrix reads.
    conviction: Conviction = field(default_factory=Conviction)
    # What a cycle may do with an admitted intent: keep it, export it, or send it.
    execution: Mandate = field(default_factory=Mandate)


def load_strategy_config(path: Path) -> StrategyConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from exc
    return parse_strategy_config(raw, base=path.resolve().parent)


def parse_strategy_config(raw: dict[str, Any], *, base: Path | None = None) -> StrategyConfig:
    strategy_raw = _table(raw, "strategy")
    exit_raw = _table(raw, "exit")
    run_raw = _table(raw, "run")
    controls_raw = _table(raw, "controls")

    agents = _agents(raw, strategy_raw)
    strategy = _build_strategy(strategy_raw, base) if agents is None else _placeholder(agents)
    exit_rule = ExitRule(
        stop_sigma_frac=_dec(exit_raw, "stop_sigma_frac"),
        timeout_bars=_int(exit_raw, "timeout_bars"),
        mechanism_bound=str(exit_raw.get("mechanism_bound", "")),
        take_profit_sigma_frac=(
            _dec(exit_raw, "take_profit_sigma_frac")
            if "take_profit_sigma_frac" in exit_raw
            else None
        ),
        slip_bps=_dec(exit_raw, "slip_bps") if "slip_bps" in exit_raw else Decimal("5.0"),
    )
    limits = Limits(
        max_risk_per_trade_bps=_dec(controls_raw, "max_risk_per_trade_bps"),
        max_position=_dec(controls_raw, "max_position"),
        max_orders_per_minute=_int(controls_raw, "max_orders_per_minute"),
        daily_loss_limit_bps=_dec(controls_raw, "daily_loss_limit_bps"),
    )
    kill = controls_raw.get("kill_switch")
    run = RunSettings(
        instrument=str(run_raw.get("instrument", "")),
        venue_key=str(run_raw.get("venue_key", "")),
        bar_interval_s=_int(run_raw, "bar_interval_s"),
        size=_dec(run_raw, "size"),
        spread_cost_bps=_dec(run_raw, "spread_cost_bps"),
        equity=_dec(run_raw, "equity") if "equity" in run_raw else None,
        latency_ms=_dec(run_raw, "latency_ms"),
        slippage_bps=_dec(run_raw, "slippage_bps"),
        settlement_interval_s=_int(run_raw, "settlement_interval_s"),
        size_step=_dec(run_raw, "size_step") if "size_step" in run_raw else Decimal("0.000001"),
    )
    if not run.instrument or not run.venue_key:
        raise ConfigError("[run] names the instrument and the venue_key (venue/channel)")
    seal = strategy_raw.get("registration_seal")
    if seal is not None and (not isinstance(seal, str) or len(seal) != 64):
        raise ConfigError("[strategy].registration_seal is the 64-character pre-registration seal")
    funding = None
    funding_file = run_raw.get("funding")
    if funding_file is not None:
        funding = load_funding(_resolve(Path(str(funding_file)), base), run.settlement_interval_s)
    return StrategyConfig(
        strategy=strategy,
        exit_rule=exit_rule,
        limits=limits,
        kill_switch=Path(str(kill)) if kill else None,
        run=run,
        registration_seal=seal,
        live=_live(raw, run.venue_key),
        canary=_canary(raw),
        funding=funding,
        agents=agents,
        confluence=_confluence(raw),
        targets=_targets(raw, exit_rule),
        conviction=_conviction(raw),
        execution=_mandate(raw, run.venue_key),
    )


class _Pending:
    """Stands in for the agent core until the CLI has built a completer for it."""

    name = "agent-pending"

    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings

    def on_bar(self, bar: Any) -> Any:
        raise ConfigError("the agent core has not been built; the CLI builds it from [agents]")


def _placeholder(settings: AgentSettings) -> Strategy:
    return _Pending(settings)


KNOWN_ROLES = (
    "market_screener",
    "technical_analyst",
    "macro_analyst",
    "onchain_analyst",
    "sentiment_analyst",
    "risk_manager",
    "executor",
    "auditor",
    # One stance is one call, so a validator may run on its own model like any other role.
    "validator",
)
ROLE_KEYS = ("provider", "model", "base_url", "max_output_tokens", "call_timeout_s", "max_retries")


ASKABLE = (
    "technical_analyst",
    "macro_analyst",
    "onchain_analyst",
    "sentiment_analyst",
)


def _analysts(raw: Any) -> tuple[str, ...]:
    """`[agents].analysts`, when the operator names the roster instead of letting sources decide."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ConfigError("[agents].analysts is a list of role names")
    names = [str(name) for name in raw]
    unknown = sorted(set(names) - set(ASKABLE))
    if unknown:
        raise ConfigError(f"[agents].analysts: {unknown} cannot be asked; have {list(ASKABLE)}")
    if "technical_analyst" not in names:
        raise ConfigError(
            "[agents].analysts includes technical_analyst; it is the role that decides"
        )
    if len(set(names)) != len(names):
        raise ConfigError("[agents].analysts names a role twice")
    return tuple(names)


def _roles(raw: Any) -> dict[str, dict[str, Any]]:
    """`[agents.roles.<role>]` tables, checked against the roles this package actually asks."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("[agents.roles] holds one table per role")
    out: dict[str, dict[str, Any]] = {}
    for role, table in raw.items():
        if role not in KNOWN_ROLES:
            raise ConfigError(f"[agents.roles.{role}] is not a role; have {list(KNOWN_ROLES)}")
        if not isinstance(table, dict):
            raise ConfigError(f"[agents.roles.{role}] is a table")
        unknown = sorted(set(table) - set(ROLE_KEYS))
        if unknown:
            raise ConfigError(
                f"[agents.roles.{role}]: {unknown} is not settable; {list(ROLE_KEYS)}"
            )
        out[role] = dict(table)
    return out


def _agents(raw: dict[str, Any], strategy_raw: dict[str, Any]) -> AgentSettings | None:
    role = strategy_raw.get("agent")
    if role is None:
        return None
    if role != "technical_analyst":
        raise ConfigError("[strategy].agent names a decision role; only technical_analyst decides")
    if strategy_raw.get("file") or strategy_raw.get("import"):
        raise ConfigError("[strategy] names either a code core or an agent, not both")
    table = raw.get("agents")
    if not isinstance(table, dict):
        raise ConfigError("[agents] is required when [strategy].agent is set")
    provider = str(table.get("provider", ""))
    if not provider:
        raise ConfigError("[agents].provider names the model provider")
    mechanism = str(table.get("mechanism", "")).strip()
    if not mechanism:
        raise ConfigError("[agents].mechanism states what the analyst reasons from")
    pricing: dict[str, tuple[Decimal, Decimal]] = {}
    for model_name, row in (table.get("pricing") or {}).items():
        if not isinstance(row, dict):
            raise ConfigError("[agents.pricing.<model>] is a table")
        pricing[str(model_name)] = (_dec(row, "input_per_million"), _dec(row, "output_per_million"))
    confidence = table.get("min_confidence", 0.5)
    if isinstance(confidence, bool) or not isinstance(confidence, int | float | Decimal):
        raise ConfigError("[agents].min_confidence is a number in [0, 1]")
    return AgentSettings(
        role=str(role),
        provider=provider,
        model=str(table["model"]) if table.get("model") else None,
        base_url=str(table["base_url"]) if table.get("base_url") else None,
        mechanism=mechanism,
        token_budget=_int(table, "token_budget"),
        max_calls_per_hour=_int(table, "max_calls_per_hour"),
        min_interval_s=_int(table, "min_interval_s"),
        call_timeout_s=int(table.get("call_timeout_s", 60)),
        lookback_bars=int(table.get("lookback_bars", 60)),
        min_confidence=float(confidence),
        pricing=pricing,
        decide_every_bars=int(table.get("decide_every_bars", 1)),
        max_output_tokens=int(table.get("max_output_tokens", 1024)),
        max_retries=int(table.get("max_retries", 2)),
        magnitude_bps=_dec(table, "magnitude_bps") if "magnitude_bps" in table else None,
        screen=_screen(table.get("screen")),
        roles=_roles(table.get("roles")),
        analysts=_analysts(table.get("analysts")),
        validators=_validators(table.get("validators")),
    )


CONFLUENCE_KEYS = (
    "required",
    "long_states",
    "short_states",
    "on_missing",
    "min_support",
    "max_opposition",
)
TARGET_KEYS = ("min_net_bps", "min_reward_risk", "max_round_trips_per_day")


EXECUTION_KEYS = (
    "mode",
    "venue",
    "acknowledged_by",
    "acknowledged_on",
    "max_notional",
    "max_orders_per_cycle",
    "limit_timeout_s",
    "price_offset_bps",
    "adopt_open_position",
    "params",
)


def _mandate(raw: dict[str, Any], venue_key: str) -> Mandate:
    """[execution]: the mode, and the acknowledgement venue mode needs before it starts."""
    table = raw.get("execution")
    if table is None:
        return Mandate()
    if not isinstance(table, dict):
        raise ConfigError("[execution] must be a table")
    unknown = sorted(set(table) - set(EXECUTION_KEYS))
    if unknown:
        raise ConfigError(f"[execution]: unknown {unknown}; have {list(EXECUTION_KEYS)}")
    leaked = [k for k in table if any(word in k.lower() for word in SECRET_LIKE)]
    if leaked:
        raise ConfigError(
            f"[execution] carries {leaked}; keys come from the environment, never from a file"
        )
    try:
        return Mandate(
            mode=str(table.get("mode", "sandbox")),
            venue=str(table.get("venue") or venue_key.partition("/")[0]),
            acknowledged_by=str(table.get("acknowledged_by", "")),
            acknowledged_on=str(table.get("acknowledged_on", "")),
            max_notional=_dec(table, "max_notional") if "max_notional" in table else None,
            max_orders_per_cycle=int(table.get("max_orders_per_cycle", 1)),
            limit_timeout_s=float(table.get("limit_timeout_s", 30.0)),
            price_offset_bps=(
                _dec(table, "price_offset_bps") if "price_offset_bps" in table else Decimal(0)
            ),
            adopt_open_position=_flag(table, "adopt_open_position"),
            params=dict(table.get("params") or {}),
        )
    except MandateError as exc:
        raise ConfigError(str(exc)) from exc


CONVICTION_KEYS = ("weights", "validator_weight", "min_score", "max_leverage", "ladder")


def _conviction(raw: dict[str, Any]) -> Conviction:
    """[conviction]: what each voice is worth, the floor, and the ladder from score to size."""
    table = raw.get("conviction")
    if table is None:
        return Conviction()
    if not isinstance(table, dict):
        raise ConfigError("[conviction] must be a table")
    unknown = sorted(set(table) - set(CONVICTION_KEYS))
    if unknown:
        raise ConfigError(f"[conviction]: unknown {unknown}; have {list(CONVICTION_KEYS)}")
    weights_raw = table.get("weights") or {}
    if not isinstance(weights_raw, dict):
        raise ConfigError("[conviction].weights is a table of role to weight")
    stray = sorted(set(weights_raw) - set(ASKABLE))
    if stray:
        raise ConfigError(f"[conviction].weights: {stray} is not a role that is asked")
    weights = {role: _dec(weights_raw, role) for role in weights_raw}
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
            validator_weight=(
                _dec(table, "validator_weight") if "validator_weight" in table else Decimal(0)
            ),
            min_score=_dec(table, "min_score") if "min_score" in table else Decimal(0),
            ladder=tuple(rungs),
            max_leverage=_dec(table, "max_leverage") if "max_leverage" in table else None,
        )
    except ConvictionError as exc:
        raise ConfigError(str(exc)) from exc


def _validators(raw: Any) -> tuple[str, ...]:
    """`[agents].validators`: the stances that argue about a proposal before the votes are read."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ConfigError("[agents].validators is a list of stances")
    names = [str(name) for name in raw]
    unknown = sorted(set(names) - set(STANCES))
    if unknown:
        raise ConfigError(f"[agents].validators: {unknown} is not a stance; have {list(STANCES)}")
    if len(set(names)) != len(names):
        raise ConfigError("[agents].validators asks each stance once")
    return tuple(names)


def _confluence(raw: dict[str, Any]) -> Matrix:
    """[confluence]: which roles must agree with the direction, and how votes are counted."""
    table = raw.get("confluence")
    if table is None:
        return Matrix()
    if not isinstance(table, dict):
        raise ConfigError("[confluence] must be a table")
    unknown = sorted(set(table) - set(CONFLUENCE_KEYS))
    if unknown:
        raise ConfigError(f"[confluence]: unknown {unknown}; have {list(CONFLUENCE_KEYS)}")
    required = _names(table.get("required"), "required")
    reading = tuple(r for r in ASKABLE if r != "technical_analyst")
    stray = sorted(set(required) - set(reading))
    if stray:
        raise ConfigError(
            f"[confluence].required: {stray} does not read a context; have {list(reading)}"
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
    if "min_reward_risk" in table and exit_rule.take_profit_sigma_frac is None:
        raise ConfigError(
            "[targets].min_reward_risk needs [exit].take_profit_sigma_frac; "
            "a ratio without a take profit is not a ratio"
        )
    try:
        return Targets(
            min_net_bps=_dec(table, "min_net_bps") if "min_net_bps" in table else None,
            min_reward_risk=(
                _dec(table, "min_reward_risk") if "min_reward_risk" in table else None
            ),
            max_round_trips_per_day=(
                _count(table, "max_round_trips_per_day")
                if "max_round_trips_per_day" in table
                else None
            ),
        )
    except TargetError as exc:
        raise ConfigError(str(exc)) from exc


def _names(raw: Any, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ConfigError(f"[confluence].{key} is a list of names")
    return tuple(str(name) for name in raw)


def _count(table: dict[str, Any], key: str) -> int:
    value = table.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{key} is a non-negative integer")
    return value


def _screen(raw: Any) -> dict[str, float]:
    """[agents.screen]: the screener's two thresholds, both the operator's."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("[agents.screen] is a table")
    out: dict[str, float] = {}
    for key in ("min_prints_per_minute", "volatile_above_bps"):
        if key in raw:
            out[key] = float(_dec(raw, key))
    unknown = sorted(set(raw) - set(out))
    if unknown:
        raise ConfigError(f"[agents.screen]: unknown {unknown}")
    return out


def _live(raw: dict[str, Any], venue_key: str) -> LiveSettings | None:
    table = raw.get("live")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[live] must be a table")
    leaked = [k for k in table if any(word in k.lower() for word in SECRET_LIKE)]
    if leaked:
        raise ConfigError(
            f"[live] carries {leaked}; keys come from the environment, never from a file"
        )
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


def _flag(table: dict[str, Any], key: str) -> bool:
    value = table.get(key, False)
    if not isinstance(value, bool):
        raise ConfigError(f"[live].{key} is true or false")
    return value


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


def _build_strategy(raw: dict[str, Any], base: Path | None) -> Strategy:
    """A decision core named by `import = "pkg.mod:Class"` or `file = "x.py"` + `class`.

    The package ships no strategy of its own. `examples/strategies/` holds two that a run can
    point at by file; they are examples, not recommendations.
    """
    params = raw.get("params", {})
    if not isinstance(params, dict):
        raise ConfigError("[strategy.params] must be a table")
    target = raw.get("import")
    file = raw.get("file")
    if isinstance(target, str) and target:
        if ":" not in target:
            raise ConfigError("[strategy].import is 'package.module:Class'")
        module_name, _, class_name = target.partition(":")
        try:
            cls = getattr(importlib.import_module(module_name), class_name)
        except (ImportError, AttributeError) as exc:
            raise ConfigError(f"cannot import strategy {target!r}: {exc}") from exc
        label = target
    elif isinstance(file, str) and file:
        class_name = str(raw.get("class", ""))
        if not class_name:
            raise ConfigError("[strategy].file needs [strategy].class naming the core")
        cls = _class_from_file(_resolve(Path(file), base), class_name)
        label = f"{file}:{class_name}"
    else:
        raise ConfigError(
            "[strategy] names the core: import = 'package.module:Class', or "
            "file = 'path/to/core.py' with class = 'Class'"
        )
    try:
        instance = cls(**params)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"strategy {label!r}: {exc}") from exc
    if not hasattr(instance, "on_bar") or not hasattr(instance, "name"):
        raise ConfigError(f"{label!r} is not a Strategy: it needs `name` and `on_bar`")
    return instance  # type: ignore[no-any-return]


def _resolve(file: Path, base: Path | None) -> Path:
    # Relative to the config file first, so a run directory carries its own core.
    if file.is_absolute() or base is None:
        return file
    beside = base / file
    return beside if beside.is_file() else file


def _class_from_file(path: Path, class_name: str) -> Any:
    if not path.is_file():
        raise ConfigError(f"strategy file {str(path)!r} does not exist")
    spec = importlib.util.spec_from_file_location(f"strategy_file_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"cannot load strategy file {str(path)!r}")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: a dataclass in the file looks itself up in sys.modules.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(spec.name, None)
        raise ConfigError(f"strategy file {str(path)!r} failed to load: {exc}") from exc
    if not hasattr(module, class_name):
        raise ConfigError(f"{str(path)!r} has no class {class_name!r}")
    return getattr(module, class_name)


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] table is required")
    return value


def _dec(table: dict[str, Any], key: str) -> Decimal:
    if key not in table:
        raise ConfigError(f"missing {key}")
    value = table[key]
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a number")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        try:
            return Decimal(str(value))
        except ArithmeticError as exc:
            raise ConfigError(f"{key} is not a number: {value!r}") from exc
    raise ConfigError(f"{key} must be a number")


def _int(table: dict[str, Any], key: str) -> int:
    if key not in table:
        raise ConfigError(f"missing {key}")
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    return value
