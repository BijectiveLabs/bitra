"""`strategy.toml`: everything a run needs, declared once, none of it in the logic."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.data.sources.config import DataConfigError, parse_duration
from trading_research_agents.execution.confluence import STANCES, ConfluenceError, Matrix
from trading_research_agents.execution.conviction import Conviction, ConvictionError, Tier
from trading_research_agents.execution.cost import CostError, FundingSchedule
from trading_research_agents.execution.desk import Desk, DeskError
from trading_research_agents.execution.mandate import Mandate, MandateError
from trading_research_agents.execution.regime import REGIME_KEYS, RegimeError, RegimeRule
from trading_research_agents.execution.stops import MODES, StopError, VolatilityExit
from trading_research_agents.execution.targets import TargetError, Targets
from trading_research_agents.execution.trigger import Trigger, TriggerError
from trading_research_agents.execution.universe import Universe, UniverseError
from trading_research_agents.strategy.core import Strategy
from trading_research_agents.strategy.exits import ExitRule
from trading_research_agents.trading.canary import CanaryError, CanaryLimits
from trading_research_agents.trading.controls import ControlError, Limits

# Keys never live in a config file. A setting whose name reads like a credential is refused by
# name - but only after the table's own settings are accounted for, because `key_ip_unbound_ok` is
# a flag about a key rather than a key.
SECRET_LIKE = ("api_key", "api_secret", "secret", "key", "token", "password", "passphrase")


def _no_secrets(unknown: Sequence[str], where: str) -> None:
    """Refuse a setting nobody declared that reads like a credential, before anything else."""
    leaked = [k for k in unknown if any(word in k.lower() for word in SECRET_LIKE)]
    if leaked:
        raise ConfigError(f"{where} carries {leaked}; keys come from the environment, never a file")


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
    # The base position said in quote currency instead of base units, for a desk whose instruments
    # are not worth the same. Exactly one of size and size_quote is declared.
    size_quote: Decimal | None = None
    # `size_step` written as a table, one increment per instrument. Venues step BTC and ETH
    # differently, and a size off the venue's step is either refused there or quietly rounded by
    # the client library - neither of which the cost gate priced.
    size_steps: Mapping[str, Decimal] = field(default_factory=dict)
    # Higher timeframes the technical analyst reads as context, in seconds. The decision, the
    # trigger and the exits stay on bar_interval_s.
    context_intervals: tuple[int, ...] = ()

    def step_for(self, instrument: str) -> Decimal:
        return self.size_steps.get(instrument, self.size_step)

    def fixed_size(self) -> Decimal:
        """The base size for a path that trades one size throughout: a replay, paper, a session.

        A quote size is converted at each cycle's own mid, and a replay has no cycle to convert it
        at. Picking a price on the operator's behalf would make the replay trade a size nobody
        declared, so it is refused by name instead.
        """
        if self.size_quote is not None:
            raise ConfigError(
                "[run].size_quote is converted at each cycle's mid, which only `tra run` has; "
                "a replay, paper or live session trades one size in base units, so declare "
                "[run].size for it"
            )
        return self.size


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


@dataclass(frozen=True)
class RoleModel:
    """One role's model, after the defaults in [agents] and its own overrides are settled."""

    role: str
    provider: str
    model: str | None
    base_url: str | None
    max_output_tokens: int | None
    call_timeout_s: int
    max_retries: int
    fallback: tuple[str, ...] = ()
    # How hard this role is asked to think, where the vendor takes such a thing. Undeclared means
    # the vendor's own default, which is what every run did before this existed.
    effort: str | None = None


