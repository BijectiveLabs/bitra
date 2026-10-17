"""One cycle: what the engine saw, what the agents said, what the cost gate decided.

The order is fixed and short. Code takes a snapshot, which costs nothing, and asks the trigger
whether anything has happened worth paying a model to look at. Code builds the briefs. Agents read
the briefs, in parallel, and answer inside a schema. Code prices the round trip on the book it is
holding, sizes the position from the agreement and then from what the account can afford to lose
at the stop, and compares what is left with the edge the analyst claimed. Only then is there an
intent, and in the default mode that intent goes to a file rather than to a venue.

Two steps here belong to a risk manager and are deliberately not a model's to make. How much goes
behind an idea comes from the conviction ladder; how much the account may lose if the stop is hit
comes from `[controls].max_risk_per_trade_bps`. Both are arithmetic over the operator's own
declarations, so neither can be talked out of.

Everything the cycle touched stays in the cycle's directory, so an auditor can replay the
reasoning without asking anyone to remember it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from itertools import islice
from pathlib import Path
from typing import Any

from trading_research_agents.agents import (
    macro_analyst,
    onchain_analyst,
    sentiment_analyst,
    technical_analyst,
    validator,
)
from trading_research_agents.agents.alarms import DAILY_LOSS_MARK, DRAWDOWN_MARK
from trading_research_agents.agents.briefs import (
    BriefError,
    context_brief,
    cost_brief,
    data_brief,
    declared_state,
    interval_name,
    technical_brief,
)
from trading_research_agents.agents.cited import survived
from trading_research_agents.agents.parallel import FanOut, Task, run_tasks
from trading_research_agents.agents.progress import (
    DONE,
    FAILED,
    HELD,
    REFUSED,
    RUNNING,
    SKIPPED,
    Watch,
    announce,
    silent,
    timed,
)
from trading_research_agents.agents.roles import RoleError, prompt
from trading_research_agents.agents.scope import brief_for, crossings, dropped
from trading_research_agents.agents.validator.tools import checked, figures_of, validator_brief
from trading_research_agents.data.schema import Direction
from trading_research_agents.data.series import (
    Event,
    Series,
    SeriesError,
    load_news,
    load_readable,
    read_events,
)
from trading_research_agents.data.sources.base import Fetched
from trading_research_agents.data.sources.chain import ChainExhaustedError, SourceChain
from trading_research_agents.envelope.model import Channel, Envelope
from trading_research_agents.execution.confluence import STANCES, Confluence, Matrix, confluent
from trading_research_agents.execution.conviction import (
    Conviction,
    Stake,
    score_of,
    stake_for,
)
from trading_research_agents.execution.desk import Desk, DeskVerdict, Open, within_desk
from trading_research_agents.execution.mandate import MODES as MANDATE_MODES
from trading_research_agents.execution.pretrade import PreTradeVerdict, admit
from trading_research_agents.execution.regime import RegimeRule, misfit
from trading_research_agents.execution.risk import RiskVerdict, within_risk
from trading_research_agents.execution.stops import Placement, StopError, VolatilityExit
from trading_research_agents.execution.targets import (
    Targets,
    TargetVerdict,
    edge_multiple,
    intents_today,
    research_bps,
    within_targets,
)
from trading_research_agents.execution.trigger import (
    Trigger,
    Wake,
    last_seen,
    spread_of,
    worth_asking,
)
from trading_research_agents.llm.completer import Completer
from trading_research_agents.llm.usage import BudgetSpentError, UsageLedger
from trading_research_agents.records import (
    COMPACT,
    append_index,
    append_line,
    build_index,
    has_index,
    index_row,
    recent,
    sha256_of,
    write_gzip_json,
    write_json,
)
from trading_research_agents.store.cycles import SUMMARY, CycleDir
from trading_research_agents.store.reuse import key_of, lookup, remember
from trading_research_agents.store.snapshot import (
    INPUTS,
    Snapshot,
    cycle_id,
    inputs_record,
    take_snapshot,
)
from trading_research_agents.trading.capital import (
    EQUITY_PEAK,
    day_loss_bps,
    drawdown_bps,
    sizing_equity,
)

# The third mode sends orders, and it only exists when the caller hands the cycle something to
# send them with. That hand is built by the CLI, and only after the mandate has cleared.
MODES = MANDATE_MODES
TARGETS = {"long": Direction.LONG, "short": Direction.SHORT}
# Snapshots looked through for the last open interest; a desk that slept longer starts afresh.
OPEN_INTEREST_LOOK_BACK = 12
# The analysts that only read and label. They run beside the technical analyst, they never place
# anything, and each one's answer is recorded whether or not the operator gave it a source.
# The shelf files that feed each reading analyst, for saying where to declare one.
CLASSES_OF: dict[str, tuple[str, ...]] = {
    macro_analyst.ROLE: ("macro",),
    onchain_analyst.ROLE: ("onchain", "derivatives"),
    sentiment_analyst.ROLE: ("sentiment",),
}
READING: dict[str, Any] = {
    macro_analyst.ROLE: macro_analyst,
    onchain_analyst.ROLE: onchain_analyst,
    sentiment_analyst.ROLE: sentiment_analyst,
}


# What a contract market has and a spot pair does not.
CONTRACT_KINDS = ("funding", "open_interest")


class CycleError(ValueError):
    """The cycle cannot run as configured, or cannot run at all on what the engine returned."""


@dataclass(frozen=True)
class CycleConfig:
    """Everything one cycle needs, and nothing it should decide for itself."""

    instrument: str
    envelope: Envelope
    venue_key: str
    chains: Mapping[str, SourceChain]
    completer_for: Callable[[str], Completer]
    usage: UsageLedger
    root: Path
    mechanism: str
    magnitude_bps: Decimal
    # The base position, in base units. A desk that watches several instruments says it in quote
    # currency instead - 0.01 is eight hundred dollars of one and twenty-seven of another - and the
    # cycle converts it at its own mid.
    size: Decimal = Decimal(0)
    size_quote: Decimal | None = None
    bar_interval_s: int = 60
    lookback_bars: int = 60
    interval_s: int = 60
    settlement_interval_s: int = 28_800
    latency_slippage_bps: Decimal = Decimal(0)
    margin_bps: Decimal = Decimal(0)
    min_confidence: float = 0.6
    mode: str = "sandbox"
    # Two gates the operator declares: whether the context agrees with the direction, and whether
    # the trade is the shape this desk said it wanted. Both are applied by code, never by a model.
    matrix: Matrix = field(default_factory=Matrix)
    targets: Targets = field(default_factory=Targets)
    # How much goes behind the idea, from the same declarations the matrix reads. Left undeclared,
    # every admitted cycle trades the operator's base size, which is what it did before.
    conviction: Conviction = field(default_factory=Conviction)
    max_position: Decimal | None = None
    # The position cap in quote currency, converted at this cycle's mid; the smaller cap applies.
    max_position_quote: Decimal | None = None
    equity: Decimal | None = None
    size_step: Decimal = Decimal("0.000001")
    reward_to_risk: Decimal | None = None
    # [trigger]: what has to have happened before anyone is paid to have an opinion. Undeclared,
    # every scheduled cycle asks the whole roster, which is what it did before.
    trigger: Trigger = field(default_factory=Trigger)
    # [controls].max_risk_per_trade_bps, applied to the size the conviction ladder arrived at. The
    # session runtime has always enforced this; a cycle that places its own orders has to as well.
    max_risk_per_trade_bps: Decimal | None = None
    # [desk]: what the whole book may carry at once, and where to look for what is already open.
    # The caller decides where to look, because only the caller knows: the venue in venue mode, the
    # intents on disk everywhere else.
    desk: Desk = field(default_factory=Desk)
    held: Callable[[], tuple[tuple[Open, ...], str]] | None = None
    # Venue mode without adopt_open_position: an instrument already open at the venue waits for its
    # exits rather than being decided on again, since the venue hand would refuse the entry.
    wait_while_open: bool = False
    # Venue mode: what the account actually holds, read before anyone is asked. Sizing and risk use
    # the smaller of this and [run].equity, and the day's loss is measured from it.
    account: Callable[[], Any] | None = None
    daily_loss_limit_bps: Decimal | None = None
    # [controls].max_drawdown_bps, from the desk's highest equity; with on_drawdown = "flatten"
    # the kill switch file is written, and the next pass flattens and stops the run.
    max_drawdown_bps: Decimal | None = None
    drawdown_kill: Path | None = None
    # What the models have already cost since this desk last wrote an intent. A day of refusals is
    # paid for by whatever trade the day produces, so the caller carries the figure across cycles.
    carried_spend: Decimal = Decimal(0)
    # `tra run --now`: ask the roster whatever the trigger would have said. An override belongs to
    # the operator, and it is written into the cycle rather than left to be inferred.
    forced: bool = False
    # The exit rule as declared, so the cycle can price the stop and the take profit for the venue.
    # Code computes them from the operator's own fractions; no model is asked where a stop goes.
    stop_frac: Decimal | None = None
    take_frac: Decimal | None = None
    # [exit].mode = "volatility": each trade's stop measured from the decision timeframe's range.
    volatility_exit: VolatilityExit | None = None
    # [agents].validators: the stances to ask once there is a proposal worth arguing about. Each
    # stance is one more call, so an operator turns them on deliberately.
    validators: tuple[str, ...] = ()
    # refuse: a declared stance that no model answered stops the trade. continue: trade on the votes
    # that came back. A lost check never adds size either way.
    on_missing_validator: str = "refuse"
    # How long a reading analyst's answer stands while its brief's figures are unchanged; 0 is off.
    reuse_reading_s: float = 0.0
    # Higher timeframes read as context, in seconds; the decision stays on interval_s.
    context_intervals: tuple[int, ...] = ()
    # How long a position is kept: the analyst's horizon, or [exit].timeout_bars. The cost gate
    # prices funding over the longer of what the desk will hold, not the shorter.
    hold_limit: str = "horizon"
    timeout_bars: int | None = None
    regime: RegimeRule = field(default_factory=RegimeRule)
    # What actually sends an order, when the operator is in venue mode and the mandate cleared.
    place: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    # The instrument is a spot market on the venue orders go to. A short there needs a borrow, so it
    # is refused by code before it is priced; funding and open interest belong to contracts, so
    # they are not asked for, rather than asked for and refused every cycle.
    spot: bool = False
    # A reading analyst is asked only when the roster names it. `tra run` names the ones with a
    # declared source, because paying a model to say it was given nothing is waste; an operator who
    # wants a role asked anyway, on notes alone, names it in [agents].analysts.
    data_options: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    series: Mapping[str, tuple[Path, ...]] = field(default_factory=dict)
    news: tuple[Path, ...] = ()
    analysts: tuple[str, ...] = (technical_analyst.ROLE,)
    max_workers: int = 4
    # Progress, on its own wire. A watcher sees the steps as they happen and can change none of
    # them; the default is nobody watching.
    watch: Watch = silent
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.instrument.strip():
            raise CycleError("a cycle names its instrument")
        if technical_analyst.ROLE not in self.analysts:
            raise CycleError(f"a cycle asks {technical_analyst.ROLE}; it is the role that decides")
        unknown = sorted(set(self.analysts) - set(READING) - {technical_analyst.ROLE})
        if unknown:
            raise CycleError(f"{unknown} is not an analyst; have {sorted(READING)}")
        stray = sorted(set(self.series) - set(READING))
        if stray:
            raise CycleError(f"series declared for {stray}, which read nothing")
        bad_stance = sorted(set(self.validators) - set(STANCES))
        if bad_stance:
            raise CycleError(f"{bad_stance} is not a stance; have {list(STANCES)}")
        if len(set(self.validators)) != len(self.validators):
            raise CycleError("each stance is asked once")
        if self.mode not in MODES:
            raise CycleError(f"mode is one of {list(MODES)}")
        if self.mode == "venue" and self.place is None:
            raise CycleError(
                "venue mode needs something to send with; the CLI builds it from [execution] "
                "once the mandate, the ladder and the keys are all in place"
            )
        if self.size < 0 or (self.size_quote is not None and self.size_quote <= 0):
            raise CycleError("[run].size and [run].size_quote are positive when declared")
        declared = [
            name
            for name, value in (("size", self.size), ("size_quote", self.size_quote))
            if value is not None and value > 0
        ]
        if len(declared) != 1:
            raise CycleError(
                "a cycle is sized by [run].size in base units or [run].size_quote in quote "
                f"currency, and exactly one of them: {declared or 'neither'} is declared"
            )
        if not isinstance(self.magnitude_bps, Decimal) or self.magnitude_bps <= 0:
            raise CycleError("magnitude_bps is the edge the operator claims, in bps, positive")
        if self.channel is None:
            raise CycleError(f"venue_key {self.venue_key!r} is not a channel in the envelope")

    @property
    def channel(self) -> Channel | None:
        return next((c for c in self.envelope.channels if c.key == self.venue_key), None)


@dataclass
class CycleResult:
    """What the cycle did, in the order a reader wants it."""

    cycle: str
    instrument: str
    snapshot: Snapshot
    briefs: dict[str, Any] = field(default_factory=dict)
    fan: FanOut = field(default_factory=FanOut)
    # The validators are a second round: they argue about a proposal that has to exist first. Kept
    # apart from the first round so each round's wall clock still means what it says.
    debate: FanOut | None = None
    context: dict[str, Any] = field(default_factory=dict)
    analyst: Any = None
    cited: list[dict[str, str]] = field(default_factory=list)
    confluence: Confluence | None = None
    stake: Stake | None = None
    risk: RiskVerdict | None = None
    book: DeskVerdict | None = None
    verdict: PreTradeVerdict | None = None
    targets: TargetVerdict | None = None
    intent: dict[str, Any] | None = None
    placed: dict[str, Any] | None = None
    refusals: list[str] = field(default_factory=list)
    stopped_by: str | None = None
    path: Path | None = None
    # What this cycle saw for free, and what the trigger made of it. The mid is written down even on
    # a cycle that asked nobody, because the next cycle measures its move against it.
    seen: dict[str, Any] = field(default_factory=dict)
    # Where the entry, the stop and the take profit sit, as prices and as distances from the entry.
    exits: dict[str, Any] = field(default_factory=dict)
    # The higher timeframes' candles, kept with the snapshot as this cycle's raw inputs.
    frames: dict[int, Fetched] = field(default_factory=dict)
    wake: Wake | None = None
    held: bool = False

    @property
    def acted(self) -> bool:
        return self.intent is not None

    def record(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle,
            "instrument": self.instrument,
            "data": self.briefs.get("data"),
            # Present only when the size was declared in quote currency: what it was converted at.
            "size": self.briefs.get("size"),
            # Venue mode only: the account as read before the cycle, and the equity sizing used.
            "equity": self.briefs.get("equity"),
            "agents": self.fan.record(),
            "validators": None if self.debate is None else self.debate.record(),
            "analyst": None if self.analyst is None else _said(self.analyst),
            "cited": self.cited,
            "context": self.context,
            "confluence": None if self.confluence is None else self.confluence.record(),
            "conviction": None if self.stake is None else self.stake.record(),
            "risk": None if self.risk is None else self.risk.record(),
            "desk": None if self.book is None else self.book.record(),
            "cost": None if self.verdict is None else self.verdict.record(),
            "targets": None if self.targets is None else self.targets.record(),
            "intent": self.intent,
            "placed": self.placed,
            "refusals": self.refusals,
            "stopped_by": self.stopped_by,
            "seen": self.seen,
            # Every series a reading analyst was handed: where from, the value, and how old.
            "series": self.briefs.get("series"),
            # Reading analysts this cycle did not ask, and why.
            "not_asked": self.briefs.get("not_asked"),
            # Reading analysts whose answer was reused, and how old it was in seconds.
            "reused": self.briefs.get("reused"),
            "exits": self.exits or None,
            # What the models cost this cycle, and which calls had no declared price.
            "spend": self.briefs.get("spend"),
            "targets_rule": self.briefs.get("targets_rule"),
            "trigger": None if self.wake is None else self.wake.record(),
            "held": self.held,
            "mode": self.briefs.get("mode"),
        }


# A desk picks its setup from the market in front of it; the research core trades one registered
# mechanism. Same role and schema, so only the instructions differ.
LIVE_PROMPTS = {technical_analyst.ROLE: "technical_analyst_live"}


def _ask(
    cfg: CycleConfig, role: str, question: Mapping[str, Any], schema: Mapping[str, Any]
) -> str:
    completer = cfg.completer_for(role)
    done = completer.complete(
        system=prompt(LIVE_PROMPTS.get(role, role)),
        user=json.dumps(question, sort_keys=True, default=str, separators=COMPACT),
        schema=dict(schema),
    )
    return done.text


def _before_votes(cfg: CycleConfig, result: CycleResult, costs: Any, edge: Decimal) -> str:
    """A refusal later steps are certain to make whatever the validators say, or nothing.

    Only what cannot change after the votes is read here: the desk with nothing added to it, the
    day's round trips, the declared reward to risk, and the edge against the part of the round trip
    that does not grow with size. A smaller conviction size only lowers the impact, so a claim that
    fails without impact fails with it too.
    """
    rule = cfg.targets
    if not rule.idle:
        if rule.max_round_trips_per_day is not None:
            trips = intents_today(cfg.root, at=cfg.clock(), instrument=cfg.instrument)
            if trips >= rule.max_round_trips_per_day:
                return (
                    f"targets: {trips} intent(s) already written today; the declared limit is "
                    f"{rule.max_round_trips_per_day}"
                )
        if rule.min_reward_risk is not None and (
            cfg.reward_to_risk is None or cfg.reward_to_risk < rule.min_reward_risk
        ):
            return (
                f"targets: reward-to-risk {cfg.reward_to_risk} is below the declared "
                f"{rule.min_reward_risk}"
            )
        floor = sum((c.bps for c in costs.cost.components if c.name != "impact"), Decimal(0))
        if rule.min_edge_multiple is not None and floor > 0:
            best = edge_multiple(edge_bps=edge, cost_bps=floor)
            if best is not None and best < rule.min_edge_multiple:
                return (
                    f"targets: the claimed edge is at most {best}x the round trip and the desk "
                    f"declared {rule.min_edge_multiple}x"
                )
        if rule.min_net_bps is not None and edge - floor < rule.min_net_bps:
            return (
                f"targets: net is at most {edge - floor} bps and the declared "
                f"minimum is {rule.min_net_bps} bps"
            )
    if cfg.desk.bounds_a_trade:
        held, source = _held(cfg, result)
        book = within_desk(
            cfg.desk, instrument=cfg.instrument, adding_bps=Decimal(0), held=held, source=source
        )
        if not book.admitted:
            result.book = book
            return "desk: " + "; ".join(book.reasons)
    return ""


def _ask_readable(
    cfg: CycleConfig,
    role: str,
    question: Mapping[str, Any],
    schema: Mapping[str, Any],
    parse: Callable[[str], Any],
) -> str:
    """Ask, and ask once more when the answer does not fit its schema.

    A malformed answer is a failed call, not a verdict, and with on_missing_validator at refuse one
    of them stops a trade (seen 2026-10-06: a validator's JSON came back with its fields out of
    place). One more call is cheaper than a lost cycle; a second malformed answer is left to fail.
    """
    text = _ask(cfg, role, question, schema)
    try:
        parse(text)
    except (RoleError, ValueError):
        return _ask(cfg, role, question, schema)
    return text


def run_cycle(cfg: CycleConfig) -> CycleResult:
    """Take one snapshot, ask the agents, price the trade, and write the cycle down."""
    cycle = cycle_id(cfg.clock(), cfg.instrument)
    where = CycleDir(root=cfg.root, cycle=cycle).make()
    spent_before = cfg.usage.cost
    calls_before = len(cfg.usage.records)
    kinds = [k for k in cfg.chains if not (cfg.spot and k in CONTRACT_KINDS)]
    announce(cfg.watch, "data", RUNNING, f"{len(kinds)} kind(s)")
    # What the operator declared per kind in data.toml, then the window this cycle needs. A venue
    # quirk lives in that file, so dropping it here would be dropping the operator's own decision.
    options = {kind: dict(cfg.data_options.get(kind, {})) for kind in kinds}
    options.setdefault("ohlcv", {}).update(
        {"interval_s": cfg.interval_s, "limit": max(cfg.lookback_bars, 2)}
    )
    snapshot = take_snapshot(cfg.instrument, cfg.chains, cycle=cycle, kinds=kinds, options=options)
    snapshot.write(where.snapshot)
    result = CycleResult(cycle=cycle, instrument=cfg.instrument, snapshot=snapshot, path=where.path)
    result.briefs["data"] = data_brief(snapshot).record()
    result.briefs["mode"] = cfg.mode
    for kind, reason in snapshot.refused.items():
        result.refusals.append(f"data {kind}: {reason}")
    for kind in snapshot.stale:
        result.refusals.append(f"data {kind}: used while stale, because the config allows it")
    answered = len(kinds) - len(snapshot.refused)
    announce(cfg.watch, "data", DONE, f"{answered} of {len(kinds)} kind(s) answered")

    bid, ask = _prices(snapshot)
    mid_now, spread_now = spread_of(bid, ask)
    base = cfg.size
    result.seen = {
        "mid": None if mid_now is None else str(mid_now),
        "spread_bps": None if spread_now is None else str(spread_now),
    }
    if cfg.forced and not cfg.trigger.idle:
        result.wake = Wake(
            ask=True, reasons=("the operator asked with --now, so the trigger was not applied",)
        )
        announce(cfg.watch, "trigger", SKIPPED, "asked with --now")
    elif not cfg.trigger.idle:
        wake = worth_asking(
            cfg.trigger,
            mid=mid_now,
            spread_bps=spread_now,
            seen=last_seen(cfg.root, instrument=cfg.instrument),
            now=cfg.clock(),
            round_trips_today=intents_today(cfg.root, at=cfg.clock(), instrument=cfg.instrument),
            max_round_trips_per_day=cfg.targets.max_round_trips_per_day,
        )
        result.wake = wake
        if not wake.ask:
            result.held = True
            for reason in wake.reasons:
                result.refusals.append(f"trigger: {reason}")
            announce(cfg.watch, "trigger", HELD, wake.reasons[0])
            _finish(cfg, where, result)
            return result
        announce(
            cfg.watch,
            "trigger",
            DONE,
            "" if wake.moved_bps is None else f"moved {wake.moved_bps} bps",
        )

    if cfg.size_quote is not None:
        # Converted before anyone is asked: a size that cannot exist is not worth a model call.
        if mid_now is None:
            why = (
                "size: [run].size_quote is declared and this snapshot has no two-sided book, so "
                "there is no price to convert it at"
            )
            return _stop_before_asking(cfg, where, result, why)
        base = _quantized(cfg.size_quote / mid_now, cfg.size_step)
        result.briefs["size"] = {
            "size_quote": str(cfg.size_quote),
            "mid": str(mid_now),
            "size": str(base),
            "step": str(cfg.size_step),
        }
        if base <= 0:
            why = (
                f"size: {cfg.size_quote} of quote at {mid_now} is less than one step of "
                f"{cfg.size_step}, so there is no size to trade"
            )
            return _stop_before_asking(cfg, where, result, why)
        announce(cfg.watch, "size", DONE, f"{cfg.size_quote} of quote is {base} at {mid_now}")

    equity = cfg.equity
    if cfg.account is not None:
        try:
            held_now = cfg.account()
        except Exception as exc:
            return _stop_before_asking(cfg, where, result, f"capital: {exc}", step="capital")
        equity = sizing_equity(cfg.equity, held_now.equity)
        result.briefs["equity"] = {
            "declared": None if cfg.equity is None else str(cfg.equity),
            "venue": str(held_now.equity),
            "free": str(held_now.free),
            "used": None if equity is None else str(equity),
        }
        if cfg.daily_loss_limit_bps is not None:
            down = day_loss_bps(cfg.root, cfg.clock(), held_now.equity)
            result.briefs["equity"]["day_loss_bps"] = str(down)
            if down >= cfg.daily_loss_limit_bps:
                why = (
                    f"{DAILY_LOSS_MARK} {down} bps since the day began, at or past "
                    f"[controls].daily_loss_limit_bps {cfg.daily_loss_limit_bps}; no new entry "
                    "today, and open positions keep their exits"
                )
                return _stop_before_asking(cfg, where, result, why, step="capital")
        if cfg.max_drawdown_bps is not None:
            fallen, peak = drawdown_bps(cfg.root, cfg.clock(), held_now.equity)
            result.briefs["equity"]["drawdown_bps"] = str(fallen)
            result.briefs["equity"]["peak"] = str(peak)
            if fallen >= cfg.max_drawdown_bps:
                then = "open positions keep their exits"
                if cfg.drawdown_kill is not None:
                    cfg.drawdown_kill.parent.mkdir(parents=True, exist_ok=True)
                    cfg.drawdown_kill.write_text(
                        f"armed by [controls].max_drawdown_bps at {cfg.clock().isoformat()}\n",
                        encoding="utf-8",
                    )
                    then = f"the kill switch at {cfg.drawdown_kill} is armed to flatten and stop"
                why = (
                    f"{DRAWDOWN_MARK} of {fallen} bps from the peak of {peak}, at or past "
                    f"[controls].max_drawdown_bps {cfg.max_drawdown_bps}; no new entry until "
                    f"the peak file {EQUITY_PEAK} is cleared, and {then}"
                )
                return _stop_before_asking(cfg, where, result, why, step="capital")
        announce(cfg.watch, "capital", DONE, f"equity {held_now.equity:.2f}, used {equity}")

    if cfg.wait_while_open and cfg.held is not None:
        rows, _source = _held(cfg, result)
        if any(row.instrument == cfg.instrument for row in rows):
            why = (
                f"open: a position on {cfg.instrument} is open at the venue, so the next entry "
                "waits for its exits; nothing was asked"
            )
            result.refusals.append(why)
            result.held = True
            announce(cfg.watch, "desk", HELD, "a position is already open")
            _finish(cfg, where, result)
            return result

    # The reading briefs come first: what the operator's thresholds say on them is code, known
    # before any model is asked, and the technical analyst decides with it in front of it.
    calendar = _calendar(cfg, result)
    reading_briefs: dict[str, dict[str, Any]] = {}
    for role in READING:
        if role not in cfg.analysts:
            # Shown, not hidden: an analyst missing from the table reads as one that broke.
            why = _not_on_roster(cfg, role)
            result.briefs.setdefault("not_asked", {})[role] = why
            announce(cfg.watch, role, SKIPPED, why)
            continue
        built = _reading_brief(cfg, role, snapshot, calendar, result)
        if built is None:
            why = next((r for r in reversed(result.refusals) if r.startswith(role)), "not asked")
            result.briefs.setdefault("not_asked", {})[role] = why
            announce(cfg.watch, role, SKIPPED, why.removeprefix(f"{role}: "))
            continue
        reading_briefs[role] = _within_scope(role, built, result)
    context = _context_frames(cfg, result)
    result.frames = context
    try:
        technical = technical_brief(
            snapshot,
            mechanism=cfg.mechanism,
            interval_s=cfg.interval_s,
            lookback_bars=cfg.lookback_bars,
            rule=cfg.regime,
            context=context,
            previous_open_interest=_previous_open_interest(cfg.root, cfg.instrument, cycle),
            readings={role: _reading_state(role, brief) for role, brief in reading_briefs.items()},
        )
    except BriefError as exc:
        result.stopped_by = f"no technical brief: {exc}"
        _finish(cfg, where, result)
        return result
    asked = _within_scope(technical_analyst.ROLE, technical.record(), result)
    write_json(where.brief("technical"), asked)
    announce(cfg.watch, "regime", DONE, _regime_line(cfg, technical))

    tasks = [
        Task(
            name="technical",
            work=timed(
                cfg.watch,
                "technical",
                lambda: _ask_readable(
                    cfg,
                    technical_analyst.ROLE,
                    asked,
                    technical_analyst.SCHEMA,
                    technical_analyst.parse,
                ),
            ),
            path=where.answer("technical"),
        )
    ]
    keys: dict[str, str] = {}
    reused: dict[str, float] = {}
    for role, brief in reading_briefs.items():
        write_json(where.brief(role), brief)
        module = READING[role]
        keys[role] = key_of(brief)
        earlier = lookup(cfg.root, role, keys[role], window_s=cfg.reuse_reading_s, now=cfg.clock())
        if earlier is not None and earlier[0] is not None:
            # The same figures were read and answered within the window; the answer is put where
            # this cycle's own would go, and the task below finds it there instead of asking.
            write_json(where.answer(role), earlier[0])
            reused[role] = round(earlier[1])
            announce(
                cfg.watch, role, DONE, f"reused, {reused[role]}s old; its figures have not changed"
            )
        tasks.append(
            Task(
                name=role,
                work=timed(cfg.watch, role, _reading_task(cfg, module, brief)),
                path=where.answer(role),
            )
        )
    if reused:
        result.briefs["reused"] = reused

    fan = run_tasks(tasks, max_workers=cfg.max_workers, resume=bool(reused))
    result.fan = fan
    for role in reading_briefs:
        if role in reused or role not in fan.answers:
            continue
        try:
            READING[role].parse(str(fan.answers[role]))
        except (RoleError, ValueError):
            continue
        remember(cfg.root, role, keys[role], fan.answers[role], now=cfg.clock())
    result.context = _context_of(fan, reading_briefs, result)
    for name, error in fan.failed.items():
        result.refusals.append(f"{name}: {error}")
        if "BudgetSpentError" in error:
            result.stopped_by = f"{name}: {error}"
    if "technical" not in fan.answers:
        result.stopped_by = result.stopped_by or "the technical analyst did not answer"
        _finish(cfg, where, result)
        return result

    try:
        analyst = technical_analyst.parse(str(fan.answers["technical"]))
    except RoleError as exc:
        result.refusals.append(f"technical: {exc}")
        result.stopped_by = f"technical: {exc}"
        _finish(cfg, where, result)
        return result
    result.analyst = analyst

    kept, lost = survived(analyst.figures_used, figures_of(asked))
    result.cited = [figure.record() for figure in kept]
    for gone in lost:
        result.refusals.append(f"technical: discarded a citation, {gone}")

    if analyst.target == "flat":
        result.refusals.append("technical: flat, so there is nothing to price")
        announce(cfg.watch, "technical", DONE, "flat")
        _finish(cfg, where, result)
        return result
    if cfg.spot and analyst.target == "short":
        result.refusals.append(
            f"market: {cfg.instrument} is a spot market; a short would sell coins the account "
            "does not hold, so it is not taken"
        )
        announce(cfg.watch, "technical", REFUSED, "short on a spot market")
        _finish(cfg, where, result)
        return result
    if not kept:
        # The role that decides is the one role whose citations have teeth. A direction that cannot
        # point at a figure in its own brief is a direction that came from somewhere else.
        result.refusals.append(
            "technical: not one figure from its own brief survived the check, so the direction "
            "rests on nothing this cycle measured"
        )
        announce(cfg.watch, "technical", REFUSED, "cited nothing")
        _finish(cfg, where, result)
        return result
    if analyst.setup == "no_setup":
        result.refusals.append(
            f"technical: {analyst.target} with no setup named; a direction without a shape behind "
            "it is not something this desk can act on or review afterwards"
        )
        announce(cfg.watch, "technical", REFUSED, "no setup")
        _finish(cfg, where, result)
        return result
    misfit_why = misfit(
        analyst.setup,
        analyst.target,
        technical.regime,
        zscore=technical.features.zscore,
        window_move_bps=technical.features.window_move_bps,
        position_in_range=technical.features.position_in_range,
        trend_strength=technical.features.trend_strength,
    )
    if misfit_why and cfg.regime.setups_must_fit:
        result.refusals.append(f"regime: {misfit_why} ([regime].setups_must_fit)")
        announce(cfg.watch, "technical", REFUSED, "the setup does not fit the regime")
        _finish(cfg, where, result)
        return result
    if misfit_why:
        result.refusals.append(f"regime: {misfit_why}; allowed, setups_must_fit is false")
    if analyst.confidence < cfg.min_confidence:
        result.refusals.append(
            f"technical: confidence {analyst.confidence} below the declared {cfg.min_confidence}"
        )
        announce(cfg.watch, "technical", REFUSED, "below the confidence floor")
        _finish(cfg, where, result)
        return result

    direction = TARGETS[analyst.target]
    try:
        costs = cost_brief(
            snapshot,
            channel=_channel(cfg),
            size=base,
            direction=direction,
            horizon_bars=_held_bars(cfg, analyst.horizon_bars),
            bar_interval_s=cfg.bar_interval_s,
            settlement_interval_s=cfg.settlement_interval_s,
            latency_slippage_bps=cfg.latency_slippage_bps,
        )
    except BriefError as exc:
        result.refusals.append(f"cost: {exc}")
        result.stopped_by = f"cost: {exc}"
        _finish(cfg, where, result)
        return result
    result.briefs["cost"] = costs.record()
    write_json(where.brief("cost"), costs.record())
    announce(cfg.watch, "cost", DONE, f"round trip {costs.cost.round_trip_bps:.2f} bps")

    # Where the trade is wrong and where it is done depend on the price and the bars, not on the
    # size, so they are settled before anyone is paid to vote on a trade they would refuse.
    try:
        exits, placement = _exits(
            cfg,
            costs.cost.mid,
            analyst.target,
            snapshot=snapshot,
            horizon_bars=_held_bars(cfg, analyst.horizon_bars),
            stop_price=analyst.stop_price,
            target_price=analyst.target_price,
        )
    except StopError as exc:
        # A stop that cannot be placed inside the operator's bounds is not replaced by a guess.
        result.refusals.append(f"exit: {exc}")
        announce(cfg.watch, "exit", REFUSED, str(exc))
        _finish(cfg, where, result)
        return result
    view: dict[str, Any] = dict(_exit_view(costs.cost.mid, analyst.target, exits))
    if placement is not None:
        view["measured"] = placement.record()
    result.exits = view
    take_pnl = view.get("take_pnl_bps")
    if take_pnl is not None and Decimal(take_pnl) <= costs.cost.round_trip_bps:
        # A take profit inside the round trip turns the trade's best outcome into a loss.
        result.refusals.append(
            f"exit: the take profit is {take_pnl} bps away and the round trip costs "
            f"{costs.cost.round_trip_bps:.2f} bps, so a winner would lose money"
        )
        announce(cfg.watch, "exit", REFUSED, "take profit inside the round trip")
        _finish(cfg, where, result)
        return result
    even = _break_even(view, costs.cost.round_trip_bps)
    bound = cfg.targets.max_break_even_win_rate
    if even is not None and Decimal(even) > bound:
        result.refusals.append(
            f"exit: {Decimal(even) * 100:.1f}% of trades like this would have to reach the take "
            f"profit to cover the stops and the {costs.cost.round_trip_bps:.2f} bps round trip; "
            f"[targets].max_break_even_win_rate is {bound * 100:.0f}%"
        )
        announce(cfg.watch, "exit", REFUSED, f"break even at {Decimal(even) * 100:.1f}%")
        _finish(cfg, where, result)
        return result
    edge, edge_source = _claimed_edge(cfg, placement)
    result.briefs["edge"] = {"edge_bps": str(edge), "source": edge_source}
    if view.get("stop_pnl_bps") is not None:
        announce(
            cfg.watch,
            "exit",
            DONE,
            f"stop {view['stop_pnl_bps']} bps, take {view.get('take_pnl_bps')} bps"
            + (f", break even {Decimal(even) * 100:.0f}%" if even is not None else ""),
        )

    if cfg.validators:
        early = _before_votes(cfg, result, costs, edge)
        if early:
            # Refused by code that does not read a single vote, so asking three models first
            # would only pay for an answer nothing can use.
            result.refusals.append(f"{early}; the validators were not asked")
            announce(cfg.watch, early.partition(":")[0], REFUSED, early.partition(": ")[2])
            _finish(cfg, where, result)
            return result
        _run_validators(cfg, where, result, asked=asked, analyst=analyst, costs=costs, edge=edge)
        silent = [
            name
            for name in (f"validator_{stance}" for stance in cfg.validators)
            if "error" in (result.context.get(name) or {"error": "not asked"})
        ]
        if silent and cfg.on_missing_validator == "refuse" and result.stopped_by is None:
            why = (
                f"validators: {', '.join(silent)} did not answer, and "
                "[agents].on_missing_validator is refuse; a check you declared did not happen"
            )
            result.refusals.append(why)
            result.stopped_by = why
            announce(cfg.watch, "validators", REFUSED, f"{', '.join(silent)} did not answer")
            _finish(cfg, where, result)
            return result
        if result.stopped_by is not None:
            _finish(cfg, where, result)
            return result

    if not cfg.matrix.idle:
        agreement = confluent(
            cfg.matrix,
            target=analyst.target,
            states={
                role: said["declared_state"]
                for role, said in result.context.items()
                if isinstance(said.get("declared_state"), Mapping)
            },
            votes=_votes(result),
        )
        result.confluence = agreement
        for reason in agreement.reasons:
            result.refusals.append(f"confluence: {reason}")
        announce(
            cfg.watch,
            "confluence",
            DONE if agreement.admitted else REFUSED,
            agreement.reasons[0] if agreement.reasons else "",
        )
        if not agreement.admitted:
            _finish(cfg, where, result)
            return result

    size = base
    if not cfg.conviction.idle:
        stake = stake_for(
            cfg.conviction,
            score_of(
                cfg.conviction,
                target=analyst.target,
                states={
                    role: said["declared_state"]
                    for role, said in result.context.items()
                    if isinstance(said.get("declared_state"), Mapping)
                },
                confidence=analyst.confidence,
                votes=_informative_votes(result),
            ),
            base_size=base,
            price=costs.cost.mid,
            max_position=_position_cap(cfg, costs.cost.mid, base),
            equity=equity,
            step=cfg.size_step,
        )
        result.stake = stake
        for reason in stake.reasons:
            result.refusals.append(f"conviction: {reason}")
        announce(
            cfg.watch,
            "conviction",
            DONE if stake.admitted else REFUSED,
            f"score {stake.score.score}",
        )
        if not stake.admitted:
            _finish(cfg, where, result)
            return result
        size = stake.size

    stop_at = exits.get("stop_price")
    if cfg.max_risk_per_trade_bps is not None and stop_at is None:
        # A declared risk limit that cannot be computed is not a limit. Fail closed and say which
        # declaration is missing rather than sending a size nothing checked.
        result.refusals.append(
            "risk: [controls].max_risk_per_trade_bps is declared and no stop is, so the loss at "
            "the stop cannot be computed; declare [exit].stop_frac or drop the limit"
        )
        announce(cfg.watch, "risk", REFUSED, "no stop to size from")
        _finish(cfg, where, result)
        return result
    capped = within_risk(
        size=size,
        entry=costs.cost.mid,
        stop=None if stop_at is None else Decimal(stop_at),
        equity=equity,
        allowed_bps=cfg.max_risk_per_trade_bps,
        step=cfg.size_step,
    )
    result.risk = capped
    for reason in capped.reasons:
        result.refusals.append(f"risk: {reason}")
    announce(
        cfg.watch,
        "risk",
        DONE if capped.admitted else REFUSED,
        "" if capped.risk_bps is None else f"{capped.risk_bps} bps of equity at the stop",
    )
    if not capped.admitted:
        _finish(cfg, where, result)
        return result
    size = capped.size

    if cfg.desk.bounds_a_trade:
        held, source = _held(cfg, result)
        book = within_desk(
            cfg.desk,
            instrument=cfg.instrument,
            adding_bps=capped.risk_bps,
            held=held,
            source=source,
        )
        result.book = book
        for reason in book.reasons:
            result.refusals.append(f"desk: {reason}")
        announce(
            cfg.watch,
            "desk",
            DONE if book.admitted else REFUSED,
            "" if book.open_risk_bps is None else f"{book.open_risk_bps} bps open across the book",
        )
        if not book.admitted:
            _finish(cfg, where, result)
            return result

    if size != base:
        # Impact is a function of the size that actually goes out, so the gate prices the trade it
        # is admitting rather than the one the operator declared as a base.
        try:
            costs = cost_brief(
                snapshot,
                channel=_channel(cfg),
                size=size,
                direction=direction,
                horizon_bars=_held_bars(cfg, analyst.horizon_bars),
                bar_interval_s=cfg.bar_interval_s,
                settlement_interval_s=cfg.settlement_interval_s,
                latency_slippage_bps=cfg.latency_slippage_bps,
            )
        except BriefError as exc:
            result.refusals.append(f"cost at {size}: {exc}")
            result.stopped_by = f"cost at {size}: {exc}"
            _finish(cfg, where, result)
            return result
        result.briefs["cost"] = costs.record()
        write_json(where.brief("cost"), costs.record())

    verdict = admit(
        edge_bps=edge,
        cost=costs.cost,
        margin_bps=cfg.margin_bps,
        edge_source=edge_source,
    )
    result.verdict = verdict
    announce(
        cfg.watch,
        "cost gate",
        DONE if verdict.admitted else REFUSED,
        f"net {verdict.net_bps:.2f} bps",
    )
    if not verdict.admitted:
        result.refusals.append(f"cost gate: {verdict.reason}")
        _finish(cfg, where, result)
        return result

    spent = cfg.carried_spend + (cfg.usage.cost - spent_before)
    calls = cfg.usage.records[calls_before:]
    result.briefs["spend"] = {
        "calls": len(calls),
        "unpriced": sorted({f"{c.provider}:{c.model}" for c in calls if c.cost is None}),
    }
    if not cfg.targets.idle:
        result.briefs["targets_rule"] = cfg.targets.record()
        met = within_targets(
            cfg.targets,
            net_bps=verdict.net_bps,
            reward_to_risk=cfg.reward_to_risk,
            round_trips_today=intents_today(cfg.root, at=cfg.clock(), instrument=cfg.instrument),
            over_cost=edge_multiple(edge_bps=edge, cost_bps=costs.cost.round_trip_bps),
            research=research_bps(spend=spent, notional=size * costs.cost.mid),
        )
        result.targets = met
        for reason in met.reasons:
            result.refusals.append(f"targets: {reason}")
        announce(
            cfg.watch,
            "targets",
            DONE if met.admitted else REFUSED,
            met.reasons[0] if met.reasons else "every declared target met",
        )
        if not met.admitted:
            _finish(cfg, where, result)
            return result

    result.intent = {
        "instrument": cfg.instrument,
        "target": analyst.target,
        "setup": analyst.setup,
        "size": str(size),
        "base_size": str(base),
        "conviction": None if result.stake is None else str(result.stake.score.score),
        "risk_bps": None
        if result.risk is None or result.risk.risk_bps is None
        else str(result.risk.risk_bps),
        "horizon_bars": analyst.horizon_bars,
        "confidence": analyst.confidence,
        "reason": analyst.reason,
        # What the direction was read from, after code checked each figure against the brief. An
        # intent that cannot say this is an intent nobody can review a month later.
        "cited": result.cited,
        "edge_bps": str(edge),
        "edge_source": verdict.edge_source,
        "break_even_win_rate": _break_even(result.exits, costs.cost.round_trip_bps),
        "net_bps": str(verdict.net_bps.quantize(Decimal("0.0001"))),
        "edge_multiple": (
            None
            if result.targets is None or result.targets.edge_multiple is None
            else str(result.targets.edge_multiple)
        ),
        "research_bps": (
            None
            if result.targets is None or result.targets.research_bps is None
            else str(result.targets.research_bps)
        ),
        "mode": cfg.mode,
        "mid": str(costs.cost.mid),
        "at": cycle,
        # The touch this cycle saw, so an order that rests on the book rests where the cost was
        # priced rather than wherever the book has moved to by the time it is sent.
        **_touch(snapshot),
        **exits,
        "channel": cfg.venue_key,
        "entry_role": _channel(cfg).entry_role,
    }
    announce(cfg.watch, "intent", DONE, f"{analyst.target} {size} on {analyst.setup}")
    append_line(where.path / "intents.jsonl", result.intent, durable=True)

    if cfg.mode == "venue" and cfg.place is not None:
        # The intent is already on disk. Whatever the venue does next, the record of what this
        # cycle decided survives it.
        announce(cfg.watch, "venue", RUNNING)
        try:
            result.placed = dict(cfg.place(result.intent))
        except Exception as exc:
            result.placed = {"sent": False, "error": f"{exc.__class__.__name__}: {exc}"}
            result.refusals.append(f"venue: {exc.__class__.__name__}: {exc}")
            announce(cfg.watch, "venue", FAILED, exc.__class__.__name__)
        else:
            if result.placed.get("sent"):
                announce(
                    cfg.watch, "venue", DONE, f"order {result.placed.get('order_id') or 'sent'}"
                )
            else:
                said = str(result.placed.get("refused") or result.placed.get("error") or "not sent")
                result.refusals.append(f"venue: not sent, {said}")
                announce(cfg.watch, "venue", REFUSED, said)
            write_json(where.path.joinpath("placed.json"), result.placed, durable=True)
            if result.placed.get("interrupted"):
                result.stopped_by = f"interrupted at the venue: {result.placed.get('note')}"
                _finish(cfg, where, result)
                # The record is on disk and the order is off the book; now the operator gets the
                # stop they asked for, instead of a scheduler that carries on.
                raise KeyboardInterrupt(result.stopped_by)
    _finish(cfg, where, result)
    return result


def _context_frames(cfg: CycleConfig, result: CycleResult) -> dict[int, Fetched]:
    """The higher timeframes' closed candles. One that cannot be had is named and left out."""
    chain = cfg.chains.get("ohlcv")
    if chain is None or not cfg.context_intervals:
        return {}
    out: dict[int, Fetched] = {}
    for seconds in cfg.context_intervals:
        # A closed candle on this timeframe is up to one interval old by construction, so the
        # decision chain's freshness bar would refuse every one of them.
        wider = replace(chain, ttl_s=chain.ttl_s + seconds)
        options = dict(cfg.data_options.get("ohlcv", {}))
        options.update({"interval_s": seconds, "limit": max(cfg.lookback_bars, 2)})
        try:
            got = wider.fetch(cfg.instrument, **options)
        except ChainExhaustedError as exc:
            result.refusals.append(f"context {interval_name(seconds)}: {exc}")
            continue
        if got.fetched is not None:
            out[seconds] = got.fetched
    if out:
        result.briefs["context"] = {
            interval_name(seconds): fetched.record() for seconds, fetched in out.items()
        }
    return out


