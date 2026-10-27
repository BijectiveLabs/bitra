"""A desk on a terminal: runs, sessions, evaluations, traces, the ladder, watch, keys, audits."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from types import TracebackType
from typing import Any

from rich import box
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from trading_research_agents.cli.tui import _bps, _kv, console


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
        self.live = Live(
            Text("connected; waiting for the first closed bar", style="dim"),
            console=console(),
            refresh_per_second=4,
            transient=True,
        )
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
    "macro",
    "onchain",
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
            str(h.get("at"))[:19].replace("T", " "),
            str(h.get("kind")),
            str(h.get("from")),
            str(h.get("to")),
            detail,
        )
    return Group(Text(f"tier {tier}", style="bold"), table)


def watch_view(record: Mapping[str, Any]) -> Group:
    """A desk as its own records describe it. Read-only, and it says so."""
    from trading_research_agents.cli.watch import short_time

    parts: list[Any] = []
    decisions = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="decisions")
    for name, justify in (
        ("when", "left"),
        ("instrument", "left"),
        ("setup", "left"),
        ("conviction", "right"),
        ("risk bps", "right"),
        ("net bps", "right"),
        ("outcome", "left"),
        ("detail", "left"),
    ):
        decisions.add_column(name, justify=justify)  # type: ignore[arg-type]
    styles = {
        "placed": "green",
        "intent": "green",
        "refused": "yellow",
        "held": "dim",
        "not placed": "red",
        "stopped": "red",
    }
    for row in record.get("decisions") or []:
        outcome = str(row.get("outcome") or "")
        decisions.add_row(
            short_time(str(row.get("at") or "")),
            str(row.get("instrument") or ""),
            f"{row.get('target') or ''} {row.get('setup') or ''}".strip(),
            str(row.get("conviction") or ""),
            str(row.get("risk_bps") or ""),
            str(row.get("net_bps") or ""),
            Text(outcome, style=styles.get(outcome, "")),
            str(row.get("detail") or "")[:54],
        )
    parts.append(decisions)

    screen = record.get("screen") or {}
    if screen.get("candidates"):
        chosen = set(screen.get("chosen") or ())
        row = ", ".join(
            f"{c['instrument']}{' *' if c['instrument'] in chosen else ''}"
            for c in screen["candidates"]
        )
        parts.append(
            Text(f"last pass {short_time(str(screen.get('at') or ''))}: {row}", style="dim")
        )

    book = record.get("book") or []
    if book:
        held = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="the book")
        for name in ("instrument", "side", "risk bps", "notional"):
            held.add_column(name)
        for row in book:
            held.add_row(
                str(row.get("instrument") or ""),
                str(row.get("side") or ""),
                str(row.get("risk_bps") or "-"),
                str(row.get("notional") or "-"),
            )
        parts.append(held)

    for line in record.get("alarms") or []:
        parts.append(Text(f"alarm  {line}"[:160], style="yellow"))

    spend = record.get("spend") or {}
    audits = ", ".join(record.get("audits") or []) or "none"
    parts.append(
        Text(
            f"{spend.get('cycles_read', 0)} cycle(s) in view, {spend.get('tokens_in_view', 0)} "
            f"tokens; audits: {audits}",
            style="dim",
        )
    )
    parts.append(
        Text(
            f"{record.get('at', '')}  reading {record.get('root', '')} - this view only reads",
            style="dim",
        )
    )
    return Group(*parts)


class WatchView:
    """The desk redrawn on a timer, from files. It holds nothing a desk could notice."""

    def __init__(self, look: Callable[[], Mapping[str, Any]], every_s: float = 5.0) -> None:
        self.look = look
        self.every_s = max(0.5, every_s)
        self.live: Live | None = None

    def __enter__(self) -> WatchView:
        self.live = Live(watch_view(self.look()), console=console(), refresh_per_second=2)
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

    def draw(self) -> None:
        if self.live is not None:
            self.live.update(watch_view(self.look()))


def keys_table(record: Mapping[str, Any]) -> Table:
    """Every credential a run would reach. The values are never here; the fingerprints are."""
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="credentials")
    table.add_column("state", width=5)
    table.add_column("what")
    table.add_column("held")
    table.add_column("verdict")
    for row in record.get("credentials") or []:
        ready = bool(row.get("ready"))
        held = ", ".join(
            f"{name} {fp}"
            for name, fp in zip(
                row.get("present") or [], row.get("fingerprints") or [], strict=False
            )
        )
        table.add_row(
            Text("ok" if ready else "todo", style="green" if ready else "yellow"),
            str(row.get("what")),
            held,
            str(row.get("verdict")),
        )
    table.caption = "fingerprints are one-way; no key is printed, logged or returned"
    return table


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