@dataclass(frozen=True)
class AgentSettings:
    """[agents]: which model decides, how often it may be asked, and what that may cost."""

    role: str
    provider: str
    model: str | None
    base_url: str | None
    mechanism: str
    # Every ceiling below is optional, and undeclared means counted rather than capped. A role cut
    # off mid-answer costs the call and returns a worse decision, so a ceiling is something an
    # operator asks for deliberately, not something this software assumes on their behalf.
    token_budget: int | None = None
    max_calls_per_hour: int | None = None
    min_interval_s: int = 0
    call_timeout_s: int = 60
    lookback_bars: int = 60
    min_confidence: float = 0.5
    pricing: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    decide_every_bars: int = 1
    max_output_tokens: int | None = None
    max_retries: int = 2
    effort: str | None = None
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
    # [agents].fallback: `provider:model` entries every role inherits, tried in order when the
    # provider above cannot be reached. A rate limit is a dependency failing, not a reason to stop.
    fallback: tuple[str, ...] = ()
    # A declared validator that no model answered: refuse the trade (a check you asked for did not
    # happen) or continue on the votes that did come back.
    on_missing_validator: str = "refuse"
    # How long a role waits for a provider that said "try again in N seconds" before the next link.
    max_retry_wait_s: float = 15.0
    # A reading analyst's answer is reused while its brief's figures are unchanged, for at most
    # this long. New data always asks again. Zero turns reuse off.
    reuse_reading_s: float = 3600.0

    def for_role(self, role: str) -> RoleModel:
        """What this role is asked with: its own model where declared, otherwise the default."""
        table = self.roles.get(role, {})
        return RoleModel(
            role=role,
            provider=str(table.get("provider", self.provider)),
            model=(str(table["model"]) if table.get("model") else self.model),
            base_url=(str(table["base_url"]) if table.get("base_url") else self.base_url),
            max_output_tokens=(
                int(table["max_output_tokens"])
                if table.get("max_output_tokens") is not None
                else self.max_output_tokens
            ),
            call_timeout_s=int(table.get("call_timeout_s", self.call_timeout_s)),
            max_retries=int(table.get("max_retries", self.max_retries)),
            fallback=tuple(str(name) for name in (table.get("fallback") or self.fallback)),
            effort=(
                str(table["effort"])
                if table.get("effort")
                # A role that reads a table code computed does not need to think long. Only where
                # an effort is declared at all: a model with no reasoning setting refuses the field.
                else (LOW_EFFORT if role in TABLE_READERS and self.effort else self.effort)
            ),
        )


# Roles whose brief is a table of figures code already computed.
TABLE_READERS = (
    "market_screener",
    "macro_analyst",
    "onchain_analyst",
    "sentiment_analyst",
    "validator",
)
LOW_EFFORT = "low"


def _reuse_window(raw: Any) -> float:
    """[agents].reuse_reading_for: a duration such as "1h", or 0 / "off" for no reuse."""
    if raw in (0, "0", "off", False):
        return 0.0
    try:
        return parse_duration(str(raw))
    except DataConfigError as exc:
        raise ConfigError(f"[agents].reuse_reading_for: {exc}") from exc


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
    # What has to have happened before the roster is paid to have an opinion at all.
    trigger: Trigger = field(default_factory=Trigger)
    # When the auditors are asked without anyone typing the command.
    audit: AuditPolicy = field(default_factory=AuditPolicy)
    # What this desk watches, when it watches more than [run].instrument.
    universe: Universe = field(default_factory=Universe)
    # What it may be carrying across all of them at once.
    desk: Desk = field(default_factory=Desk)
    # What a cycle may do with an admitted intent: keep it, export it, or send it.
    execution: Mandate = field(default_factory=Mandate)
    # Where trending and ranging begin, and whether a setup has to fit the regime measured.
    regime: RegimeRule = field(default_factory=RegimeRule)


def load_strategy_config(path: Path) -> StrategyConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from exc
    return parse_strategy_config(raw, base=path.resolve().parent)


