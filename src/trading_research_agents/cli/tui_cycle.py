"""One cycle on a terminal: the steps as they happen, and the record laid out as panels."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from threading import Lock
from time import monotonic
from types import TracebackType
from typing import Any

from rich import box
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from trading_research_agents.cli.tui import _fmt, _kv, console
from trading_research_agents.cli.tui_panels import (
    _agents_table,
    _confluence_panel,
    _conviction_panel,
    _cost_table,
    _cycle_panel,
    _data_table,
    _reviewer_table,
    _risk_panel,
    _series_table,
    _targets_panel,
)


class CycleView:
    """The steps of a cycle, or of a pass over the universe, as a live table.

    The whole pipeline the operator configured is laid out before anything starts, every step
    pending, so a terminal is never blank while models think. A running step shows a spinner and
    its own clock ticking; a finished one shows how it ended and how long it took. Model calls run
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


def _screen_state(row: Mapping[str, Any], chosen: list[str]) -> str:
    if row["instrument"] in chosen:
        return "taken"
    # A screen recorded before 0.12 may say a screener passed an admitted instrument over.
    return "waiting" if row["admitted"] and row.get("kept_by_screener", True) else "held"


def _step_label(name: str) -> str:
    return name.replace("_analyst", "").replace("reviewer_", "reviewer ").replace("_", " ")


def screen_view(record: Mapping[str, Any]) -> Group:
    """One pass over the universe: what code found and measured, and the order it took them in."""
    from trading_research_agents.execution.universe import why_no_cycle

    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="the universe")
    for name, justify in (
        ("instrument", "left"),
        ("state", "left"),
        ("room", "right"),
        ("regime", "left"),
        ("zscore", "right"),
        ("moved bps", "right"),
        ("spread bps", "right"),
        ("24h vol", "right"),
        ("why", "left"),
    ):
        table.add_column(name, justify=justify)  # type: ignore[arg-type]
    chosen = list(record.get("chosen") or ())
    styles = {"taken": "green", "waiting": "cyan", "held": "yellow"}
    rows = sorted(record.get("candidates") or [], key=lambda r: 0 if r.get("admitted") else 1)
    for row in rows:
        state = _screen_state(row, chosen)
        why = (row.get("reasons") or [""])[0]
        if state == "waiting":
            why = (
                f"admitted, but [universe].max_per_cycle was taken by {', '.join(chosen)}, "
                f"ahead on room and pressure; {why}"
            )
        seen = row.get("features") or {}
        table.add_row(
            str(row["instrument"]),
            Text(state, style=styles.get(state, "")),
            _fmt(row.get("room")),
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
            f"kept by the system ({dropped})"
        )
    if chosen:
        caption = f"{len(chosen)} taken, in the system's order: room, then pressure"
    else:
        caption = f"no cycle this pass: {why_no_cycle(record.get('candidates') or [])}"
    caption += "; no model was asked"
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
    parts.extend(_cycle_panel(record))
    parts.extend(_data_table(record))
    parts.extend(_series_table(record))
    parts.extend(_cost_table(record))
    parts.extend(_reviewer_table(record))
    parts.extend(_confluence_panel(record))
    parts.extend(_conviction_panel(record))
    parts.extend(_risk_panel(record))
    parts.extend(_targets_panel(record))
    parts.extend(_agents_table(record))
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
