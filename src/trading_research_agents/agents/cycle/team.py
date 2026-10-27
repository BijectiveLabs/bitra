"""The deciding team: who reads which figures, how their answers are checked, how they combine.

The lead is the structure lead. A peer, when declared, reads the lead's brief blind and answers
in the lead's schema; the two must name the same side, and the team's confidence is the lower of
theirs. A flow specialist, when declared, reads the flow figures, which then leave the technical
brief; its side and confidence become a declared state that conviction and confluence weigh. Every
answer's citations are checked by code against the brief that member was given.

The typed judge, when declared, reads each structure answer's reason beside the labels code
measured (the regime, the structure, the regime on each timeframe) and says how likely the reason
describes the market that way. Below one half the trade is refused. Numbers and dates are never
asked of it: code checks those.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from trading_research_agents.agents import blind_confirmer, flow_specialist
from trading_research_agents.agents.cited import survived
from trading_research_agents.agents.cycle.asking import _ask_readable, _within_scope
from trading_research_agents.agents.cycle.record import _finish
from trading_research_agents.agents.cycle.result import _said
from trading_research_agents.agents.cycle.state import Pass
from trading_research_agents.agents.parallel import Task
from trading_research_agents.agents.progress import DONE, REFUSED, announce, timed
from trading_research_agents.agents.roles import RoleError
from trading_research_agents.agents.scope import FLOW, FLOW_READS
from trading_research_agents.agents.validator.tools import figures_of
from trading_research_agents.llm.judge import Check, JudgeError
from trading_research_agents.records import write_json

PEER, FLOW_SEAT = blind_confirmer.ROLE, flow_specialist.ROLE
DESCRIBES = Check(
    "Does the analyst's reason describe the market's trend and regime the way the measured "
    "labels in this state do?"
)
AGREES = 0.5
WORDS = {"long": "bullish", "short": "bearish"}


def split(shown: Mapping[str, Any], team: tuple[str, ...]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The structure brief and the flow brief; each flow figure has one reader."""
    if FLOW_SEAT not in team:
        return dict(shown), {}
    flow = {k: shown[k] for k in FLOW_READS if k in shown}
    return {k: v for k, v in shown.items() if k not in FLOW}, flow


def tasks(p: Pass) -> list[Task]:
    """The members beside the lead, each on its own brief, written down before they are asked."""
    cfg = p.cfg
    out = []
    members: list[tuple[str, str, Mapping[str, Any], Any]] = []
    if PEER in cfg.team:
        members.append(("blind_confirmer", PEER, p.asked, blind_confirmer))
    if FLOW_SEAT in cfg.team:
        flow = _within_scope(FLOW_SEAT, p.flow_brief, p.result)
        p.flow_brief = flow
        members.append(("flow_specialist", FLOW_SEAT, flow, flow_specialist))
    for name, role, brief, package in members:
        write_json(p.where.brief(name), brief)
        out.append(
            Task(
                name=name,
                work=timed(cfg.watch, name, _asker(p, role, brief, package)),
                path=p.where.answer(name),
            )
        )
    return out


def _asker(p: Pass, role: str, brief: Mapping[str, Any], package: Any) -> Callable[[], str]:
    def ask() -> str:
        return _ask_readable(p.cfg, role, brief, package.SCHEMA, package.parse)

    return ask