def parse_strategy_config(raw: dict[str, Any], *, base: Path | None = None) -> StrategyConfig:
    strategy_raw = _table(raw, "strategy")
    # [exit] and [controls] may be left out: both have standard defaults on an agent desk.
    exit_raw = dict(raw.get("exit") or {})
    # The names a desk written before 0.6.0 used. They said sigma and meant a fraction of price.
    for old, new in (
        ("stop_sigma_frac", "stop_frac"),
        ("take_profit_sigma_frac", "take_profit_frac"),
    ):
        if old in exit_raw and new not in exit_raw:
            exit_raw[new] = exit_raw.pop(old)
    run_raw = _table(raw, "run")
    controls_raw = dict(raw.get("controls") or {})
    agents = _agents(raw, strategy_raw)
    # An agent desk measures its costs from the book and states its mechanism in [agents], so the
    # research figures a code core must declare have standard defaults here.
    agent = agents is not None
    if agent and "mode" not in exit_raw and "stop_frac" not in exit_raw:
        exit_raw["mode"] = "analyst"
    volatility = _volatility_exit(exit_raw)
    if volatility is not None and agents is None:
        raise ConfigError(
            f'[exit].mode = "{volatility.mode}" is placed by the agent cycle on each decision; '
            'a scripted strategy is simulated bar by bar on mode = "fixed"'
        )
    strategy = _build_strategy(strategy_raw, base) if agents is None else _placeholder(agents)
    if "stop_frac" not in exit_raw:
        raise ConfigError(
            '[exit].stop_frac is the stop on every trade; or mode = "volatility" on an agent desk'
        )
    exit_rule = ExitRule(
        stop_frac=_dec(exit_raw, "stop_frac"),
        timeout_bars=_or_int(exit_raw, "timeout_bars", _four_hours(run_raw) if agent else None),
        mechanism_bound=str(exit_raw.get("mechanism_bound", AGENT_EXIT_BOUND if agent else "")),
        take_profit_frac=(
            _dec(exit_raw, "take_profit_frac") if "take_profit_frac" in exit_raw else None
        ),
        slip_bps=_dec(exit_raw, "slip_bps") if "slip_bps" in exit_raw else Decimal("5.0"),
        volatility=volatility,
    )
    limits = _limits(controls_raw)
    kill = controls_raw.get("kill_switch")
    if limits.on_drawdown == "flatten" and not kill:
        raise ConfigError(
            '[controls].on_drawdown = "flatten" works through the kill switch; declare '
            '[controls].kill_switch = "KILL"'
        )
    run = RunSettings(
        instrument=str(run_raw.get("instrument", "")),
        venue_key=str(run_raw.get("venue_key", "")),
        bar_interval_s=_or_int(run_raw, "bar_interval_s", 60),
        size=_dec(run_raw, "size") if "size" in run_raw else Decimal(0),
        size_quote=_dec(run_raw, "size_quote") if "size_quote" in run_raw else None,
        spread_cost_bps=_or_dec(run_raw, "spread_cost_bps", "3.0" if agent else None),
        equity=_dec(run_raw, "equity") if "equity" in run_raw else None,
        latency_ms=_or_dec(run_raw, "latency_ms", "50" if agent else None),
        slippage_bps=_or_dec(run_raw, "slippage_bps", "1.0" if agent else None),
        settlement_interval_s=_or_int(run_raw, "settlement_interval_s", 28_800),
        **_steps(run_raw.get("size_step")),
        context_intervals=_context_intervals(
            run_raw.get("context_intervals"), _or_int(run_raw, "bar_interval_s", 60)
        ),
    )
    if not run.instrument or not run.venue_key:
        raise ConfigError("[run] names the instrument and the venue_key (venue/channel)")
    _sized_once(run)
    universe = _universe(raw)
    if universe.discover and limits.max_position_quote is None:
        # [controls].max_position counts units: ten units of BTC and ten of DOGE are not one cap.
        raise ConfigError(
            "[universe].discover needs [controls].max_position_quote, the position cap in quote "
            "currency; [controls].max_position counts units and would cap a coin priced in cents "
            "at a few cents"
        )
    if universe.discover and run.size_quote is None:
        # One base size across instruments priced from cents to tens of thousands is no size at
        # all; a quote size converts at each instrument's own price.
        raise ConfigError(
            "[universe].discover needs [run].size_quote, the size in quote currency, because "
            "discovered instruments are priced too differently for one size in base units"
        )
    if run.size_steps:
        unstepped = [
            name
            for name in dict.fromkeys((run.instrument, *universe.instruments))
            if name not in run.size_steps
        ]
        if unstepped:
            raise ConfigError(
                f"[run].size_step is a table and has no step for {unstepped}; every instrument "
                "this desk can trade needs its venue's increment"
            )
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
        trigger=_trigger(raw),
        audit=_audit(raw),
        universe=universe,
        desk=_desk(raw),
        regime=_regime(raw),
        conviction=_conviction(raw),
        execution=_mandate(raw, run.venue_key),
    )


def _steps(raw: Any) -> dict[str, Any]:
    """`size_step` as one number for every instrument, or a table of one per instrument."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        step = _dec({"size_step": raw}, "size_step")
        if step <= 0:
            raise ConfigError("[run].size_step is positive")
        return {"size_step": step}
    steps = {str(name): _dec(raw, name) for name in raw}
    bad = sorted(name for name, step in steps.items() if step <= 0)
    if not steps:
        raise ConfigError("[run].size_step as a table names at least one instrument")
    if bad:
        raise ConfigError(f"[run].size_step entries are positive increments; not {bad}")
    return {"size_steps": steps}


def _sized_once(run: RunSettings) -> None:
    """One way of saying the base size, never two that could disagree."""
    if run.size < 0 or (run.size_quote is not None and run.size_quote <= 0):
        raise ConfigError("[run].size and [run].size_quote are positive")
    declared = [name for name, v in (("size", run.size), ("size_quote", run.size_quote)) if v]
    if len(declared) != 1:
        raise ConfigError(
            "[run] declares the base size once: size in base units, or size_quote in quote "
            "currency for a desk whose instruments are not worth the same "
            f"({declared or 'neither'} is declared)"
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
# The llm layer owns this vocabulary; it is repeated here because the gates never import it, and
# repeated in one place so a bad spelling is refused while reading the file rather than mid-cycle.
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
ROLE_KEYS = (
    "provider",
    "model",
    "base_url",
    "max_output_tokens",
    "call_timeout_s",
    "max_retries",
    # `provider:model` entries tried in order when the one above cannot be reached.
    "fallback",
    # minimal | low | medium | high | xhigh | max, mapped onto whatever the vendor takes.
    "effort",
)


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
        _effort(table.get("effort"))
        out[role] = dict(table)
    return out


def _choice(
    table: dict[str, Any], key: str, allowed: tuple[str, ...], default: str, where: str
) -> str:
    value = str(table.get(key, default))
    if value not in allowed:
        raise ConfigError(f"{where}.{key} is one of {list(allowed)}")
    return value


def _effort(raw: Any) -> str | None:
    """`effort`, where a vendor takes one. Undeclared leaves the vendor's own default alone."""
    if raw is None:
        return None
    if not isinstance(raw, str) or raw not in EFFORTS:
        raise ConfigError(f"effort is one of {list(EFFORTS)}")
    return raw


