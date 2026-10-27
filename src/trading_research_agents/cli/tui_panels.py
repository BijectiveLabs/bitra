"""The panels of one cycle on a terminal: the head, data, series, cost, validators, confluence,
conviction, risk, targets and agents.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from trading_research_agents.cli.tui import _bps, _kv


def _cycle_panel(record: Mapping[str, Any]) -> list[Any]:
    parts: list[Any] = []
    head: list[tuple[str, Any]] = [
        ("cycle", record.get("cycle_id")),
        ("instrument", record.get("instrument")),
        ("mode", record.get("mode")),
    ]
    analyst = record.get("analyst") or {}
    if analyst:
        head.append(
            (
                "technical",
                f"{analyst.get('target')} on {analyst.get('setup')} at "
                f"{analyst.get('confidence')} over {analyst.get('horizon_bars')} bars: "
                f"{analyst.get('reason')}",
            )
        )
    cited = record.get("cited") or []
    if cited:
        head.append(
            ("read", ", ".join(f"{f['figure']}={f['value']}" for f in cited)),
        )
    wake = record.get("trigger") or {}
    if wake and not wake.get("ask"):
        head.append(("held", Text("; ".join(wake.get("reasons") or []), style="yellow")))
    intent = record.get("intent")
    if intent:
        head.append(("intent", Text(f"{intent['target']} {intent['size']}", style="bold")))
    placed = record.get("placed")
    if isinstance(placed, dict):
        if placed.get("sent"):
            filled = placed.get("filled")
            head.append(
                (
                    "venue",
                    Text(
                        f"sent, order {placed.get('order_id') or '?'}"
                        + (f", filled {filled}" if filled not in (None, "") else "")
                        + (
                            f", {placed['decision_age_s']}s after the cycle began"
                            if placed.get("decision_age_s") is not None
                            else ""
                        )
                        + (
                            f", slippage {placed['slippage_bps']} bps"
                            if placed.get("slippage_bps") is not None
                            else ""
                        ),
                        style="green",
                    ),
                )
            )
        else:
            why = placed.get("refused") or placed.get("error") or "not sent"
            head.append(("venue", Text(f"NOT SENT: {why}", style="bold red")))
    for refusal in (record.get("refusals") or [])[:6]:
        head.append(("refused", Text(str(refusal), style="yellow")))
    if record.get("stopped_by"):
        head.append(("stopped by", Text(str(record["stopped_by"]), style="yellow")))
    skipped = _not_reached(record)
    if skipped:
        # A panel that is missing reads as a panel that broke; say the cycle ended before it.
        head.append(("not reached", Text(skipped, style="dim")))
    parts.append(Panel(_kv(head), title="cycle", box=box.ROUNDED, expand=False))
    return parts


def _data_table(record: Mapping[str, Any]) -> list[Any]:
    parts: list[Any] = []
    data = record.get("data") or {}
    if data.get("values"):
        table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="data used")
        for name in ("kind", "source", "tier", "age s"):
            table.add_column(name)
        for kind, value in sorted(data["values"].items()):
            table.add_row(
                kind, str(value.get("source")), str(value.get("tier")), str(value.get("age_s"))
            )
        parts.append(table)
    return parts


def _series_table(record: Mapping[str, Any]) -> list[Any]:
    parts: list[Any] = []
    if record.get("series"):
        read = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="series read")
        for name in ("reading", "series", "value", "as of", "age h", "state", "from"):
            read.add_column(name)
        for row in record["series"]:
            state = str(row.get("state"))
            read.add_row(
                # A record written before 0.11 named the analyst role the reading replaced.
                str(row.get("reading") or str(row.get("role", "")).removesuffix("_analyst")),
                str(row.get("series")),
                str(row.get("value")),
                str(row.get("observed_at") or "")[:16],
                str(row.get("age_h")),
                Text(state, style="yellow" if state in ("stale", "unknown") else ""),
                f"{row.get('origin')}; {row.get('source') or 'no source named'}",
            )
        parts.append(read)
    return parts


def _cost_table(record: Mapping[str, Any]) -> list[Any]:
    parts: list[Any] = []
    cost = record.get("cost") or {}
    if cost:
        costs = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
        costs.title = f"cost before the order: {cost['cost']['round_trip_bps']} bps round trip"
        for name, justify in (
            ("component", "left"),
            ("bps", "right"),
            ("certainty", "left"),
            ("from", "left"),
        ):
            costs.add_column(name, justify=justify)  # type: ignore[arg-type]
        styles = {"declared": "green", "measured": "cyan", "computed": "", "estimated": "yellow"}
        for component in cost["cost"]["components"]:
            costs.add_row(
                component["name"],
                component["bps"],
                Text(component["certainty"], style=styles.get(component["certainty"], "")),
                component["source"],
            )
        costs.caption = f"{cost['reason']}; edge is {cost['edge_certainty']}: {cost['edge_source']}"
        parts.append(costs)
    return parts


def _validator_table(record: Mapping[str, Any]) -> list[Any]:
    parts: list[Any] = []
    votes = {
        role: said
        for role, said in (record.get("context") or {}).items()
        if role.startswith("validator")
    }
    if votes:
        panel = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="validators")
        for name in ("stance", "verdict", "cited", "discarded", "reason"):
            panel.add_column(name)
        colours = {"support": "green", "oppose": "red", "abstain": "yellow"}
        for role, said in sorted(votes.items()):
            verdict = str(said.get("verdict") or "failed")
            panel.add_row(
                str(said.get("stance") or role.partition("_")[2]),
                Text(verdict, style=colours.get(verdict, "yellow")),
                str(len(said.get("claims") or ())),
                str(len(said.get("discarded") or ())),
                str(said.get("reason") or said.get("error") or ""),
            )
        parts.append(panel)
    return parts


def _confluence_panel(record: Mapping[str, Any]) -> list[Any]:
    parts: list[Any] = []
    agreement = record.get("confluence") or {}
    if agreement:
        rows = [(role, state) for role, state in sorted((agreement.get("states") or {}).items())]
        rows.append(("support", agreement.get("support")))
        rows.append(("opposition", agreement.get("opposition")))
        style = "green" if agreement.get("admitted") else "yellow"
        parts.append(
            Panel(
                _kv(rows),
                title=Text("confluence", style=style),
                box=box.ROUNDED,
                expand=False,
            )
        )
    return parts


def _conviction_panel(record: Mapping[str, Any]) -> list[Any]:
    parts: list[Any] = []
    stake = record.get("conviction") or {}
    if stake:
        score = stake.get("score") or {}
        voices: list[tuple[str, Any]] = [("score", f"{score.get('score')} of 1.0")]
        most = score.get("weights") or {}
        for role, value in (score.get("contributions") or {}).items():
            of = f" of {most[role]}" if role in most else ""
            pulled = Decimal(str(value))
            way = "for the trade" if pulled > 0 else "against it" if pulled < 0 else "silent"
            voices.append((role.replace("_analyst", ""), f"{pulled:+} {of.strip()}, {way}"))
        voices.append(("size", f"{stake.get('size')} at {stake.get('multiple')}x base"))
        if stake.get("leverage") is not None:
            voices.append(("leverage", f"{stake.get('leverage')}x equity"))
        if stake.get("capped_by"):
            voices.append(("capped by", stake.get("capped_by")))
        style = "green" if stake.get("admitted") else "yellow"
        parts.append(
            Panel(_kv(voices), title=Text("conviction", style=style), box=box.ROUNDED, expand=False)
        )
    return parts


def _risk_panel(record: Mapping[str, Any]) -> list[Any]:
    parts: list[Any] = []
    risk = record.get("risk") or {}
    if risk and risk.get("risk_bps") is not None:
        style = "green" if risk.get("admitted") else "yellow"
        sized: list[tuple[str, Any]] = [
            ("at the stop", f"{risk.get('risk_bps')} bps of equity"),
            ("allowed", f"{risk.get('allowed_bps')} bps"),
            ("size", risk.get("size")),
        ]
        for reason in risk.get("reasons") or []:
            sized.append(("cut", Text(str(reason), style="yellow")))
        parts.append(
            Panel(
                _kv(sized), title=Text("risk per trade", style=style), box=box.ROUNDED, expand=False
            )
        )
    return parts


def _targets_panel(record: Mapping[str, Any]) -> list[Any]:
    parts: list[Any] = []
    met = record.get("targets") or {}
    exits = record.get("exits") or {}
    if met or exits:
        style = "green" if met.get("admitted", True) else "yellow"
        shown: list[tuple[str, Any]] = _exit_rows(exits)
        cost = record.get("cost") or {}
        if met:
            rule = record.get("targets_rule") or {}
            shown.extend(
                [
                    ("reward : risk", _ratio(met.get("reward_risk"))),
                    ("expected edge", _edge(cost)),
                    ("round trip", _trip(cost)),
                    ("after cost", _after(met, rule)),
                    ("model spend", _spend(met.get("research_bps"), record.get("spend") or {})),
                    ("trades today", f"{met.get('round_trips_today')} before this one"),
                ]
            )
        parts.append(
            Panel(
                _kv(shown),
                title=Text("targets" if met else "exits", style=style),
                box=box.ROUNDED,
                expand=False,
            )
        )
    return parts


def _agents_table(record: Mapping[str, Any]) -> list[Any]:
    parts: list[Any] = []
    agents = record.get("agents") or {}
    if agents.get("agents"):
        who = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="agents")
        for name in ("agent", "state", "seconds"):
            who.add_column(name)
        rounds = [*agents["agents"], *((record.get("validators") or {}).get("agents") or [])]
        said = record.get("context") or {}
        for row in rounds:
            state = "reused" if row.get("reused") else ("ok" if row.get("ok") else "failed")
            answer = said.get(str(row.get("agent")))
            if state == "ok" and isinstance(answer, Mapping) and answer.get("error"):
                # The call came back, and what came back could not be read as an answer.
                state = "unreadable"
            style = {"ok": "green", "failed": "red", "reused": "dim", "unreadable": "red"}[state]
            who.add_row(str(row.get("agent")), Text(state, style=style), str(row.get("seconds")))
        for name, why in sorted((record.get("not_read") or record.get("not_asked") or {}).items()):
            who.add_row(name, Text(f"not read: {why}", style="yellow"), "")
        parts.append(who)
    return parts


# The steps after the analyst, in the order a cycle runs them, by the record key each one fills.
LATER_STEPS = (
    ("exits", "stop and target"),
    ("conviction", "conviction"),
    ("risk", "risk per trade"),
    ("desk", "desk"),
    ("cost", "cost gate"),
    ("targets", "targets"),
    ("intent", "intent"),
)


def _not_reached(record: Mapping[str, Any]) -> str:
    """The steps a cycle that ended early never got to, and where it ended."""
    if record.get("intent") or record.get("held") or not record.get("analyst"):
        return ""
    present = [i for i, (key, _) in enumerate(LATER_STEPS) if record.get(key)]
    last = present[-1] if present else -1
    missing = [label for key, label in LATER_STEPS[last + 1 :]]
    if not missing:
        return ""
    ended = str(record.get("stopped_by") or (record.get("refusals") or ["a refusal"])[-1])
    return f"{', '.join(missing)}; the cycle ended at {ended.partition(':')[0]}"


def _exit_rows(exits: dict[str, Any]) -> list[tuple[str, Any]]:
    """The position, then each exit as a price, how far price moves to reach it, and what that
    does to the position. A short's take profit is a fall in price and a gain to the position."""
    if not exits:
        return []
    rows: list[tuple[str, Any]] = [
        ("position", f"{exits.get('side')} at about {exits.get('entry')}")
    ]
    for label, name in (("take profit", "take"), ("stop loss", "stop")):
        if exits.get(f"{name}_price") is None:
            continue
        move = Decimal(str(exits[f"{name}_bps"])) / 100
        pnl = exits.get(f"{name}_pnl_bps", exits[f"{name}_bps"])
        rows.append(
            (
                label,
                Text.assemble(
                    f"{exits[f'{name}_price']}  price {move:+.2f}%, the position ", _bps(pnl)
                ),
            )
        )
    return rows


