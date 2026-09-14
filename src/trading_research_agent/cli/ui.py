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

TAGLINE = "trading-research-agent: a research agent that refuses to claim what it has not measured"
_console: Console | None = None


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
    head = Text(BANNER, style="bold cyan")
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