def _agents(raw: dict[str, Any], strategy_raw: dict[str, Any]) -> AgentSettings | None:
    role = strategy_raw.get("agent")
    if role is None:
        return None
    if role != "technical_analyst":
        raise ConfigError("[strategy].agent names a decision role; only technical_analyst decides")
    if strategy_raw.get("file") or strategy_raw.get("import"):
        raise ConfigError("[strategy] names either a scripted strategy or an agent, not both")
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
        token_budget=_int(table, "token_budget") if "token_budget" in table else None,
        max_calls_per_hour=(
            _int(table, "max_calls_per_hour") if "max_calls_per_hour" in table else None
        ),
        min_interval_s=int(table.get("min_interval_s", 0)),
        call_timeout_s=int(table.get("call_timeout_s", 60)),
        lookback_bars=int(table.get("lookback_bars", 60)),
        min_confidence=float(confidence),
        pricing=pricing,
        decide_every_bars=int(table.get("decide_every_bars", 1)),
        max_output_tokens=(
            int(table["max_output_tokens"]) if table.get("max_output_tokens") is not None else None
        ),
        max_retries=int(table.get("max_retries", 2)),
        effort=_effort(table.get("effort")),
        magnitude_bps=_dec(table, "magnitude_bps") if "magnitude_bps" in table else None,
        screen=_screen(table.get("screen")),
        roles=_roles(table.get("roles")),
        analysts=_analysts(table.get("analysts")),
        validators=_validators(table.get("validators")),
        on_missing_validator=_choice(
            table, "on_missing_validator", ("refuse", "continue"), "refuse", "[agents]"
        ),
        max_retry_wait_s=float(table.get("max_retry_wait_s", 15.0)),
        reuse_reading_s=_reuse_window(table.get("reuse_reading_for", "1h")),
        fallback=_fallback(table.get("fallback")),
    )


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
    "environment",
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


def _fallback(raw: Any) -> tuple[str, ...]:
    """`fallback = ["groq:openai/gpt-oss-20b", ...]`: other models, in the order to try them."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ConfigError("fallback is a list of provider:model strings")
    names = [str(name) for name in raw]
    for name in names:
        # The shape only: this layer knows nothing about which providers exist, and the CLI checks
        # the names against the registry where both are in reach.
        provider_name, _, model = name.partition(":")
        if not provider_name.strip() or not model.strip():
            raise ConfigError(f"{name!r} is not a fallback; write it as provider:model")
    if len(set(names)) != len(names):
        raise ConfigError("a fallback names each model once")
    return tuple(names)


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
        )
    except TargetError as exc:
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


def _names(raw: Any, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ConfigError(f"[confluence].{key} is a list of names")
    return tuple(str(name) for name in raw)


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
        )
    except RegimeError as exc:
        raise ConfigError(str(exc)) from exc


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


EXIT_KEYS = (
    "mode",
    "stop_frac",
    "take_profit_frac",
    "timeout_bars",
    "mechanism_bound",
    "slip_bps",
)
VOLATILITY_KEYS = ("stop_atr", "reward_to_risk", "min_stop_frac", "max_stop_frac", "atr_bars")
ANALYST_KEYS = ("min_reward_to_risk", "max_reward_to_risk")


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
        )
    except ControlError as exc:
        raise ConfigError(f"[controls]: {exc}") from exc


def _or_dec(table: dict[str, Any], key: str, default: str | None) -> Decimal:
    """The declared figure, or the default; no default means the figure is required."""
    if key in table or default is None:
        return _dec(table, key)
    return Decimal(default)


def _four_hours(run_raw: dict[str, Any]) -> int:
    # The default hold is a span of time, so it is the same four hours on any decision timeframe.
    interval = _or_int(run_raw, "bar_interval_s", 60)
    return max(1, 14_400 // max(interval, 1))


def _or_int(table: dict[str, Any], key: str, default: int | None) -> int:
    if key in table or default is None:
        return _int(table, key)
    return default


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