def _ratio(value: Any) -> Any:
    if value in (None, ""):
        return "-"
    return f"{Decimal(str(value)).normalize():f} : 1"


def _edge(cost: dict[str, Any]) -> Any:
    """What the desk expects to earn per trade on average, and whether anyone measured it."""
    if cost.get("edge_bps") is None:
        return "-"
    edge = Decimal(str(cost["edge_bps"]))
    return f"{edge:.2f} bps a trade on average; {cost.get('edge_source')}"


def _trip(cost: dict[str, Any]) -> Any:
    trip = (cost.get("cost") or {}).get("round_trip_bps")
    return "-" if trip is None else f"{Decimal(str(trip)):.2f} bps, paid win or lose"


def _after(met: dict[str, Any], rule: dict[str, Any]) -> Any:
    """The expected edge less the cost, and how many times the cost the edge is."""
    if met.get("net_bps") is None:
        return "-"
    said = Text.assemble(_bps(met["net_bps"]))
    if met.get("edge_multiple") not in (None, ""):
        asks = rule.get("min_edge_multiple")
        tail = f"; the desk asks {asks}x" if asks not in (None, "") else ""
        said.append(f"  (the edge is {met['edge_multiple']}x the cost{tail})")
    return said


def _spend(research: Any, spend: dict[str, Any]) -> Any:
    """Model calls as a cost of this trade; calls with no declared price are named, not zeroed."""
    shown = "-" if research in (None, "") else f"{research} bps of this notional"
    unpriced = spend.get("unpriced") or []
    if unpriced:
        return Text(f"{shown}; not priced: {', '.join(unpriced)}", style="yellow")
    return shown


def _multiple(value: Any) -> Any:
    return "-" if value in (None, "") else f"{value}x the round trip"