def _previous_open_interest(root: Path, instrument: str, cycle: str) -> dict[str, Any] | None:
    """The open interest the latest earlier snapshot of this instrument recorded."""
    if not root.is_dir():
        return None
    if has_index(root):
        earlier = (r for r in recent(root, instrument=instrument) if str(r["cycle_id"]) < cycle)
        for row in islice(earlier, OPEN_INTEREST_LOOK_BACK):
            if isinstance(row.get("open_interest"), dict):
                return dict(row["open_interest"])
        return None
    safe = "".join(c for c in instrument if c.isalnum() or c in "-_")
    mine = sorted(
        (p for p in root.glob(f"*-{safe}") if p.is_dir() and p.name < cycle),
        reverse=True,
    )
    for path in mine[:OPEN_INTEREST_LOOK_BACK]:
        try:
            values = json.loads((path / "snapshot.json").read_text("utf-8")).get("values", {})
        except (OSError, ValueError, AttributeError):
            continue
        found = values.get("open_interest")
        if isinstance(found, dict):
            return found
    return None


def _reading_state(role: str, brief: Mapping[str, Any]) -> dict[str, Any]:
    declared = declared_state(role, brief)
    return {
        "declared": declared.get("as_role_says_it") or declared.get("declared"),
        "score": declared.get("score", "0"),
    }