def combine(p: Pass) -> bool:
    """The flow read recorded as a voice; the peer's read held against the lead's."""
    p.team_reads = {"structure_lead": p.analyst.target} if p.cfg.team else {}
    if FLOW_SEAT in p.cfg.team:
        _read_flow(p)
    if PEER not in p.cfg.team:
        return True
    peer = _usable(p, "blind_confirmer", PEER, blind_confirmer.parse, p.asked)
    if peer is None:
        why = (
            "blind_confirmer: no usable answer, and the team trades only when both reads agree; "
            "no trade"
        )
        return _refuse(p, why, PEER, "no usable answer")
    p.result.context[PEER] = _said(peer)
    p.team_reads[PEER] = peer.target
    lead = p.analyst
    if peer.target != lead.target:
        why = (
            f"structure_lead: the lead reads {lead.target} and the blind confirmer "
            f"{peer.target}; the structure reads disagree, so there is no trade"
        )
        return _refuse(p, why, PEER, "the reads disagree")
    p.analyst = replace(lead, confidence=min(lead.confidence, peer.confidence))
    p.result.analyst = p.analyst
    announce(p.cfg.watch, PEER, DONE, f"agrees: {peer.target}")
    return True


def _read_flow(p: Pass) -> None:
    flow = _usable(p, "flow_specialist", FLOW_SEAT, flow_specialist.parse, p.flow_brief)
    if flow is None:
        p.result.context[FLOW_SEAT] = {"read_by": FLOW_SEAT, "error": "no usable answer"}
        return
    sign = {"long": 1, "short": -1}.get(flow.target, 0)
    state = {"declared": WORDS.get(flow.target, "undeclared"), "score": str(sign * flow.confidence)}
    p.result.context[FLOW_SEAT] = {
        "declared_state": state,
        "read_by": FLOW_SEAT,
        "target": flow.target,
        "confidence": flow.confidence,
        "reason": flow.reason,
        "cited": [figure.record() for figure in flow.figures_used],
    }
    p.team_reads[FLOW_SEAT] = flow.target
    announce(p.cfg.watch, "flow_specialist", DONE, f"{flow.target} at {flow.confidence}")


def _usable(
    p: Pass, name: str, role: str, parse: Callable[[str], Any], brief: Mapping[str, Any]
) -> Any:
    """A member's answer when it parses and rests on at least one figure it was given."""
    result = p.result
    text = p.answers.get(name)
    if text is None:
        return None
    try:
        said = parse(str(text))
    except (RoleError, ValueError) as exc:
        result.refusals.append(f"{role}: {exc}")
        return None
    kept, lost = survived(said.figures_used, figures_of(brief))
    for gone in lost:
        result.refusals.append(f"{role}: discarded a citation, {gone}")
    if not kept:
        result.refusals.append(f"{role}: not one figure from its own brief survived the check")
        return None
    return replace(said, figures_used=tuple(kept))


def _refuse(p: Pass, why: str, step: str, detail: str) -> bool:
    p.result.refusals.append(why)
    announce(p.cfg.watch, step, REFUSED, detail)
    _finish(p.cfg, p.where, p.result)
    return False


def check_reasons(p: Pass) -> bool:
    """The judge's reading of each structure reason against the measured labels, when declared."""
    judge = p.cfg.judge
    if judge is None:
        return True
    labels = {
        "regime": p.asked.get("regime"),
        "market_structure": p.asked.get("market_structure"),
        "timeframes": p.asked.get("timeframes"),
    }
    reasons = {"structure_lead": p.analyst.reason}
    peer = p.result.context.get(PEER)
    if isinstance(peer, Mapping) and peer.get("reason"):
        reasons[PEER] = str(peer["reason"])
    for member, reason in reasons.items():
        try:
            said = judge.evaluate({**labels, "reason": reason}, {"describes": DESCRIBES})
        except (JudgeError, LookupError) as exc:
            p.result.refusals.append(f"judge: {member} not checked, {exc}")
            continue
        answer = said["describes"]
        agrees = float(getattr(answer, "probability", 0.0))
        p.result.context[f"{member}_check"] = {"model": said.model, "describes": agrees}
        if agrees < AGREES:
            why = (
                f"{member}: the judge read its reason as contradicting the measured regime and "
                f"structure ({agrees:.2f} that it describes them), so there is no trade"
            )
            return _refuse(p, why, "judge", f"{member}'s reason contradicts the measurements")
    return True


__all__ = ["check_reasons", "combine", "split", "tasks"]
