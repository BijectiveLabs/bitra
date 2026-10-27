"""How one cycle reads to a person: the live table's steps, the plain report, the team's lines."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from trading_research_agents.cli.common import (
    _cycle_ui,
)
from trading_research_agents.readings import READINGS


def _cycle_steps(cfg: Any) -> list[str]:
    """The pipeline this project configured, in order, so the live table starts complete.

    An operator watching a cycle should see every step that can happen, including the ones that are
    not configured - a table that grows as it goes tells you less than one that was there first.
    """
    steps = ["data"]
    if not cfg.trigger.idle:
        steps.append("trigger")
    if cfg.size_quote is not None:
        steps.append("size")
    if cfg.account is not None:
        steps.append("capital")
    steps.extend(["regime", "structure_lead"])
    # The members beside the lead report under their seat names.
    steps.extend(cfg.team)
    if cfg.judge is not None:
        steps.append("judge")
    # Every reading has a row, read or not; the row says which.
    steps.extend(["macro", "onchain", "sentiment"])
    steps.extend(["cost", "exit", "paths"])
    steps.extend(f"reviewer_{stance}" for stance in cfg.reviewers)
    if not cfg.matrix.idle:
        steps.append("confluence")
    if not cfg.conviction.idle:
        steps.append("conviction")
    if cfg.max_risk_per_trade_bps is not None:
        steps.append("risk")
    if cfg.desk.bounds_a_trade:
        steps.append("desk")
    steps.append("cost gate")
    if not cfg.targets.idle:
        steps.append("targets")
    steps.extend(["risk gate", "intent"])
    if cfg.mode == "venue":
        steps.append("venue")
    return steps


def order_outcome(placed: Mapping[str, Any]) -> str:
    """What became of a sent order, in one phrase, from the record the venue hand wrote."""
    if placed.get("interrupted"):
        return str(placed.get("note") or "interrupted")
    filled = placed.get("filled")
    if placed.get("note"):
        return str(placed["note"])
    if filled not in (None, "", "0", "0.0"):
        return f"filled {filled}" + (f" at {placed['average']}" if placed.get("average") else "")
    return str(placed.get("status") or "sent; the venue reported no state")


def _cycle_view(payload: Mapping[str, Any]) -> Any:
    return _cycle_ui().cycle_view(payload)


def _team_lines(context: Mapping[str, Any]) -> list[str]:
    """The members beside the lead, and the judge's reading of each structure reason."""
    lines = []
    confirmer = context.get("blind_confirmer")
    if isinstance(confirmer, Mapping) and confirmer.get("target"):
        lines.append(
            f"confirmer: {confirmer['target']} on {confirmer.get('setup')} at "
            f"{confirmer.get('confidence')}: {confirmer.get('reason')}"
        )
    flow = context.get("flow_specialist")
    if isinstance(flow, Mapping):
        said = flow.get("error") or f"{flow.get('target')} at {flow.get('confidence')}"
        lines.append(f"flow:      {said}" + (f": {flow['reason']}" if flow.get("reason") else ""))
    for member in ("structure_lead", "blind_confirmer"):
        check = context.get(f"{member}_check")
        if isinstance(check, Mapping):
            lines.append(
                f"judge:     {member} describes the measurements at {check.get('describes')} "
                f"({check.get('model')})"
            )
    return lines


def _cycle_report(result: Any, usage: Any, mode: str) -> tuple[dict[str, Any], list[str]]:
    """One cycle as a record and as the lines a person reads."""
    payload = result.record()
    payload["tokens"] = usage.tokens
    payload["out"] = str(result.path)
    spent = f"{result.fan.seconds}s of agents"
    if result.debate is not None:
        spent += f", {result.debate.seconds}s of reviewers"
    lines = [f"cycle {result.cycle} {result.instrument}: {spent}"]
    lines.extend(_data_lines(result))
    lines.extend(_voice_lines(result))
    lines.extend(_reviewer_lines(result))
    lines.extend(_sizing_lines(result))
    lines.extend(_exit_lines(result))
    lines.extend(_path_lines(result.briefs.get("paths")))
    lines.extend(_target_lines(result))
    lines.extend(_cost_lines(result))
    lines.extend(_intent_lines(result, mode))
    for refusal in result.refusals[:6]:
        lines.append(f"  refused {refusal}")
    if result.stopped_by:
        lines.append(f"stopped by: {result.stopped_by}")
    lines.append(f"snapshot.json, briefs, answers and cycle.json in {result.path}")
    return payload, lines