def _regime_line(cfg: CycleConfig, brief: Any) -> str:
    said = [f"{brief.regime} on {interval_name(cfg.interval_s)}"]
    said.extend(f"{name} {frame['regime']}" for name, frame in (brief.timeframes or {}).items())
    said.extend(f"{role} {state['declared']}" for role, state in (brief.readings or {}).items())
    return ", ".join(said)


def _not_on_roster(cfg: CycleConfig, role: str) -> str:
    if cfg.series.get(role):
        return "not in [agents].analysts, so not asked"
    shelves = " or ".join(f"sources/{name}.toml" for name in CLASSES_OF.get(role, ()))
    return f"nothing declared in {shelves or 'sources/'}, so not asked"


def _calendar(cfg: CycleConfig, result: CycleResult) -> tuple[Event, ...]:
    """Every dated text file the project declares, as one list. A bad file is named, not guessed."""
    out: list[Event] = []
    for path in cfg.news:
        try:
            out.extend(load_news(path) if path.suffix == ".toml" else read_events(path))
        except SeriesError as exc:
            result.refusals.append(f"news {path.name}: {exc}")
    return tuple(sorted(out, key=lambda e: e.when_ns))


def _reading_brief(
    cfg: CycleConfig,
    role: str,
    snapshot: Snapshot,
    calendar: tuple[Event, ...],
    result: CycleResult,
) -> dict[str, Any] | None:
    """One reading analyst's brief, or nothing when its series cannot be read at all."""
    series: tuple[Series, ...] = ()
    files = cfg.series.get(role, ())
    for declared in files:
        try:
            readable, problems = load_readable(declared)
        except SeriesError as exc:
            # One unreadable file does not silence a role that has other shelves to read from.
            result.refusals.append(f"{role} {declared.name}: {exc}")
            continue
        series = (*series, *readable)
        result.refusals.extend(f"{role} {declared.name}: {problem}" for problem in problems)
    if files and not series:
        # Everything this role was given is unreadable. Asking anyway would pay a model to say it
        # saw nothing, and would hide a broken file behind a shrug.
        result.refusals.append(f"{role}: nothing readable in {[p.name for p in files]}; not asked")
        return None
    try:
        brief = context_brief(role, snapshot, series=series, events=calendar)
    except BriefError as exc:
        result.refusals.append(f"{role}: {exc}")
        return None
    rows = list(brief.get("series") or [])
    result.briefs.setdefault("series", []).extend(
        {
            "role": role,
            "series": row.get("series"),
            "source": row.get("source"),
            "origin": row.get("origin"),
            "value": row.get("value"),
            "observed_at": row.get("observed_at"),
            "age_h": row.get("age_h"),
            "state": row.get("state"),
        }
        for row in rows
    )
    old = [row for row in rows if row.get("stale")]
    for row in old:
        result.refusals.append(
            f"{role} {row['series']}: last value {str(row['observed_at'])[:16]}, "
            f"{row['age_h']}h old, past max_age {row['max_age']}; shown, not counted"
        )
    if rows and len(old) == len(rows) and not (brief.get("events") or brief.get("notes")):
        # Every figure this role would read is out of date; paying a model to read them would buy
        # a label nothing can count.
        result.refusals.append(f"{role}: every series is past its max_age; not asked")
        return None
    return brief


