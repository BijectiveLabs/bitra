"""One cycle: what the engine saw, what the agents said, what the cost gate decided.

The order is fixed and short. Code takes a snapshot. Code builds the briefs. Agents read the
briefs, in parallel, and answer inside a schema. Code prices the round trip on the book it is
holding and compares it with the edge the analyst claimed. Only then is there an intent, and in
the default mode that intent goes to a file rather than to a venue.

Everything the cycle touched stays in the cycle's directory, so an auditor can replay the
reasoning without asking anyone to remember it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.agents import (
    macro_analyst,
    onchain_analyst,
    sentiment_analyst,
    technical_analyst,
    validator,
)
from trading_research_agents.agents.briefs import (
    BriefError,
    context_brief,
    cost_brief,
    data_brief,
    declared_state,
    technical_brief,
)
from trading_research_agents.agents.parallel import FanOut, Task, run_tasks
from trading_research_agents.agents.roles import RoleError, prompt
from trading_research_agents.agents.scope import brief_for, crossings, dropped
from trading_research_agents.agents.validator.tools import checked, figures_of, validator_brief
from trading_research_agents.data.schema import Direction
from trading_research_agents.data.series import (
    Event,
    Series,
    SeriesError,
    load_all,
    load_news,
    read_events,
)
from trading_research_agents.data.sources.chain import SourceChain
from trading_research_agents.envelope.model import Channel, Envelope
from trading_research_agents.execution.confluence import STANCES, Confluence, Matrix, confluent
from trading_research_agents.execution.conviction import (
    Conviction,
    Stake,
    score_of,
    stake_for,
)
from trading_research_agents.execution.mandate import MODES as MANDATE_MODES
from trading_research_agents.execution.pretrade import PreTradeVerdict, admit
from trading_research_agents.execution.targets import (
    Targets,
    TargetVerdict,
    intents_today,
    within_targets,
)
from trading_research_agents.llm.completer import Completer
from trading_research_agents.llm.usage import BudgetSpentError, UsageLedger
from trading_research_agents.store.cycles import CycleDir
from trading_research_agents.store.snapshot import Snapshot, cycle_id, take_snapshot

# The third mode sends orders, and it only exists when the caller hands the cycle something to
# send them with. That hand is built by the CLI, and only after the mandate has cleared.
MODES = MANDATE_MODES
TARGETS = {"long": Direction.LONG, "short": Direction.SHORT}
# The analysts that only read and label. They run beside the technical analyst, they never place
# anything, and each one's answer is recorded whether or not the operator gave it a source.
READING: dict[str, Any] = {
    macro_analyst.ROLE: macro_analyst,
    onchain_analyst.ROLE: onchain_analyst,
    sentiment_analyst.ROLE: sentiment_analyst,
}


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
    size: Decimal
    magnitude_bps: Decimal
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
    equity: Decimal | None = None
    size_step: Decimal = Decimal("0.000001")
    reward_to_risk: Decimal | None = None
    # The exit rule as declared, so the cycle can price the stop and the take profit for the venue.
    # Code computes them from the operator's own fractions; no model is asked where a stop goes.
    stop_frac: Decimal | None = None
    take_frac: Decimal | None = None
    # [agents].validators: the stances to ask once there is a proposal worth arguing about. Each
    # stance is one more call, so an operator turns them on deliberately.
    validators: tuple[str, ...] = ()
    # What actually sends an order, when the operator is in venue mode and the mandate cleared.
    place: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    # A reading analyst is asked only when the roster names it. `tra run` names the ones with a
    # declared source, because paying a model to say it was given nothing is waste; an operator who
    # wants a role asked anyway, on notes alone, names it in [agents].analysts.
    data_options: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    series: Mapping[str, tuple[Path, ...]] = field(default_factory=dict)
    news: tuple[Path, ...] = ()
    analysts: tuple[str, ...] = (technical_analyst.ROLE,)
    resume: bool = False
    max_workers: int = 4
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
        if not isinstance(self.size, Decimal) or self.size <= 0:
            raise CycleError("size is a positive Decimal")
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
    confluence: Confluence | None = None
    stake: Stake | None = None
    verdict: PreTradeVerdict | None = None
    targets: TargetVerdict | None = None
    intent: dict[str, Any] | None = None
    placed: dict[str, Any] | None = None
    refusals: list[str] = field(default_factory=list)
    stopped_by: str | None = None
    path: Path | None = None

    @property
    def acted(self) -> bool:
        return self.intent is not None

    def record(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle,
            "instrument": self.instrument,
            "data": self.briefs.get("data"),
            "agents": self.fan.record(),
            "validators": None if self.debate is None else self.debate.record(),
            "analyst": None if self.analyst is None else vars(self.analyst),
            "context": self.context,
            "confluence": None if self.confluence is None else self.confluence.record(),
            "conviction": None if self.stake is None else self.stake.record(),
            "cost": None if self.verdict is None else self.verdict.record(),
            "targets": None if self.targets is None else self.targets.record(),
            "intent": self.intent,
            "placed": self.placed,
            "refusals": self.refusals,
            "stopped_by": self.stopped_by,
            "mode": self.briefs.get("mode"),
        }


def _ask(
    cfg: CycleConfig, role: str, question: Mapping[str, Any], schema: Mapping[str, Any]
) -> str:
    completer = cfg.completer_for(role)
    done = completer.complete(
        system=prompt(role),
        user=json.dumps(question, sort_keys=True, default=str),
        schema=dict(schema),
    )
    return done.text


def run_cycle(cfg: CycleConfig) -> CycleResult:
    """Take one snapshot, ask the agents, price the trade, and write the cycle down."""
    cycle = cycle_id(cfg.clock())
    where = CycleDir(root=cfg.root, cycle=cycle).make()
    kinds = list(cfg.chains)
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

    try:
        technical = technical_brief(
            snapshot,
            mechanism=cfg.mechanism,
            interval_s=cfg.interval_s,
            lookback_bars=cfg.lookback_bars,
        )
    except BriefError as exc:
        result.stopped_by = f"no technical brief: {exc}"
        _finish(cfg, where, result)
        return result
    asked = _within_scope(technical_analyst.ROLE, technical.record(), result)
    where.brief("technical").write_text(
        json.dumps(asked, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )

    tasks = [
        Task(
            name="technical",
            work=lambda: _ask(cfg, technical_analyst.ROLE, asked, technical_analyst.SCHEMA),
            path=where.answer("technical"),
        )
    ]
    calendar = _calendar(cfg, result)
    reading_briefs: dict[str, dict[str, Any]] = {}
    for role in cfg.analysts:
        if role not in READING:
            continue
        built = _reading_brief(cfg, role, snapshot, calendar, result)
        if built is None:
            continue
        brief = _within_scope(role, built, result)
        reading_briefs[role] = brief
        where.brief(role).write_text(
            json.dumps(brief, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        module = READING[role]
        tasks.append(
            Task(
                name=role,
                work=_reading_task(cfg, module, brief),
                path=where.answer(role),
            )
        )

    fan = run_tasks(tasks, max_workers=cfg.max_workers, resume=cfg.resume)
    result.fan = fan
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

    if analyst.target == "flat":
        result.refusals.append("technical: flat, so there is nothing to price")
        _finish(cfg, where, result)
        return result
    if analyst.confidence < cfg.min_confidence:
        result.refusals.append(
            f"technical: confidence {analyst.confidence} below the declared {cfg.min_confidence}"
        )
        _finish(cfg, where, result)
        return result

    direction = TARGETS[analyst.target]
    try:
        costs = cost_brief(
            snapshot,
            channel=_channel(cfg),
            size=cfg.size,
            direction=direction,
            horizon_bars=analyst.horizon_bars,
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
    where.brief("cost").write_text(
        json.dumps(costs.record(), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )

    if cfg.validators:
        _run_validators(cfg, where, result, asked=asked, analyst=analyst, costs=costs)

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
        if not agreement.admitted:
            _finish(cfg, where, result)
            return result

    size = cfg.size
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
                votes=_votes(result),
            ),
            base_size=cfg.size,
            price=costs.cost.mid,
            max_position=cfg.max_position if cfg.max_position is not None else cfg.size,
            equity=cfg.equity,
            step=cfg.size_step,
        )
        result.stake = stake
        for reason in stake.reasons:
            result.refusals.append(f"conviction: {reason}")
        if not stake.admitted:
            _finish(cfg, where, result)
            return result
        size = stake.size
        if size != cfg.size:
            # Impact is a function of the size that actually goes out, so the gate prices the trade
            # it is admitting rather than the one the operator declared as a base.
            try:
                costs = cost_brief(
                    snapshot,
                    channel=_channel(cfg),
                    size=size,
                    direction=direction,
                    horizon_bars=analyst.horizon_bars,
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
            where.brief("cost").write_text(
                json.dumps(costs.record(), indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )

    verdict = admit(
        edge_bps=cfg.magnitude_bps,
        cost=costs.cost,
        margin_bps=cfg.margin_bps,
        edge_source="[agents].magnitude_bps, declared by the operator and not measured here",
    )
    result.verdict = verdict
    if not verdict.admitted:
        result.refusals.append(f"cost gate: {verdict.reason}")
        _finish(cfg, where, result)
        return result

    if not cfg.targets.idle:
        met = within_targets(
            cfg.targets,
            net_bps=verdict.net_bps,
            reward_to_risk=cfg.reward_to_risk,
            round_trips_today=intents_today(cfg.root, at=cfg.clock(), instrument=cfg.instrument),
        )
        result.targets = met
        for reason in met.reasons:
            result.refusals.append(f"targets: {reason}")
        if not met.admitted:
            _finish(cfg, where, result)
            return result

    result.intent = {
        "instrument": cfg.instrument,
        "target": analyst.target,
        "size": str(size),
        "base_size": str(cfg.size),
        "conviction": None if result.stake is None else str(result.stake.score.score),
        "horizon_bars": analyst.horizon_bars,
        "confidence": analyst.confidence,
        "reason": analyst.reason,
        "edge_bps": str(cfg.magnitude_bps),
        "edge_source": verdict.edge_source,
        "net_bps": str(verdict.net_bps),
        "mode": cfg.mode,
        "mid": str(costs.cost.mid),
        "at": cycle,
        # The touch this cycle saw, so an order that rests on the book rests where the cost was
        # priced rather than wherever the book has moved to by the time it is sent.
        **_touch(snapshot),
        **_exits(cfg, costs.cost.mid, analyst.target),
        "channel": cfg.venue_key,
        "entry_role": _channel(cfg).entry_role,
    }
    with (where.path / "intents.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result.intent, sort_keys=True) + "\n")

    if cfg.mode == "venue" and cfg.place is not None:
        # The intent is already on disk. Whatever the venue does next, the record of what this
        # cycle decided survives it.
        try:
            result.placed = dict(cfg.place(result.intent))
        except Exception as exc:
            result.placed = {"sent": False, "error": f"{exc.__class__.__name__}: {exc}"}
            result.refusals.append(f"venue: {exc.__class__.__name__}: {exc}")
        else:
            where.path.joinpath("placed.json").write_text(
                json.dumps(result.placed, indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
    _finish(cfg, where, result)
    return result


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
            series = (*series, *load_all(declared))
        except SeriesError as exc:
            # One unreadable file does not silence a role that has other shelves to read from.
            result.refusals.append(f"{role} {declared.name}: {exc}")
    if files and not series:
        # Everything this role was given is unreadable. Asking anyway would pay a model to say it
        # saw nothing, and would hide a broken file behind a shrug.
        result.refusals.append(f"{role}: nothing readable in {[p.name for p in files]}; not asked")
        return None
    try:
        return context_brief(role, snapshot, series=series, events=calendar)
    except BriefError as exc:
        result.refusals.append(f"{role}: {exc}")
        return None


def _reading_task(cfg: CycleConfig, module: Any, brief: dict[str, Any]) -> Callable[[], str]:
    def ask() -> str:
        return _ask(cfg, module.ROLE, brief, module.SCHEMA)

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
        if role not in fan.answers:
            continue
        try:
            said = vars(module.parse(str(fan.answers[role])))
        except RoleError as exc:
            out[role] = {"error": str(exc)}
            continue
        declared = declared_state(role, briefs.get(role, {}))
        label = said.get("regime") or said.get("signal") or said.get("state")
        said["declared_state"] = declared
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
) -> None:
    """Ask each declared stance about the proposal, then keep only the claims that cite a figure."""
    figures = figures_of(asked)
    proposal = {
        "target": analyst.target,
        "confidence": analyst.confidence,
        "horizon_bars": analyst.horizon_bars,
        "reason": analyst.reason,
        "mechanism": cfg.mechanism,
        "edge_bps": str(cfg.magnitude_bps),
    }
    states = {
        role: said.get("declared_state", {})
        for role, said in result.context.items()
        if isinstance(said.get("declared_state"), Mapping)
    }
    tasks = []
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
        where.brief(name).write_text(
            json.dumps(brief, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        tasks.append(Task(name=name, work=_validator_task(cfg, brief), path=where.answer(name)))

    fan = run_tasks(tasks, max_workers=cfg.max_workers, resume=cfg.resume)
    result.debate = fan
    for name, error in fan.failed.items():
        result.refusals.append(f"{name}: {error}")
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
        verdict = checked(said, figures)
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
        return _ask(cfg, validator.ROLE, brief, validator.SCHEMA)

    return ask


def _votes(result: CycleResult) -> tuple[str, ...]:
    """The validators' verdicts, after code checked the claims they rest on."""
    out = []
    for role, said in sorted(result.context.items()):
        if role.startswith("validator") and isinstance(said.get("verdict"), str):
            out.append(str(said["verdict"]))
    return tuple(out)