def _path_lines(paths: Mapping[str, Any] | None) -> list[str]:
    """How the instrument's own paths read the lead's levels."""
    if not paths:
        return []
    if "absent" in paths:
        return [f"paths:     not read, {paths['absent']}"]
    return [
        f"paths:     take first {paths['take_first']:.2f}, stop first {paths['stop_first']:.2f}, "
        f"neither {paths['neither']:.2f} over {paths['horizon_bars']} bars "
        f"({paths['paths']} paths); {paths['expected_bps']:+.2f} bps net of the round trip"
    ]


def _data_lines(result: Any) -> list[str]:
    """What the cycle read: each kind of data, each series, and what it could not read."""
    lines: list[str] = []
    data = result.briefs.get("data") or {}
    for kind, value in sorted((data.get("values") or {}).items()):
        lines.append(f"  {kind:<14} {value['source']:<18} age {value['age_s']}s")
    for row in result.briefs.get("series") or []:
        lines.append(
            f"  {row['series']:<14} {row['value']} as of {str(row['observed_at'])[:16]}, "
            f"{row['age_h']}h old, {row['state']}; {row['origin']}"
        )
    for name, why in sorted((result.briefs.get("not_read") or {}).items()):
        lines.append(f"{name:<10} {why}")
    return lines


def _voice_lines(result: Any) -> list[str]:
    """What each voice said: the readings, the team, the deciding analyst, or why none was asked."""
    lines: list[str] = []
    for name, said in sorted(result.context.items()):
        if name not in READINGS:
            continue
        declared = said.get("declared_state") or {}
        word = declared.get("declared", "undeclared")
        spoken = declared.get("in_its_words")
        state = f"{word} ({spoken})" if spoken else word
        lines.append(f"{name:<10} read by code: {state}")
    lines.extend(_team_lines(result.context))
    if result.analyst is not None:
        lines.append(
            f"lead:      {result.analyst.target} on {result.analyst.setup} at "
            f"{result.analyst.confidence:.2f} over {result.analyst.horizon_bars} bars: "
            f"{result.analyst.reason}"
        )
        if result.cited:
            read = ", ".join(f"{f['figure']}={f['value']}" for f in result.cited)
            lines.append(f"read: {read}")
    if result.wake is not None and not result.wake.ask:
        lines.append(f"held: {result.wake.reasons[0]}")
        if result.wake.moved_bps is not None:
            lines.append(
                f"the mid moved {result.wake.moved_bps} bps against the "
                f"{result.wake.required_bps} bps this desk asks for before it looks"
            )
    return lines


def _reviewer_lines(result: Any) -> list[str]:
    lines: list[str] = []
    for role, said in sorted(result.context.items()):
        if not role.startswith("reviewer"):
            continue
        verdict = said.get("verdict") or f"failed: {said.get('error', '')}"
        cited = len(said.get("claims") or ())
        dropped_claims = len(said.get("discarded") or ())
        counted = f"{cited} cited figure(s)"
        if dropped_claims:
            counted += f", {dropped_claims} discarded"
        lines.append(f"{role.replace('_', ' '):<18} {verdict} on {counted}")
    return lines


def _sizing_lines(result: Any) -> list[str]:
    """How the voices sized the trade: confluence, conviction, stake and risk at the stop."""
    lines: list[str] = []
    if result.confluence is not None:
        agreement = result.confluence
        states = ", ".join(
            f"{role.replace('_analyst', '')} {state}"
            for role, state in sorted(agreement.states.items())
        )
        counted = f"support {agreement.support}, opposition {agreement.opposition}"
        lines.append(f"confluence: {states or 'no role required'}; {counted}")
    if result.stake is not None:
        stake = result.stake
        score = stake.score
        most = dict(score.weights)
        voices = ", ".join(
            f"{role.replace('_analyst', '')} {value}"
            + (f" of {most[role]}" if role in most else "")
            for role, value in score.contributions
        )
        lines.append(f"conviction: {score.score} of 1.0 ({voices})")
        if stake.admitted:
            leverage = "" if stake.leverage is None else f", {stake.leverage}x equity"
            lines.append(f"stake: {stake.size} at {stake.multiple} of the base size{leverage}")
    if result.risk is not None and result.risk.risk_bps is not None:
        risk = result.risk
        lines.append(
            f"risk: {risk.risk_bps} bps of equity at the stop against the declared "
            f"{risk.allowed_bps} bps, size {risk.size}"
        )
    return lines


