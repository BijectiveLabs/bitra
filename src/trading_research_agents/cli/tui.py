"""The console face of `tra`: the same commands, rendered for a person at a terminal.

Every command still speaks plain text and `--json`; this module only decides how the same
figures look when a person is watching. It is off when stdout is not a terminal, when
`--plain` or `TRA_PLAIN=1` is given, or when `--json` is asked for, so scripts, CI and agent
hosts never see a table where they expected a line. Nothing here reaches back into a run:
the live views read reports and decide nothing.
"""

from __future__ import annotations

import io
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from importlib.resources import files
from threading import Lock
from time import monotonic, sleep
from types import TracebackType
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

BANNER = r"""
 _____  ____      _
|_   _||  _ \    / \
  | |  | |_) |  / _ \
  | |  |  _ <  / ___ \
  |_|  |_| \_\/_/   \_\
""".strip("\n")

TAGLINE = "trading-research-agents: autonomous trading agents that cannot lie about their results"
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


def console(safe: bool = False) -> Console:
    global _console
    if safe:
        return _safe_console()
    if _console is None:
        # No markup: config names such as [run].size_step appear in refusals and would be read as
        # style tags and silently dropped from the line.
        _console = Console(highlight=False, markup=False)
    return _console


# Cursor home, erase the screen, then erase the scrollback: what `clear` sends. The order matters on
# the classic Windows console, where erasing the screen pushes it into the scrollback; erasing the
# scrollback first left the shell's lines above the cockpit (seen 2026-10-06).
TAKE_SCREEN = "\x1b[H\x1b[2J\x1b[3J"
VT_PROCESSING = 0x0004
STD_OUTPUT = -11


def take_screen(stream: Any = None) -> bool:
    """Give the whole terminal to the console, the way a full-screen program starts.

    Returns whether it did; a pipe or a file is left alone.
    """
    out = stream or sys.stdout
    if not getattr(out, "isatty", lambda: False)():
        return False
    if os.name == "nt":
        _virtual_terminal()
    out.write(TAKE_SCREEN)
    out.flush()
    return True


def _virtual_terminal() -> None:
    """Escape sequences on the classic Windows console are text until this mode is on."""
    import ctypes

    kernel = getattr(ctypes, "windll").kernel32  # noqa: B009 - absent off Windows
    handle = kernel.GetStdHandle(STD_OUTPUT)
    mode = ctypes.c_ulong()
    if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel.SetConsoleMode(handle, mode.value | VT_PROCESSING)


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