def _exits(cfg: CycleConfig, mid: Decimal, target: str) -> dict[str, str]:
    """Where the stop and the take profit sit, from the operator's own fractions and this price.

    A position without an exit is not a trade, so these travel with the intent and, in venue mode,
    go to the venue attached to the entry. Nothing here asks a model where a stop belongs.
    """
    if cfg.stop_frac is None or cfg.take_frac is None or mid <= 0:
        return {}
    away = Decimal(1) if target == "long" else Decimal(-1)
    stop = mid * (Decimal(1) - away * cfg.stop_frac)
    take = mid * (Decimal(1) + away * cfg.take_frac)
    return {"stop_price": str(stop.quantize(mid)), "take_price": str(take.quantize(mid))}


def _touch(snapshot: Snapshot) -> dict[str, str]:
    """The best bid and ask of this snapshot's book, when it has one."""
    try:
        book = snapshot.get("orderbook").value
    except KeyError:
        return {}
    bids, asks = getattr(book, "bids", ()), getattr(book, "asks", ())
    if not bids or not asks:
        return {}
    return {"bid": str(bids[0].price), "ask": str(asks[0].price)}


def _channel(cfg: CycleConfig) -> Channel:
    channel = cfg.channel
    if channel is None:  # pragma: no cover - CycleConfig refuses this
        raise CycleError(f"venue_key {cfg.venue_key!r} is not a channel in the envelope")
    return channel


def _finish(cfg: CycleConfig, where: CycleDir, result: CycleResult) -> None:
    payload = result.record()
    payload["tokens"] = cfg.usage.tokens
    payload["cost_declared"] = str(cfg.usage.cost)
    payload["finished_at"] = cfg.clock().isoformat()
    where.write_summary(payload)


def budget_spent(error: str) -> bool:
    """Whether a failure was the budget refusing, which ends a cycle rather than an agent."""
    return BudgetSpentError.__name__ in error