def _reading_task(cfg: CycleConfig, module: Any, brief: dict[str, Any]) -> Callable[[], str]:
    def ask() -> str:
        return _ask_readable(cfg, module.ROLE, brief, module.SCHEMA, module.parse)

    return ask


def _context_of(
    fan: FanOut, briefs: Mapping[str, dict[str, Any]], result: CycleResult
) -> dict[str, Any]:
    """What each reading analyst said, beside what the operator's own thresholds say.

    The model's label explains; the declared state decides. Keeping both in the record is what makes
    a disagreement visible instead of a matter of whose sentence was read last.
    """
    out: dict[str, Any] = {}
    for role, module in READING.items():
        if role not in briefs:
            continue
        # The declared state is code over the operator's thresholds, so it stands whether or not
        # the model answered: a rate limit on the analyst must not erase what the figures say.
        declared = declared_state(role, briefs.get(role, {}))
        if role not in fan.answers:
            out[role] = {"declared_state": declared, "error": "the model did not answer"}
            continue
        try:
            said = vars(module.parse(str(fan.answers[role])))
        except RoleError as exc:
            out[role] = {"declared_state": declared, "error": str(exc)}
            continue
        label = said.get("regime") or said.get("signal") or said.get("state")
        said["declared_state"] = declared
        cited = said.get("figures_used")
        if isinstance(cited, tuple):
            kept, lost = survived(cited, figures_of(briefs.get(role, {})))
            said["figures_used"] = [figure.record() for figure in kept]
            if lost:
                said["uncited"] = list(lost)
                for gone in lost:
                    result.refusals.append(f"{role}: discarded a citation, {gone}")
            if not kept:
                # The label still counts for nothing on its own - the gates read the operator's own
                # declared state - but a sentence with no figure behind it should not sit in the
                # record looking like evidence.
                result.refusals.append(
                    f"{role}: pointed at no figure in its own brief; the label is recorded and "
                    "supports nothing"
                )
        reached = crossings(role, said)
        if reached:
            said["out_of_scope"] = list(reached)
            for finding in reached:
                result.refusals.append(
                    f"{role}: {finding}; that belongs to a later step and changes nothing here"
                )
        expected = declared.get("as_role_says_it")
        said["agrees_with_declared"] = None if expected is None else label == expected
        if expected is not None and label != expected:
            result.refusals.append(
                f"{role}: said {label!r} while the operator's threshold says {expected!r}; "
                "the declaration is what the next gate reads"
            )
        out[role] = said
    return out


