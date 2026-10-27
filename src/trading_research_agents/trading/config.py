"""`strategy.toml`: everything a run needs, declared once, none of it in the logic."""

from __future__ import annotations

import difflib
import importlib
import importlib.util
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.execution.confluence import Matrix
from trading_research_agents.execution.conviction import Conviction
from trading_research_agents.execution.cost import FundingSchedule
from trading_research_agents.execution.desk import Desk
from trading_research_agents.execution.kelly import SizingRule
from trading_research_agents.execution.mandate import Mandate
from trading_research_agents.execution.regime import REGIME_KEYS, RegimeRule
from trading_research_agents.execution.targets import Targets
from trading_research_agents.execution.trigger import Trigger
from trading_research_agents.execution.universe import Universe
from trading_research_agents.strategy.core import Strategy
from trading_research_agents.strategy.exits import ExitRule
from trading_research_agents.trading.canary import CanaryLimits
from trading_research_agents.trading.config_agents import (
    AGENTS_KEYS,
    ROLE_KEYS,
    AgentSettings,
    _agents,
)
from trading_research_agents.trading.config_gates import (
    AUDIT_KEYS,
    CONFLUENCE_KEYS,
    CONVICTION_KEYS,
    DESK_KEYS,
    SIZING_KEYS,
    TARGET_KEYS,
    TRIGGER_KEYS,
    UNIVERSE_KEYS,
    AuditPolicy,
    _audit,
    _confluence,
    _context_intervals,
    _conviction,
    _desk,
    _regime,
    _sizing,
    _targets,
    _trigger,
    _universe,
)
from trading_research_agents.trading.config_values import (
    ConfigError,
    _dec,
    _four_hours,
    _or_dec,
    _or_int,
    _table,
)
from trading_research_agents.trading.config_venue import (
    AGENT_EXIT_BOUND,
    ANALYST_KEYS,
    CONTROLS_KEYS,
    EXECUTION_KEYS,
    EXIT_KEYS,
    LIVE_KEYS,
    VOLATILITY_KEYS,
    LiveSettings,
    _canary,
    _limits,
    _live,
    _mandate,
    _volatility_exit,
    load_funding,
)
from trading_research_agents.trading.controls import Limits


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
class StrategyConfig:
    strategy: Strategy
    exit_rule: ExitRule
    limits: Limits
    kill_switch: Path | None
    run: RunSettings
    registration_seal: str | None
    # A file whose presence holds every new entry and keeps every open position looked after.
    pause_switch: Path | None = Path("PAUSE")
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
    # How far the desk's own record may lower the declared risk per trade.
    sizing: SizingRule = field(default_factory=SizingRule)


def load_strategy_config(path: Path) -> StrategyConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from exc
    return parse_strategy_config(raw, base=path.resolve().parent)


# Every table strategy.toml may hold. A name outside this list is a typo or a setting from another
# file, and either way it would be ignored, so it is refused: [sizng] silently left sizing off.
TABLES = (
    "strategy",
    "exit",
    "controls",
    "run",
    "data",
    "live",
    "canary",
    "agents",
    "confluence",
    "targets",
    "trigger",
    "audit",
    "universe",
    "desk",
    "regime",
    "conviction",
    "execution",
    "sizing",
)


def parse_strategy_config(raw: dict[str, Any], *, base: Path | None = None) -> StrategyConfig:
    unknown = sorted(set(raw) - set(TABLES))
    if unknown:
        hints = [
            f"[{name}] (did you mean [{close[0]}]?)" if close else f"[{name}]"
            for name in unknown
            for close in [difflib.get_close_matches(name, TABLES, n=1)]
        ]
        raise ConfigError(
            f"strategy.toml: {', '.join(hints)} is not a table this file reads; it has "
            f"{', '.join(f'[{t}]' for t in TABLES)}"
        )
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
        pause_switch=Path(str(controls_raw.get("pause_switch", "PAUSE"))),
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
        sizing=_sizing(raw),
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


# What each table accepts, the same lists the loader refuses an unknown key against, so a proposed
# setting can be checked before anyone reads it.
SETTINGS: dict[str, tuple[str, ...]] = {
    "agents": AGENTS_KEYS,
    "agents.roles": ROLE_KEYS,
    "audit": AUDIT_KEYS,
    "confluence": CONFLUENCE_KEYS,
    "controls": CONTROLS_KEYS,
    "conviction": CONVICTION_KEYS,
    "desk": DESK_KEYS,
    "execution": EXECUTION_KEYS,
    "exit": EXIT_KEYS + VOLATILITY_KEYS + ANALYST_KEYS,
    "live": LIVE_KEYS,
    "regime": REGIME_KEYS,
    "sizing": SIZING_KEYS,
    "targets": TARGET_KEYS,
    "trigger": TRIGGER_KEYS,
    "universe": UNIVERSE_KEYS,
}
