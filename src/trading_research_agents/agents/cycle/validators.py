"""The validators: the stances asked about a proposal, their checked claims, and the votes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from trading_research_agents.agents import (
    validator,
)
from trading_research_agents.agents.cycle.asking import JUDGE_PROMPT, _ask_readable, _within_scope
from trading_research_agents.agents.cycle.config import CycleConfig
from trading_research_agents.agents.cycle.pricing import _held
from trading_research_agents.agents.cycle.record import budget_spent
from trading_research_agents.agents.cycle.result import CycleResult
from trading_research_agents.agents.parallel import Task, run_tasks
from trading_research_agents.agents.progress import (
    DONE,
    announce,
    timed,
)
from trading_research_agents.agents.roles import RoleError
from trading_research_agents.agents.scope import crossings
from trading_research_agents.agents.validator.tools import checked, figures_of, validator_brief
from trading_research_agents.execution.desk import within_desk
from trading_research_agents.execution.targets import (
    edge_multiple,
    intents_today,
)
from trading_research_agents.records import (
    write_json,
)
from trading_research_agents.store.cycles import CycleDir


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


def _run_validators(
    cfg: CycleConfig,
    where: CycleDir,
    result: CycleResult,
    *,
    asked: Mapping[str, Any],
    analyst: Any,
) -> None:
    """Ask each declared stance about the proposal, then keep only the claims that cite a figure.

    The proposal is the analyst's reading as given. The round trip, the claimed edge and the
    desk's rules are left out: whether a trade clears them is code's question, and a vote that
    could see them would be a second, softer cost gate.

    With neutral beside pro or contra, the advocates build their cases apart, neither seeing the
    other's, and neutral judges the two cases once code has checked every claim in them.
    """
    # The analyst's last reading is its own continuity, not evidence about this proposal.
    figures = {k: v for k, v in figures_of(asked).items() if not k.startswith("previous.")}
    proposal = {
        "target": analyst.target,
        "setup": analyst.setup,
        "confidence": analyst.confidence,
        "horizon_bars": analyst.horizon_bars,
        "stop_price": None if analyst.stop_price is None else str(analyst.stop_price),
        "target_price": None if analyst.target_price is None else str(analyst.target_price),
        "reason": analyst.reason,
        "mechanism": cfg.mechanism,
    }
    judged = judge_mode(cfg.validators)
    tasks = []
    seen: dict[str, dict[str, str]] = {}
    for stance in cfg.validators:
        if judged and stance == "neutral":
            continue
        name = f"validator_{stance}"
        brief = _within_scope(
            validator.ROLE,
            validator_brief(
                instrument=cfg.instrument,
                stance=stance,
                proposal=proposal,
                figures=figures,
            ),
            result,
        )
        write_json(where.brief(name), brief)
        # What this validator may cite is what this validator was given, the proposal's own
        # figures, rather than a narrower set it never saw.
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
        _record_verdict(result, name, answer, seen.get(name, figures))
    if judged and result.stopped_by is None:
        _judge(cfg, where, result, proposal=proposal, figures=figures)


def judge_mode(stances: tuple[str, ...]) -> bool:
    """Neutral judges the advocates' cases when at least one advocate is asked beside it."""
    return "neutral" in stances and bool({"pro", "contra"} & set(stances))


def _record_verdict(
    result: CycleResult, name: str, answer: Any, figures: Mapping[str, str]
) -> dict[str, Any] | None:
    stance = name.partition("_")[2]
    try:
        said = validator.parse(str(answer))
    except (RoleError, ValueError) as exc:
        result.context[name] = {"stance": stance, "error": str(exc)}
        result.refusals.append(f"{name}: {exc}")
        return None
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
    return record


def _judge(
    cfg: CycleConfig,
    where: CycleDir,
    result: CycleResult,
    *,
    proposal: Mapping[str, Any],
    figures: Mapping[str, str],
) -> None:
    """Neutral reads both cases, as code checked them, and its verdict is the one that counts.

    Only claims that survived the check reach it, so an invented figure never gets as far as being
    weighed. When there is nothing to weigh, code says so without paying for a call: no case on
    either side is an abstention, and a case nobody answered is uncontested.
    """
    name = "validator_neutral"
    cases = {s: result.context.get(f"validator_{s}") or {} for s in ("pro", "contra")}
    asked = [s for s in ("pro", "contra") if s in cfg.validators]
    silent = [s for s in asked if not cases[s] or "error" in cases[s]]
    case_for = list(cases["pro"].get("claims") or [])
    case_against = list(cases["contra"].get("claims") or [])
    decided = ""
    verdict = "abstain"
    if silent:
        decided = f"{' and '.join(silent)} did not answer, so there are not two cases to weigh"
    elif not case_for and not case_against:
        decided = "neither side made a claim that survived the check"
    elif not case_against and "contra" in asked:
        verdict, decided = "support", "contra found nothing in the figures against the proposal"
    elif not case_for and "pro" in asked:
        verdict, decided = "oppose", "pro found nothing in the figures for the proposal"
    if decided:
        result.context[name] = {
            "stance": "neutral",
            "verdict": verdict,
            "claims": [],
            "discarded": [],
            "reason": f"{decided}; judged by code, neutral was not asked",
            "judged": "code",
        }
        announce(cfg.watch, name, DONE, f"{verdict}: {decided}")
        return
    brief = _within_scope(
        validator.ROLE,
        {
            **validator_brief(
                instrument=cfg.instrument, stance="neutral", proposal=proposal, figures=figures
            ),
            "case_for": case_for,
            "case_against": case_against,
        },
        result,
    )
    write_json(where.brief(name), brief)
    work = timed(
        cfg.watch,
        name,
        lambda: _ask_readable(
            cfg, validator.ROLE, brief, validator.SCHEMA, validator.parse, system=JUDGE_PROMPT
        ),
    )
    try:
        answer = work()
    except Exception as exc:
        error = f"{exc.__class__.__name__}: {exc}"
        result.context[name] = {"stance": "neutral", "error": error}
        result.refusals.append(f"{name}: {error}")
        if budget_spent(error):
            result.stopped_by = f"{name}: {error}"
        return
    write_json(where.answer(name), answer)
    record = _record_verdict(result, name, answer, figures_of(brief))
    if record is not None:
        record["judged"] = "neutral"


def _validator_task(cfg: CycleConfig, brief: Mapping[str, Any]) -> Callable[[], str]:
    def ask() -> str:
        return _ask_readable(cfg, validator.ROLE, brief, validator.SCHEMA, validator.parse)

    return ask


def _counted(result: CycleResult) -> list[tuple[str, Mapping[str, Any]]]:
    """The verdicts that count: the judge's alone when there was one, else every stance's."""
    rows = [
        (role, said)
        for role, said in sorted(result.context.items())
        if role.startswith("validator") and isinstance(said.get("verdict"), str)
    ]
    return [row for row in rows if row[1].get("judged")] or rows


def _votes(result: CycleResult) -> tuple[str, ...]:
    """The validators' verdicts, after code checked the claims they rest on."""
    return tuple(str(said["verdict"]) for _, said in _counted(result))


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
    for role, said in _counted(result):
        verdict = str(said["verdict"])
        stance = role.partition("_")[2]
        out.append(STANCE_SIGNAL.get(stance, {}).get(verdict, verdict))
    return tuple(out)