def _within_scope(role: str, brief: Mapping[str, Any], result: CycleResult) -> dict[str, Any]:
    """The brief a role is handed: its own sections, with anything else named and removed."""
    extra = dropped(role, brief)
    if extra:
        result.refusals.append(f"{role}: not this role's to read, removed: {list(extra)}")
    return brief_for(role, brief)


def _run_validators(
    cfg: CycleConfig,
    where: CycleDir,
    result: CycleResult,
    *,
    asked: Mapping[str, Any],
    analyst: Any,
    costs: Any,
    edge: Decimal,
) -> None:
    """Ask each declared stance about the proposal, then keep only the claims that cite a figure."""
    figures = figures_of(asked)
    proposal = {
        "target": analyst.target,
        "confidence": analyst.confidence,
        "horizon_bars": analyst.horizon_bars,
        "reason": analyst.reason,
        "mechanism": cfg.mechanism,
        "edge_bps": str(edge),
    }
    states = {
        role: said.get("declared_state", {})
        for role, said in result.context.items()
        if isinstance(said.get("declared_state"), Mapping)
    }
    tasks = []
    seen: dict[str, dict[str, str]] = {}
    for stance in cfg.validators:
        name = f"validator_{stance}"
        brief = _within_scope(
            validator.ROLE,
            validator_brief(
                instrument=cfg.instrument,
                stance=stance,
                proposal=proposal,
                figures=figures,
                declared_states=states,
                cost_bps=str(costs.cost.round_trip_bps),
            ),
            result,
        )
        write_json(where.brief(name), brief)
        # What this validator may cite is what this validator was given - the proposal's own
        # figures and the cost included - rather than a narrower set it never saw.
        seen[name] = figures_of(brief)
        tasks.append(
            Task(
                name=name,
                work=timed(cfg.watch, name, _validator_task(cfg, brief)),
                path=where.answer(name),
            )
        )

    fan = run_tasks(tasks, max_workers=cfg.max_workers)
    result.debate = fan
    for name, error in fan.failed.items():
        result.refusals.append(f"{name}: {error}")
        result.context[name] = {"stance": name.partition("_")[2], "error": error}
        if budget_spent(error):
            result.stopped_by = f"{name}: {error}"
    for name, answer in fan.answers.items():
        stance = name.partition("_")[2]
        try:
            said = validator.parse(str(answer))
        except (RoleError, ValueError) as exc:
            result.context[name] = {"stance": stance, "error": str(exc)}
            result.refusals.append(f"{name}: {exc}")
            continue
        verdict = checked(said, seen.get(name, figures))
        record = verdict.record()
        reached = crossings(validator.ROLE, {"reason": verdict.reason})
        if reached:
            record["out_of_scope"] = list(reached)
            for finding in reached:
                result.refusals.append(f"{name}: {finding}; it changes nothing here")
        for dropped_claim in verdict.discarded:
            result.refusals.append(f"{name}: discarded a claim, {dropped_claim}")
        result.context[name] = record