def _exit_lines(result: Any) -> list[str]:
    """Where the exits sit, what they need to break even, and how they were measured."""
    lines: list[str] = []
    if result.exits:
        shown = result.exits
        said = [f"exits: {shown['side']} at about {shown['entry']}"]
        for label, name in (("take profit", "take"), ("stop loss", "stop")):
            if f"{name}_price" in shown:
                pnl = shown.get(f"{name}_pnl_bps", shown[f"{name}_bps"])
                said.append(f"{label} {shown[f'{name}_price']} (position {pnl} bps)")
        lines.append(", ".join(said))
        even = (result.intent or {}).get("break_even_win_rate")
        if even is not None:
            lines.append(
                f"break even: {Decimal(even) * 100:.1f}% of trades like this must reach the take "
                "profit to cover the stops and the round trip"
            )
        measured = shown.get("measured")
        if measured:
            held = f", held at {measured['clamped_to']}" if measured["clamped_to"] else ""
            if measured.get("source") == "analyst":
                lines.append(
                    f"exits from the analyst: stop named {measured['proposed_stop_bps']} bps away"
                    f"{held}, take profit {measured['take_bps']} bps; the market's own range "
                    f"(ATR {measured['atr_bps']} bps) would have put the stop at "
                    f"{measured['measured_stop_bps']} bps"
                )
            else:
                lines.append(
                    f"exits measured: ATR {measured['atr_bps']} bps over "
                    f"{measured['horizon_bars']} bar(s) put the stop at "
                    f"{measured['measured_stop_bps']} bps{held}"
                )
            for note in measured.get("notes") or ():
                lines.append(f"  {note}")
    return lines


def _target_lines(result: Any) -> list[str]:
    lines: list[str] = []
    if result.targets is not None:
        met = result.targets
        lines.append(
            f"targets: net {met.net_bps.quantize(Decimal('0.01'))} bps, "
            f"reward-to-risk {met.reward_risk}, {met.round_trips_today} round trip(s) today"
        )
        if met.edge_multiple is not None:
            lines.append(f"edge over cost: {met.edge_multiple}x the round trip")
        if met.research_bps is not None:
            lines.append(
                f"research: {met.research_bps} bps of this trade's notional in model calls "
                "since the last intent"
            )
    return lines


def _cost_lines(result: Any) -> list[str]:
    lines: list[str] = []
    if result.verdict is not None:
        cost = result.verdict.cost
        total = cost.round_trip_bps.quantize(Decimal("0.01"))
        guessed = cost.estimated_bps.quantize(Decimal("0.01"))
        lines.append(f"cost: {total} bps round trip, {guessed} of it estimated")
        lines.append(f"gate: {result.verdict.reason}")
        lines.append(
            f"the edge is {result.verdict.edge_certainty.value}: {result.verdict.edge_source}"
        )
        for component in cost.components:
            row = component.record()
            lines.append(f"  {row['name']:<18} {row['bps']:>10} bps  {row['certainty']}")
    return lines


def _intent_lines(result: Any, mode: str) -> list[str]:
    lines: list[str] = []
    if result.intent is not None:
        lines.append(f"intent: {result.intent['target']} {result.intent['size']} ({mode})")
        placed = result.placed or {}
        if placed.get("sent"):
            at = "" if not placed.get("average") else f" at {placed['average']}"
            lines.append(
                f"placed: {placed.get('side')} {placed.get('size')} on {placed.get('venue')}"
                f"{at}, stop {placed.get('stop_price')}, take {placed.get('take_price')}"
            )
            lines.append(f"  timing: {execution_timing(placed)}")
            # Sent is not filled. What became of the order is the line an operator acts on.
            lines.append(f"  outcome: {order_outcome(placed)}")
        elif placed.get("status") == "UNKNOWN":
            lines.append(f"order state UNKNOWN: {placed.get('unknown')}")
        elif placed.get("refused"):
            lines.append(f"not placed: {placed['refused']}")
    return lines


def execution_timing(placed: Mapping[str, Any]) -> str:
    """How old the decision was when sent, how long the venue took, and what the fill cost."""
    said = []
    if placed.get("decision_age_s") is not None:
        said.append(f"sent {placed['decision_age_s']}s after the cycle began")
    if placed.get("send_ms") is not None:
        said.append(f"the venue answered in {placed['send_ms']} ms")
    if placed.get("slippage_bps") is not None:
        said.append(f"filled {placed['slippage_bps']} bps from the mid it was priced at")
    return "; ".join(said) or "not measured"
