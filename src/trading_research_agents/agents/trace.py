"""A timeline of a run or a session, read back from the files it wrote.

Nothing here is computed at the time of the run; the trace is the run's own record put in
order. A run directory yields two clocks that must not be mixed: the market clock of the
bars (intents, fills, round trips) and the wall clock of the model calls (`usage.jsonl`).
A session directory adds the role answers in the order they were asked, the auditor's tool
steps, and one run trace per kept instrument.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.llm.usage import UsageLedger


class TraceError(ValueError):
    """The path is neither a run directory nor a session directory."""


@dataclass(frozen=True)
class Event:
    at: str
    kind: str
    who: str
    detail: str

    def record(self) -> dict[str, str]:
        return {"at": self.at, "kind": self.kind, "who": self.who, "detail": self.detail}


@dataclass(frozen=True)
class Trace:
    path: Path
    kind: str
    header: dict[str, Any]
    calls: list[Event]
    market: list[Event]
    roles: list[Event]

    def record(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "header": self.header,
            "calls": [e.record() for e in self.calls],
            "market": [e.record() for e in self.market],
            "roles": [e.record() for e in self.roles],
        }


def _market_time(ns: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ns) / 1e9, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _wall_time(at: Any) -> str:
    try:
        return datetime.fromtimestamp(float(at), tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _bps(text: Any) -> str:
    try:
        return f"{Decimal(str(text)):+.2f}"
    except (ArithmeticError, ValueError):
        return str(text)


def call_events(path: Path) -> list[Event]:
    """Every model call in `usage.jsonl`, wall clock, with running tokens and cost."""
    events = []
    tokens = 0
    cost = Decimal(0)
    for row in UsageLedger.read(path):
        tokens += int(row.get("input_tokens", 0)) + int(row.get("output_tokens", 0))
        if row.get("cost") is not None:
            cost += Decimal(str(row["cost"]))
        outcome = str(row.get("outcome", ""))
        detail = (
            f"{row.get('model') or '-'} {row.get('input_tokens', 0)}+{row.get('output_tokens', 0)} "
            f"tokens, {row.get('latency_ms', 0)} ms; total {tokens} tokens, cost {cost}"
        )
        if outcome != "ok":
            detail = f"{outcome}; {detail}"
        events.append(Event(_wall_time(row.get("at")), "call", str(row.get("role", "")), detail))
    return events


def market_events(run_dir: Path, instrument: str) -> list[Event]:
    """Intents, fills and closed round trips of one run, in market time."""
    events: list[tuple[int, int, Event]] = []
    for row in _rows(run_dir / "intents.jsonl"):
        ts = int(row.get("ts_ns", 0))
        kind = "intent" if row.get("admitted", True) else "refusal"
        detail = f"{row.get('target')} {row.get('size')}: {row.get('decision')}"
        if row.get("reason"):
            detail += f" ({row['reason']})"
        events.append((ts, 0, Event(_market_time(ts), kind, instrument, detail)))
    for row in _rows(run_dir / "fills.jsonl"):
        ts = int(row.get("ts_ns", 0))
        side = "close" if row.get("closes") else "open"
        detail = f"{side} {row.get('direction')} {row.get('size')} at {row.get('price')}"
        events.append((ts, 1, Event(_market_time(ts), "fill", instrument, detail)))
    for row in _rows(run_dir / "trades.jsonl"):
        ts = int(row.get("exit_ts_ns", 0))
        net = _bps(row.get("net_bps"))
        detail = (
            f"{row.get('direction')} held {row.get('bars_held')} bars, net {net} bps, "
            f"cause {row.get('cause')}"
        )
        events.append((ts, 2, Event(_market_time(ts), "round_trip", instrument, detail)))
    return [e for _, _, e in sorted(events, key=lambda t: (t[0], t[1]))]


def _run_header(summary: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "label",
        "instrument",
        "started_at",
        "finished_at",
        "prints",
        "bars",
        "intents",
        "refused",
        "fills",
        "round_trips",
        "net_bps_total",
        "stopped_by",
    )
    return {k: summary.get(k) for k in keys}


def trace_run(run_dir: Path) -> Trace:
    summary = json.loads((run_dir / "run.json").read_text("utf-8"))
    instrument = str(summary.get("instrument", run_dir.name))
    return Trace(
        path=run_dir,
        kind="run",
        header=_run_header(summary),
        calls=call_events(run_dir / "usage.jsonl"),
        market=market_events(run_dir, instrument),
        roles=[],
    )


def _answer_line(role: str, response: Any) -> str:
    try:
        answer = json.loads(response) if isinstance(response, str) else response
    except json.JSONDecodeError:
        return "unparseable answer"
    if not isinstance(answer, dict):
        return "unparseable answer"
    if role == "auditor":
        return f"{answer.get('verdict')}: {answer.get('summary', '')}"
    return json.dumps(answer, sort_keys=True)[:160]


def role_events(session_dir: Path) -> list[Event]:
    """The recorded role answers in the order they were asked, with the auditor's steps."""
    events = []
    for path in sorted(session_dir.glob("[0-9][0-9]-*.json")):
        try:
            saved = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(saved, dict):
            continue
        role = str(saved.get("role", path.stem.partition("-")[2]))
        events.append(Event(path.name, "answer", role, _answer_line(role, saved.get("response"))))
        for step in saved.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            if step.get("tool"):
                detail = f"{step['tool']}({json.dumps(step.get('arguments', {}), sort_keys=True)})"
                if step.get("error"):
                    detail += f" -> {step['error']}"
            elif step.get("error"):
                detail = str(step["error"])
            else:
                detail = "answered"
            events.append(Event(f"step {step.get('index', '?')}", "step", role, detail))
    return events


def trace_session(session_dir: Path) -> Trace:
    summary = json.loads((session_dir / "session.json").read_text("utf-8"))
    header = {
        k: summary.get(k)
        for k in (
            "session_id",
            "started_at",
            "finished_at",
            "runs",
            "audit_verdict",
            "tokens_total",
            "cost_total",
            "stopped_by",
        )
    }
    for key in ("kept", "excluded"):
        rows = summary.get(key) or []
        header[key] = [r.get("instrument") for r in rows if isinstance(r, dict)]
    market: list[Event] = []
    for run_json in sorted(session_dir.glob("*/run.json")):
        market.extend(market_events(run_json.parent, run_json.parent.name))
    return Trace(
        path=session_dir,
        kind="session",
        header=header,
        calls=call_events(session_dir / "usage.jsonl"),
        market=market,
        roles=role_events(session_dir),
    )


def trace(path: Path) -> Trace:
    if (path / "session.json").is_file():
        return trace_session(path)
    if (path / "run.json").is_file():
        return trace_run(path)
    raise TraceError(f"{path} has neither session.json nor run.json")


def lines(t: Trace) -> list[str]:
    """The trace as text: header, roles, model calls on the wall clock, the market clock."""
    out = [f"{t.kind} {t.path}"]
    for key, value in t.header.items():
        if value not in (None, [], {}):
            out.append(f"  {key}: {value}")
    if t.roles:
        out.append("roles:")
        out.extend(f"  {e.at:<22} {e.who:<18} {e.detail}" for e in t.roles)
    if t.calls:
        out.append("model calls (wall clock, UTC):")
        out.extend(f"  {e.at:<22} {e.who:<18} {e.detail}" for e in t.calls)
    if t.market:
        out.append("market (bar time, UTC):")
        out.extend(f"  {e.at:<22} {e.kind:<10} {e.who:<10} {e.detail}" for e in t.market)
    return out