def _validator_task(cfg: CycleConfig, brief: Mapping[str, Any]) -> Callable[[], str]:
    def ask() -> str:
        return _ask_readable(cfg, validator.ROLE, brief, validator.SCHEMA, validator.parse)

    return ask


def _votes(result: CycleResult) -> tuple[str, ...]:
    """The validators' verdicts, after code checked the claims they rest on."""
    out = []
    for role, said in sorted(result.context.items()):
        if role.startswith("validator") and isinstance(said.get("verdict"), str):
            out.append(str(said["verdict"]))
    return tuple(out)


# What a stance's verdict tells conviction. Pro argues for and contra against by assignment, so
# their expected verdict carries no information (measured on the demo desk: pro supported 36 of 37,
# contra opposed 34 of 35) and only adds weight that dilutes every score. A stance that cannot make
# its own case is the signal. Neutral counts as it votes.
STANCE_SIGNAL: dict[str, dict[str, str]] = {
    "pro": {"support": "abstain", "abstain": "oppose", "oppose": "oppose"},
    "contra": {"oppose": "abstain", "abstain": "support", "support": "support"},
}


def _informative_votes(result: CycleResult) -> tuple[str, ...]:
    """The validators' verdicts as conviction weighs them: what each one says beyond its stance."""
    out = []
    for role, said in sorted(result.context.items()):
        verdict = said.get("verdict") if role.startswith("validator") else None
        if not isinstance(verdict, str):
            continue
        stance = role.partition("_")[2]
        out.append(STANCE_SIGNAL.get(stance, {}).get(verdict, verdict))
    return tuple(out)


