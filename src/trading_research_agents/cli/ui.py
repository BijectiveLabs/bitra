"""The console face of `tra`: the same commands, rendered for a person at a terminal.

Every command still speaks plain text and `--json`; this module only decides how the same
figures look when a person is watching. It is off when stdout is not a terminal, when
`--plain` or `TRA_PLAIN=1` is given, or when `--json` is asked for, so scripts, CI and agent
hosts never see a table where they expected a line. Nothing here reaches back into a run:
the live views read reports and decide nothing.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from importlib.resources import files
from types import TracebackType
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

BANNER = r"""
 _____  ____      _
|_   _||  _ \    / \
  | |  | |_) |  / _ \
  | |  |  _ <  / ___ \
  |_|  |_| \_\/_/   \_\
""".strip("\n")

TAGLINE = "trading-research-agents: a research agent that refuses to claim what it has not measured"
_console: Console | None = None


def banner(width: int) -> str:
    """The full wordmark when the terminal is wide enough for it, the short one otherwise."""
    try:
        text = (
            files("trading_research_agents.cli").joinpath("static/welcome.txt").read_text("utf-8")
        )
    except (OSError, TypeError):
        return BANNER
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    if not lines or max(len(line) for line in lines) + 6 > width:
        return BANNER
    return "\n".join(lines)


def console() -> Console:
    global _console
    if _console is None:
        _console = Console(highlight=False)
    return _console


def enabled(plain: bool = False, as_json: bool = False) -> bool:
    """Whether a person is watching: a terminal, no --plain, no TRA_PLAIN, no --json."""
    if plain or as_json or os.environ.get("TRA_PLAIN"):
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _fmt(value: Any) -> Any:
    """Plain values for a two-column panel: mappings and lists read as words, not as reprs."""
    if value is None:
        return ""
    if isinstance(value, Text):
        return value
    if isinstance(value, Mapping):
        return "; ".join(f"{k}: {_fmt(v)}" for k, v in value.items())
    if isinstance(value, list | tuple):
        return ", ".join(str(_fmt(v)) for v in value)
    return str(value)


def _kv(rows: Sequence[tuple[str, Any]]) -> Table:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="dim")
    table.add_column()
    for key, value in rows:
        table.add_row(str(key), _fmt(value))
    return table


def _bps(value: Any) -> Text:
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return Text(str(value))
    style = "green" if number > 0 else "red" if number < 0 else ""
    return Text(f"{number:+.2f} bps", style=style)


def cockpit(version: str, good: Sequence[str], bad: Sequence[str], next_command: str) -> None:
    """`tra` with nothing else: who this is, what is ready, what to type next."""
    c = console()
    head = Text(banner(c.width), style="bold cyan")
    head.append(f"\n{TAGLINE}\n", style="dim")
    head.append(f"tra {version}  by Bijective", style="dim")
    c.print(Panel(head, box=box.ROUNDED, padding=(0, 2)))
    c.print(check_table(good, bad))
    c.print(Text("next: ", style="dim") + Text(next_command, style="bold"))


def check_table(good: Sequence[str], bad: Sequence[str]) -> Table:
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    table.add_column("state", width=5)
    table.add_column("what")
    for g in good:
        table.add_row(Text("ok", style="green"), g)
    for b in bad:
        table.add_row(Text("todo", style="yellow"), b)
    return table


def run_panel(report: Any, out: str = "") -> Panel:
    rows: list[tuple[str, Any]] = [
        ("label", f"{report.label} {report.instrument}"),
        ("prints", report.prints),
        ("bars", report.bars),
        ("intents", f"{report.intents} ({report.refused} refused)"),
        ("fills", report.fills),
        ("round trips", report.round_trips),
        ("net", _bps(report.net_bps_total)),
    ]
    if report.open_position:
        p = report.open_position
        rows.append(
            (
                "open position",
                f"{p.get('direction')} {p.get('size')} at {p.get('entry_px')}, "
                f"{p.get('bars_held')} bars held, last {p.get('last_price')}",
            )
        )
    if report.stopped_by:
        rows.append(("stopped by", Text(str(report.stopped_by), style="yellow")))
    for refusal in report.refusals[:5]:
        rows.append(("refused", refusal))
    if out:
        rows.append(("files", f"trades.jsonl, intents.jsonl, fills.jsonl, run.json in {out}"))
    title = "run" if report.finished_at is None else "run finished"
    return Panel(_kv(rows), title=title, box=box.ROUNDED, expand=False)


class RunView:
    """A live panel over a run: the observer the runtime tells after every closed bar."""

    def __init__(self, out: str) -> None:
        self.out = out
        self.live: Live | None = None
        self.bars = 0

    def __enter__(self) -> RunView:
        # Transient: the final panel is printed by the command once the run is over.
        self.live = Live(Text("starting"), console=console(), refresh_per_second=4, transient=True)
        self.live.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.live is not None:
            self.live.__exit__(exc_type, exc, tb)

    def observe(self, report: Any, bar: Any) -> None:
        # Redraw at most once per bar; the Live object rate-limits the terminal itself.
        self.bars = report.bars
        if self.live is not None:
            self.live.update(run_panel(report))


STEPS = (
    "figures",
    "market_screener",
    "macro_analyst",
    "onchain_analyst",
    "risk_manager",
    "executor",
    "auditor",
)


class SessionView:
    """The pipeline of roles as a live table: pending, running, done, skipped, refused."""

    def __init__(self, usage: Any) -> None:
        self.usage = usage
        self.state: dict[str, tuple[str, str]] = {s: ("pending", "") for s in STEPS}
        self.order: list[str] = list(STEPS)
        self.live: Live | None = None

    def __enter__(self) -> SessionView:
        self.live = Live(self.table(), console=console(), refresh_per_second=4)
        self.live.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.live is not None:
            self.live.__exit__(exc_type, exc, tb)

    def on_step(self, step: str, status: str, summary: str) -> None:
        if step not in self.state:
            # Runs appear after the screener, one per kept instrument, in the order they start.
            at = self.order.index("risk_manager")
            self.order.insert(at, step)
        self.state[step] = (status, summary)
        if self.live is not None:
            self.live.update(self.table())

    def table(self) -> Table:
        table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="session")
        table.add_column("step")
        table.add_column("state", width=8)
        table.add_column("summary")
        styles = {"running": "cyan", "done": "green", "refused": "red", "skipped": "dim"}
        for step in self.order:
            status, summary = self.state.get(step, ("pending", ""))
            table.add_row(step, Text(status, style=styles.get(status, "")), summary)
        table.caption = f"{self.usage.tokens} tokens, cost {self.usage.cost}"
        return table


def session_report(result: Any, usage: Any, out: str) -> Group:
    kept = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="instruments")
    kept.add_column("instrument")
    kept.add_column("screener")
    kept.add_column("regime")
    kept.add_column("round trips", justify="right")
    kept.add_column("net", justify="right")
    kept.add_column("reason")
    for k in result.kept:
        name = str(k.get("instrument"))
        r = result.reports.get(name)
        kept.add_row(
            name,
            Text("kept", style="green"),
            str(k.get("regime", "")),
            "" if r is None else str(r.round_trips),
            Text("") if r is None else _bps(r.net_bps_total),
            str(k.get("reason", "")),
        )
    for e in result.excluded:
        kept.add_row(
            str(e.get("instrument")),
            Text("excluded", style="yellow"),
            "",
            "",
            "",
            str(e.get("reason", "")),
        )
    rows: list[tuple[str, Any]] = [("session", result.session_id)]
    if result.risk is not None:
        rows.append(("risk manager", f"{result.risk['decision']}: {result.risk['reason']}"))
    if result.allocation:
        parts = ", ".join(f"{a.get('instrument')} {a.get('fraction')}" for a in result.allocation)
        rows.append(("executor", parts))
    if result.audit is not None:
        verdict = str(result.audit.get("verdict"))
        rows.append(
            ("auditor", Text(f"{verdict}: {result.audit.get('summary', '')}", style="bold"))
        )
    rows.append(("tokens", f"{usage.tokens} (cost {usage.cost})"))
    for refusal in result.refusals[:5]:
        rows.append(("refused", refusal))
    if result.stopped_by:
        rows.append(("stopped by", Text(str(result.stopped_by), style="yellow")))
    rows.append(("files", f"session.json, audit.json, usage.jsonl in {out}"))
    return Group(kept, Panel(_kv(rows), title="session finished", box=box.ROUNDED, expand=False))


def eval_table(rows: Sequence[Any], instrument: str, dataset: str) -> Table:
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    table.title = f"eval {instrument} over {dataset}: same bars, same controls"
    for name, justify in (
        ("model", "left"),
        ("net", "right"),
        ("rt", "right"),
        ("calls", "right"),
        ("fail", "right"),
        ("tokens", "right"),
        ("med ms", "right"),
        ("p90 ms", "right"),
        ("tok/bps", "right"),
        ("cost", "right"),
        ("stopped by", "left"),
    ):
        table.add_column(name, justify=justify)  # type: ignore[arg-type]
    for r in rows:
        table.add_row(
            f"{r.provider}:{r.model}",
            _bps(r.net_bps_total),
            str(r.round_trips),
            str(r.calls),
            Text(str(r.failed_calls), style="red" if r.failed_calls else ""),
            str(r.input_tokens + r.output_tokens),
            str(r.latency_median_ms),
            str(r.latency_p90_ms),
            "" if r.tokens_per_bps is None else str(r.tokens_per_bps),
            "" if r.cost is None else str(r.cost),
            "" if not r.stopped_by else str(r.stopped_by),
        )
    return table


def trace_view(t: Any) -> Group:
    parts: list[Any] = [
        Panel(
            _kv([(k, v) for k, v in t.header.items() if v not in (None, [], {})]),
            title=f"{t.kind} {t.path}",
            box=box.ROUNDED,
            expand=False,
        )
    ]
    if t.roles:
        roles = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="roles")
        for name in ("when", "kind", "who", "detail"):
            roles.add_column(name)
        for e in t.roles:
            roles.add_row(e.at, e.kind, e.who, e.detail)
        parts.append(roles)
    if t.calls:
        calls = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
        calls.title = "model calls (wall clock, UTC)"
        for name in ("at", "role", "detail"):
            calls.add_column(name)
        for e in t.calls:
            style = "red" if e.detail.startswith("error") else ""
            calls.add_row(e.at, e.who, Text(e.detail, style=style))
        parts.append(calls)
    if t.market:
        market = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
        market.title = "market (bar time, UTC)"
        for name in ("at", "kind", "instrument", "detail"):
            market.add_column(name)
        styles = {"fill": "cyan", "round_trip": "bold", "refusal": "yellow"}
        for e in t.market:
            market.add_row(e.at, Text(e.kind, style=styles.get(e.kind, "")), e.who, e.detail)
        parts.append(market)
    return Group(*parts)


def ledger_table(rows: Sequence[Mapping[str, Any]], counts: Mapping[str, int]) -> Table:
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="kill ledger")
    for name in ("id", "verdict", "layer", "axis", "subject", "note"):
        table.add_column(name)
    styles = {"FALSIFIED": "red", "CLOSED": "red", "NOT PROVEN": "yellow", "OPEN": "green"}
    for r in rows:
        verdict = str(r.get("verdict", ""))
        axis = ", ".join(r.get("axis_of_death", []) or []) or str(r.get("blocking_axis") or "")
        table.add_row(
            str(r.get("entry_id", "")),
            Text(verdict, style=styles.get(verdict, "")),
            "" if r.get("layer") is None else str(r.get("layer")),
            axis,
            str(r.get("subject", "")),
            str(r.get("note", "")),
        )
    table.caption = "  ".join(f"{v} {n}" for v, n in counts.items() if n) or "empty"
    return table


def ladder_table(tier: str, history: Sequence[Mapping[str, Any]]) -> Group:
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="ladder")
    for name in ("at", "kind", "from", "to", "detail"):
        table.add_column(name)
    for h in history:
        detail = str(h.get("evidence") or h.get("reason") or "")
        if h.get("signed_by"):
            detail += f" (signed by {h['signed_by']})"
        table.add_row(
            str(h.get("at")), str(h.get("kind")), str(h.get("from")), str(h.get("to")), detail
        )
    return Group(Text(f"tier {tier}", style="bold"), table)


def cycle_view(record: Mapping[str, Any]) -> Group:
    """One cycle: what the engine saw, what the analyst said, what the cost gate decided."""
    parts: list[Any] = []
    data = record.get("data") or {}
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
                f"{analyst.get('target')} at {analyst.get('confidence')} over "
                f"{analyst.get('horizon_bars')} bars: {analyst.get('reason')}",
            )
        )
    intent = record.get("intent")
    if intent:
        head.append(("intent", Text(f"{intent['target']} {intent['size']}", style="bold")))
    for refusal in (record.get("refusals") or [])[:6]:
        head.append(("refused", Text(str(refusal), style="yellow")))
    if record.get("stopped_by"):
        head.append(("stopped by", Text(str(record["stopped_by"]), style="yellow")))
    parts.append(Panel(_kv(head), title="cycle", box=box.ROUNDED, expand=False))

    if data.get("values"):
        table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="data used")
        for name in ("kind", "source", "tier", "age s"):
            table.add_column(name)
        for kind, value in sorted(data["values"].items()):
            table.add_row(
                kind, str(value.get("source")), str(value.get("tier")), str(value.get("age_s"))
            )
        parts.append(table)

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

    stake = record.get("conviction") or {}
    if stake:
        score = stake.get("score") or {}
        voices: list[tuple[str, Any]] = [("score", f"{score.get('score')} of 1.0")]
        for role, value in (score.get("contributions") or {}).items():
            voices.append((role.replace("_analyst", ""), value))
        voices.append(("size", f"{stake.get('size')} at {stake.get('multiple')}x base"))
        if stake.get("leverage") is not None:
            voices.append(("leverage", f"{stake.get('leverage')}x equity"))
        if stake.get("capped_by"):
            voices.append(("capped by", stake.get("capped_by")))
        style = "green" if stake.get("admitted") else "yellow"
        parts.append(
            Panel(_kv(voices), title=Text("conviction", style=style), box=box.ROUNDED, expand=False)
        )

    met = record.get("targets") or {}
    if met:
        style = "green" if met.get("admitted") else "yellow"
        parts.append(
            Panel(
                _kv(
                    [
                        ("net bps", met.get("net_bps")),
                        ("reward to risk", met.get("reward_risk")),
                        ("round trips today", met.get("round_trips_today")),
                    ]
                ),
                title=Text("targets", style=style),
                box=box.ROUNDED,
                expand=False,
            )
        )

    agents = record.get("agents") or {}
    if agents.get("agents"):
        who = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="agents")
        for name in ("agent", "state", "seconds"):
            who.add_column(name)
        rounds = [*agents["agents"], *((record.get("validators") or {}).get("agents") or [])]
        for row in rounds:
            state = "reused" if row.get("reused") else ("ok" if row.get("ok") else "failed")
            style = {"ok": "green", "failed": "red", "reused": "dim"}[state]
            who.add_row(str(row.get("agent")), Text(state, style=style), str(row.get("seconds")))
        parts.append(who)
    return Group(*parts)


def audit_view(record: Mapping[str, Any]) -> Group:
    """What the auditors found, and what they propose. Nothing here has been applied."""
    parts: list[Any] = [
        Panel(
            _kv(
                [
                    ("at", record.get("at")),
                    ("cycles read", record.get("cycles")),
                    ("tokens", record.get("tokens")),
                ]
            ),
            title="audit",
            box=box.ROUNDED,
            expand=False,
        )
    ]
    for role, report in sorted((record.get("reports") or {}).items()):
        table = Table(
            box=box.SIMPLE_HEAD,
            show_edge=False,
            pad_edge=False,
            title=role.replace("_", " "),
            caption=str(report.get("summary") or ""),
        )
        for name in ("finding", "confidence", "proposal", "change"):
            table.add_column(name, overflow="fold")
        for finding in report.get("findings") or ():
            change = finding.get("change") or {}
            table.add_row(
                str(finding.get("what")),
                str(finding.get("confidence")),
                str(finding.get("proposal")),
                (
                    ""
                    if not change
                    else f"{change.get('key')}: {change.get('from')} -> {change.get('to')}"
                ),
            )
        if not (report.get("findings") or ()):
            table.add_row(Text("nothing to report", style="dim"), "", "", "")
        parts.append(table)
        for reason in report.get("discarded") or ():
            parts.append(Text(f"discarded: {reason}", style="yellow"))
    for role, error in sorted((record.get("failed") or {}).items()):
        parts.append(Text(f"{role}: {error}", style="red"))
    parts.append(Text("nothing here has been applied", style="dim"))
    return Group(*parts)


def snapshot_view(record: Mapping[str, Any]) -> Group:
    """One snapshot: what answered, how old it was, and what the chain tried on the way."""
    values = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="snapshot")
    for name, justify in (
        ("kind", "left"),
        ("source", "left"),
        ("tier", "left"),
        ("age s", "right"),
        ("detail", "left"),
    ):
        values.add_column(name, justify=justify)  # type: ignore[arg-type]
    skip = {"kind", "instrument", "source", "tier", "age_s", "as_of"}
    for kind, value in sorted(record.get("values", {}).items()):
        age = value.get("age_s")
        detail = ", ".join(f"{k} {v}" for k, v in value.items() if k not in skip)
        values.add_row(kind, str(value.get("source")), str(value.get("tier")), str(age), detail)
    rows: list[tuple[str, Any]] = [
        ("cycle", record.get("cycle_id")),
        ("instrument", record.get("instrument")),
    ]
    for attempt in record.get("attempts", []):
        if attempt.get("outcome") != "ok":
            rows.append(
                (
                    attempt.get("outcome"),
                    f"{attempt.get('source')} for {attempt.get('kind')}: {attempt.get('detail')}",
                )
            )
    for kind, reason in (record.get("refused") or {}).items():
        rows.append(("refused", Text(f"{kind}: {reason}", style="yellow")))
    if record.get("stale"):
        rows.append(("stale", Text(", ".join(record["stale"]), style="yellow")))
    return Group(Panel(_kv(rows), title="engine", box=box.ROUNDED, expand=False), values)


def observe_view(record: Mapping[str, Any]) -> Group:
    recurrence = str(record["recurrence"])
    style = "green" if recurrence == "recurs" else "yellow"
    obs = record["observation"]
    rows: list[tuple[str, Any]] = [
        ("subject", obs["subject"]),
        ("recurrence", Text(recurrence.upper(), style=style)),
        (
            "events",
            f"{record['events']} over {record['bars']} bars "
            f"({record['measured']} measured, {record['skipped']} skipped)",
        ),
        ("per subperiod", ", ".join(str(n) for n in record["events_per_subperiod"])),
    ]
    r = record["response_bps"]
    if r.get("n"):
        rows.append(
            (
                f"response at {obs['horizon_bars']} bars",
                f"median {r['median']} bps, mean {r['mean']} bps, q25 {r['q25']}, q75 {r['q75']}, "
                f"positive {r['fraction_positive']}, median |response| {r['median_abs']} bps",
            )
        )
    for fired in record["fired"]:
        rows.append(("fired", Text(str(fired), style="yellow")))
    if record.get("magnitude"):
        rows.append(("magnitude", record["magnitude"]))
    if record.get("recorded"):
        rows.append(("recorded", record["recorded"]))
    curve = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="response curve")
    for name in ("bar", "n", "median bps", "mean bps"):
        curve.add_column(name, justify="right")
    for c in record["curve_bps"]:
        curve.add_row(str(c["bar"]), str(c["n"]), str(c["median"]), str(c["mean"]))
    return Group(Panel(_kv(rows), title="layer 1", box=box.ROUNDED, expand=False), curve)


def show(renderable: Any) -> None:
    console().print(renderable)


Renderer = Callable[[], Any]