class CycleView:
    """The steps of a cycle, or of a pass over the universe, as a live table.

    The whole pipeline the operator configured is laid out before anything starts, every step
    pending, so a terminal is never blank while models think. A running step shows a spinner and
    its own clock ticking; a finished one shows how it ended and how long it took. The analysts run
    on threads, so updates arrive from several at once: state changes and redraws share one lock.
    """

    def __init__(
        self,
        usage: Any,
        steps: Sequence[str],
        title: str = "cycle",
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.usage = usage
        self.title = title
        self.clock = clock
        self.order: list[str] = list(steps)
        self.state: dict[str, tuple[str, str]] = {s: ("pending", "") for s in self.order}
        self.began = clock()
        self.since: dict[str, float] = {}
        self.spinners: dict[str, Spinner] = {}
        self.live: Live | None = None
        self._lock = Lock()

    def __enter__(self) -> CycleView:
        # Redrawn from `table` on every refresh, so a running step's clock moves on its own.
        # Transient: the command prints the cycle's own panels once it is over.
        self.live = Live(
            console=console(),
            refresh_per_second=8,
            transient=True,
            get_renderable=self.frame,
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

    def on_step(self, step: Any) -> None:
        detail = step.detail
        if step.seconds is not None:
            detail = f"{detail} ({step.seconds:.1f}s)" if detail else f"{step.seconds:.1f}s"
        with self._lock:
            if step.name not in self.state:
                self.order.append(step.name)
            self.state[step.name] = (step.state, detail)
            if step.state == "running":
                self.since[step.name] = self.clock()
                self.spinners[step.name] = Spinner("line", style="cyan")

    def table(self) -> Table:
        with self._lock:
            rows = [(name, *self.state.get(name, ("pending", ""))) for name in self.order]
            since = dict(self.since)
            spinners = dict(self.spinners)
        now = self.clock()
        table = Table(
            box=box.SIMPLE_HEAD,
            show_edge=False,
            pad_edge=False,
            title=self.title,
        )
        table.add_column("step")
        table.add_column("state", width=10)
        table.add_column("detail")
        styles = {
            "done": "green",
            "refused": "yellow",
            "failed": "red",
            "held": "yellow",
            "skipped": "dim",
        }
        for name, state, detail in rows:
            label = _step_label(name)
            if state == "running":
                ticking = f"{now - since.get(name, now):.0f}s"
                spinner = spinners.get(name) or Spinner("line", style="cyan")
                spinner.text = Text("running", style="cyan")
                table.add_row(label, spinner, f"{detail} {ticking}".strip())
            else:
                table.add_row(label, Text(state, style=styles.get(state, "dim")), detail)
        return table

    def status(self) -> str:
        """One line under the table: how long, who is working right now, what it has cost."""
        with self._lock:
            working = [_step_label(n) for n in self.order if self.state[n][0] == "running"]
        said = [f"{self.clock() - self.began:.0f}s"]
        if working:
            said.append(f"working: {', '.join(working)}")
        said.append(f"{self.usage.tokens} tokens")
        if self.usage.cost:
            # Only a declared price makes a cost; without one, a zero would read as free.
            said.append(f"cost {self.usage.cost}")
        return " | ".join(said)

    def frame(self) -> Group:
        return Group(self.table(), Text(self.status(), style="dim"))


def _step_label(name: str) -> str:
    return name.replace("_analyst", "").replace("validator_", "validator ").replace("_", " ")


def countdown(seconds: float, *, until: str = "the next cycle") -> None:
    """Wait, with the time left on screen, so a desk between cycles does not look hung."""
    deadline = monotonic() + max(0.0, seconds)
    with Live(console=console(), refresh_per_second=2, transient=True) as live:
        while True:
            left = deadline - monotonic()
            if left <= 0:
                return
            minutes, secs = divmod(int(left + 0.999), 60)
            shown = f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"
            live.update(
                Text(f"{until} in {shown}; Ctrl-C stops the desk", style="dim"), refresh=True
            )
            sleep(min(1.0, left))


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


def screen_view(record: Mapping[str, Any]) -> Group:
    """One pass over the universe: what code found and measured, and what the screener chose."""
    from trading_research_agents.execution.universe import why_no_cycle

    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="the universe")
    for name, justify in (
        ("instrument", "left"),
        ("state", "left"),
        ("pick", "left"),
        ("regime", "left"),
        ("zscore", "right"),
        ("moved bps", "right"),
        ("spread bps", "right"),
        ("24h vol", "right"),
        ("why", "left"),
    ):
        table.add_column(name, justify=justify)  # type: ignore[arg-type]
    chosen = list(record.get("chosen") or ())
    picked = any(row.get("pick_rank") for row in record.get("candidates") or [])
    styles = {"taken": "green", "waiting": "cyan", "held": "yellow"}
    rows = sorted(
        record.get("candidates") or [],
        key=lambda r: (r.get("pick_rank") or 10_000, 0 if r.get("admitted") else 1),
    )
    for row in rows:
        state = (
            "taken"
            if row["instrument"] in chosen
            else ("waiting" if row["admitted"] and row["kept_by_screener"] else "held")
        )
        why = row.get("screener_said") or (row.get("reasons") or [""])[0]
        if state == "waiting":
            ranked = "the screener's order" if picked else "pressure"
            why = (
                f"admitted, but [universe].max_per_cycle was taken by {', '.join(chosen)}, "
                f"ahead on {ranked}; {why}"
            )
        seen = row.get("features") or {}
        rank = row.get("pick_rank")
        table.add_row(
            str(row["instrument"]),
            Text(state, style=styles.get(state, "")),
            f"#{rank} {row.get('setup')}" if rank else "",
            str(seen.get("regime") or ""),
            _fmt(seen.get("zscore")),
            _fmt(row.get("moved_bps")),
            _fmt(row.get("spread_bps")),
            _volume(seen.get("quote_volume_24h")),
            str(why),
        )
    found = record.get("discovered") or {}
    if found:
        dropped = ", ".join(f"{n} {why}" for why, n in sorted((found.get("dropped") or {}).items()))
        table.title = (
            f"the universe: {len(found.get('kept') or [])} of {found.get('markets')} perpetuals "
            f"kept by code ({dropped})"
        )
    code = list(record.get("code_order") or [])
    if chosen:
        caption = f"{len(chosen)} taken"
        if picked:
            caption += f"; the screener chose, code's own order was {', '.join(code[:5]) or '-'}"
        else:
            caption += "; code's order (pressure), no screener asked"
    else:
        caption = f"no cycle this pass: {why_no_cycle(record.get('candidates') or [])}" + (
            "" if record.get("asked_screener") else "; no model was asked"
        )
    table.caption = caption
    return Group(table)


def _volume(value: Any) -> str:
    """24-hour quote volume in millions, the unit a trader reads it in."""
    if value in (None, ""):
        return ""
    try:
        return f"{Decimal(str(value)) / 1_000_000:,.0f}M"
    except ArithmeticError:
        return str(value)


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
                        + (f", filled {filled}" if filled not in (None, "") else ""),
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

    if record.get("series"):
        read = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="series read")
        for name in ("analyst", "series", "value", "as of", "age h", "state", "from"):
            read.add_column(name)
        for row in record["series"]:
            state = str(row.get("state"))
            read.add_row(
                str(row.get("role", "")).removesuffix("_analyst"),
                str(row.get("series")),
                str(row.get("value")),
                str(row.get("observed_at") or "")[:16],
                str(row.get("age_h")),
                Text(state, style="yellow" if state in ("stale", "unknown") else ""),
                f"{row.get('origin')}; {row.get('source') or 'no source named'}",
            )
        parts.append(read)

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
        for role, why in sorted((record.get("not_asked") or {}).items()):
            said = str(why).removeprefix(role + ": ")
            who.add_row(role, Text(f"not asked: {said}", style="yellow"), "")
        parts.append(who)
    return Group(*parts)


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
    """Draw it, and never let a character a console cannot encode end a run.

    A model's answer carries whatever the model wrote - a non-breaking hyphen, an accent, a dash
    from a headline - and an older Windows console encodes none of those. The run has already
    happened and is already on disk by the time this draws it, so a console that cannot show a
    character falls back to text rather than raising in front of the operator.
    """
    try:
        console().print(renderable)
    except UnicodeEncodeError:
        console(safe=True).print(renderable)


def _safe_console() -> Console:
    """The same console, writing through a stream that replaces what it cannot encode."""
    stream = getattr(sys.stdout, "buffer", None)
    file = (
        io.TextIOWrapper(stream, encoding="utf-8", errors="backslashreplace", newline="")
        if stream is not None
        else sys.stdout
    )
    return Console(highlight=False, markup=False, file=file)


Renderer = Callable[[], Any]