def _held(cfg: CycleConfig, result: CycleResult) -> tuple[tuple[Open, ...], str]:
    """What this desk is already carrying, from wherever the caller said to look.

    A reader that cannot answer is not a reason to lose the cycle, but it is a reason to refuse the
    trade: a desk limit measured against an unknown book is not a limit. The failure is recorded and
    the book comes back empty with the reason in its place.
    """
    if cfg.held is None:
        return (), "nothing was asked; no reader was given to this cycle"
    try:
        return cfg.held()
    except Exception as exc:
        result.refusals.append(
            f"desk: could not read what is open: {exc.__class__.__name__}: {exc}"
        )
        return (Open(instrument="unknown"),), f"unreadable: {exc.__class__.__name__}"


def _said(analyst: Any) -> dict[str, Any]:
    """The deciding analyst's answer as JSON, with its citations flattened for the record."""
    out = dict(vars(analyst))
    cited = out.get("figures_used")
    if isinstance(cited, tuple):
        out["figures_used"] = [figure.record() for figure in cited]
    return out


def _exits(
    cfg: CycleConfig,
    mid: Decimal,
    target: str,
    *,
    snapshot: Snapshot | None = None,
    horizon_bars: int = 1,
    stop_price: Decimal | None = None,
    target_price: Decimal | None = None,
) -> tuple[dict[str, str], Placement | None]:
    """Where the stop and the take profit sit, and what measured them when the market did.

    A position without an exit is not a trade, so these travel with the intent and, in venue mode,
    go to the venue attached to the entry. Nothing here asks a model where a stop belongs. Fixed
    mode uses the operator's fractions, each half on its own; volatility mode measures both from
    the decision timeframe's closed candles.
    """
    if mid <= 0:
        return {}, None
    stop_frac, take_frac, placement = cfg.stop_frac, cfg.take_frac, None
    if cfg.volatility_exit is not None:
        candles = _candles(snapshot)
        if not candles:
            raise StopError("no closed candles in this snapshot to measure a stop from")
        placement = cfg.volatility_exit.place(
            candles,
            horizon_bars=horizon_bars,
            mid=mid,
            side=target,
            stop_price=stop_price,
            target_price=target_price,
        )
        stop_frac, take_frac = placement.stop_frac, placement.take_frac
    away = Decimal(1) if target == "long" else Decimal(-1)
    out: dict[str, str] = {}
    if stop_frac is not None:
        out["stop_price"] = str((mid * (Decimal(1) - away * stop_frac)).quantize(mid))
    if take_frac is not None:
        out["take_price"] = str((mid * (Decimal(1) + away * take_frac)).quantize(mid))
    return out, placement


def _candles(snapshot: Snapshot | None) -> list[Any]:
    """The decision timeframe's closed candles, oldest first, or none."""
    if snapshot is None:
        return []
    try:
        value = snapshot.get("ohlcv").value
    except KeyError:
        return []
    return list(value) if isinstance(value, list | tuple) else []


def _position_cap(cfg: CycleConfig, mid: Decimal, base: Decimal) -> Decimal:
    """The most this cycle may hold, in units: the unit cap, the quote cap at this mid, or both."""
    caps = [cfg.max_position] if cfg.max_position is not None else []
    if cfg.max_position_quote is not None and mid > 0:
        caps.append(_quantized(cfg.max_position_quote / mid, cfg.size_step))
    return min(caps) if caps else base


def _claimed_edge(cfg: CycleConfig, placement: Placement | None) -> tuple[Decimal, str]:
    """The move the trade claims, for the cost gate and the targets, and who claimed it.

    When the analyst named a target and it survived the bounds, that target is the claim: it is the
    analyst's own reading of where the idea is done, already held to max_reward_to_risk and to the
    break-even bound. A take profit the system placed as a multiple of the stop claims nothing, so
    without a named target the operator's declared magnitude stands.
    """
    if placement is not None and placement.target_named:
        take = (placement.take_frac * Decimal(10_000)).quantize(Decimal("0.01"))
        return take, "the analyst's own target, held inside the [exit] bounds"
    return (
        cfg.magnitude_bps,
        "[agents].magnitude_bps, declared by the operator and not measured here",
    )


def _held_bars(cfg: CycleConfig, horizon: int) -> int:
    """The bars a position can stay open: the analyst's horizon capped by the timeout when the
    desk closes on the horizon, otherwise as long as the timeout allows."""
    if cfg.timeout_bars is None:
        return horizon
    if cfg.hold_limit == "horizon":
        return min(horizon, cfg.timeout_bars)
    return max(horizon, cfg.timeout_bars)


def _break_even(exits: Mapping[str, Any], round_trip_bps: Decimal) -> str | None:
    """The share of trades that must reach the take profit for the stop, the take and the round
    trip to come out even: (stop + cost) / (take + stop). Arithmetic on figures already priced,
    with no guess at the odds."""
    take, stop = exits.get("take_pnl_bps"), exits.get("stop_pnl_bps")
    if take is None or stop is None:
        return None
    gain, loss = Decimal(take), -Decimal(stop)
    if gain <= 0 or loss <= 0:
        return None
    return str(((loss + round_trip_bps) / (gain + loss)).quantize(Decimal("0.001")))


def _exit_view(mid: Decimal, target: str, exits: Mapping[str, str]) -> dict[str, str]:
    """The exits as a person reads them: a price and how far it is from the entry, signed."""
    out = {"side": target, "entry": str(mid)}
    for name in ("stop", "take"):
        price = exits.get(f"{name}_price")
        if price is None or mid <= 0:
            continue
        moved = ((Decimal(price) / mid - 1) * 10_000).quantize(Decimal("0.01"))
        out[f"{name}_price"] = price
        # The price's move, and what that move does to this position: a short gains as it falls.
        out[f"{name}_bps"] = str(moved)
        out[f"{name}_pnl_bps"] = str(moved if target == "long" else -moved)
    return out


def _quantized(size: Decimal, step: Decimal) -> Decimal:
    """Down to the venue's own increment, never up: a size it would refuse is not a size."""
    if step <= 0:
        return size
    return (size / step).to_integral_value(rounding="ROUND_DOWN") * step


def _prices(snapshot: Snapshot) -> tuple[Decimal | None, Decimal | None]:
    """The best bid and ask of this snapshot's book, as numbers, when it has one."""
    try:
        book = snapshot.get("orderbook").value
    except KeyError:
        return None, None
    bids, asks = getattr(book, "bids", ()), getattr(book, "asks", ())
    if not bids or not asks:
        return None, None
    return Decimal(str(bids[0].price)), Decimal(str(asks[0].price))


def _touch(snapshot: Snapshot) -> dict[str, str]:
    """The best bid and ask of this snapshot's book, when it has one."""
    bid, ask = _prices(snapshot)
    if bid is None or ask is None:
        return {}
    return {"bid": str(bid), "ask": str(ask)}


def _channel(cfg: CycleConfig) -> Channel:
    channel = cfg.channel
    if channel is None:  # pragma: no cover - CycleConfig refuses this
        raise CycleError(f"venue_key {cfg.venue_key!r} is not a channel in the envelope")
    return channel


def _stop_before_asking(
    cfg: CycleConfig, where: CycleDir, result: CycleResult, why: str, step: str = "size"
) -> CycleResult:
    result.refusals.append(why)
    result.stopped_by = why
    announce(cfg.watch, step, REFUSED, why)
    _finish(cfg, where, result)
    return result


def _finish(cfg: CycleConfig, where: CycleDir, result: CycleResult) -> None:
    """Write the cycle down: raw inputs, a hash of every file, the summary, then its index line.

    In that order, so a summary never names a file that is not there yet and an index line never
    points at a summary that was not finished.
    """
    write_gzip_json(
        where.path / INPUTS,
        inputs_record(
            result.snapshot,
            result.frames,
            {
                "interval_s": cfg.interval_s,
                "lookback_bars": cfg.lookback_bars,
                "context_intervals": list(cfg.context_intervals),
                "regime": cfg.regime.record(),
                "mechanism": cfg.mechanism,
            },
        ),
    )
    payload = result.record()
    payload["tokens"] = cfg.usage.tokens
    payload["cost_declared"] = str(cfg.usage.cost)
    payload["finished_at"] = cfg.clock().isoformat()
    payload["files"] = manifest(where.path)
    summary = where.write_summary(payload)
    if not has_index(cfg.root):
        # A desk that ran before the index existed gets one built from its history first, so
        # the counts that read it never start from zero.
        build_index(cfg.root)
    oi = result.snapshot.values.get("open_interest")
    append_index(
        cfg.root,
        index_row(
            payload,
            open_interest=None if oi is None else oi.record(),
            summary_sha256=sha256_of(summary),
        ),
    )


def manifest(folder: Path) -> dict[str, str]:
    """A sha256 per file a cycle wrote, by its path inside the cycle; the summary hashes itself
    into the index instead."""
    return {
        path.relative_to(folder).as_posix(): sha256_of(path)
        for path in sorted(folder.rglob("*"))
        if path.is_file() and path.name != SUMMARY and not path.name.startswith(".")
    }


def budget_spent(error: str) -> bool:
    """Whether a failure was the budget refusing, which ends a cycle rather than an agent."""
    return BudgetSpentError.__name__ in error
