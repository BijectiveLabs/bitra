"""The risk manager inside `tra run`: asked between cycles, able only to make the desk more careful.

It is woken by the same things that wake the quant auditor - a loss limit reached, a losing streak,
the clock - and it is handed what code measured from the run's own files. It may cut the size of
every new entry, pause instruments, or halt the desk. Every one of those holds for a stated number
of hours and then lapses back to the client's own settings; none can loosen what the client wrote,
because the client's limits stay in code and are applied after these.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trading_research_agents.llm.form import (
    FormError,
    _enum,
    _list,
    _posint,
    _text,
    parse_object,
)
from trading_research_agents.records import read_lines, write_json

ROLE = "risk_manager"
PROMPT = "risk_manager_run"
DECISIONS = ("continue", "reduce", "pause", "halt")
STATE = "risk.json"
MAX_HOLD_HOURS = 72

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "size_multiple": {"type": ["number", "null"]},
        "pause_instruments": {"type": "array", "items": {"type": "string"}},
        "hold_hours": {"type": "integer", "minimum": 1},
        "reason": {"type": "string"},
        "triggers": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "decision",
        "size_multiple",
        "pause_instruments",
        "hold_hours",
        "reason",
        "triggers",
    ],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class RunDecision:
    decision: str
    size_multiple: Decimal | None
    pause_instruments: tuple[str, ...]
    hold_hours: int
    reason: str
    triggers: tuple[str, ...]


def parse(text: str) -> RunDecision:
    data = parse_object(text, SCHEMA)
    decision = _enum(data.get("decision"), "decision", DECISIONS)
    multiple = data.get("size_multiple")
    size_multiple: Decimal | None = None
    if decision == "reduce":
        try:
            size_multiple = Decimal(str(multiple))
        except (InvalidOperation, ValueError) as exc:
            raise FormError("reduce names a size_multiple") from exc
        if not Decimal(0) < size_multiple < Decimal(1):
            raise FormError("size_multiple is above 0 and below 1; a reduction never adds size")
    paused = tuple(str(x).strip().upper() for x in _list(data.get("pause_instruments"), "pause"))
    if decision == "pause" and not paused:
        raise FormError("pause names the instruments it pauses")
    return RunDecision(
        decision=decision,
        size_multiple=size_multiple,
        pause_instruments=tuple(p for p in paused if p),
        hold_hours=min(_posint(data.get("hold_hours"), "hold_hours"), MAX_HOLD_HOURS),
        reason=_text(data.get("reason"), "reason"),
        triggers=tuple(str(t) for t in _list(data.get("triggers"), "triggers")),
    )


@dataclass
class Overrides:
    """What the risk manager has put in force, read from and written to the run directory."""

    path: Path
    size_multiple: Decimal = Decimal(1)
    size_until: datetime | None = None
    paused: dict[str, datetime] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path, now: datetime) -> Overrides:
        path = Path(root) / STATE
        out = cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return out
        until = _moment(raw.get("size_until"))
        if until is not None and until > now:
            try:
                out.size_multiple = Decimal(str(raw.get("size_multiple", "1")))
                out.size_until = until
            except InvalidOperation:
                pass
        for name, stamp in (raw.get("paused") or {}).items():
            moment = _moment(stamp)
            if moment is not None and moment > now:
                out.paused[str(name)] = moment
        return out

    def multiple(self, now: datetime) -> Decimal:
        if self.size_until is None or self.size_until <= now:
            return Decimal(1)
        return self.size_multiple

    def pause_of(self, instrument: str, now: datetime) -> datetime | None:
        # "*" is every instrument: a halt on a desk with no kill switch to arm.
        until = self.paused.get(instrument.upper()) or self.paused.get("*")
        return until if until is not None and until > now else None

    def apply(self, decision: RunDecision, now: datetime) -> list[str]:
        """Put a decision in force, one way only, and say what changed."""
        until = now + timedelta(hours=decision.hold_hours)
        said: list[str] = []
        if decision.decision == "reduce" and decision.size_multiple is not None:
            # Two reductions compound downward; a later, milder one never undoes an earlier cut.
            self.size_multiple = min(self.multiple(now), decision.size_multiple)
            self.size_until = max(self.size_until or until, until)
            said.append(f"new entries at {self.size_multiple}x size until {until:%Y-%m-%d %H:%M}Z")
        if decision.decision == "pause":
            for name in decision.pause_instruments:
                self.paused[name] = max(self.paused.get(name, until), until)
            said.append(f"paused {', '.join(decision.pause_instruments)} until {until:%H:%M}Z")
        if said:
            self.save()
        return said

    def save(self) -> None:
        write_json(
            self.path,
            {
                "size_multiple": str(self.size_multiple),
                "size_until": None if self.size_until is None else self.size_until.isoformat(),
                "paused": {k: v.isoformat() for k, v in self.paused.items()},
            },
            durable=True,
        )

    def record(self, now: datetime) -> dict[str, Any]:
        return {
            "size_multiple": str(self.multiple(now)),
            "size_until": None if self.size_until is None else self.size_until.isoformat(),
            "paused": {k: v.isoformat() for k, v in self.paused.items() if v > now},
        }


def brief(
    root: Path, *, limits: Any, overrides: Overrides, alarms: list[dict[str, str]], now: datetime
) -> dict[str, Any]:
    """What code measured, from the run's own files, for the risk manager to judge."""
    trips = [t for t in read_lines(Path(root) / "round_trips.jsonl") if "net" in t]
    nets = []
    days = []
    for trip in trips:
        try:
            nets.append((str(trip.get("instrument", "")), Decimal(str(trip["net"]))))
        except InvalidOperation:
            continue
        days.append(str(trip.get("at", ""))[:10])
    recent = nets[-20:]
    streak = 0
    for _, net in reversed(nets):
        if net >= 0:
            break
        streak += 1
    per: dict[str, dict[str, Any]] = {}
    for name, net in recent:
        row = per.setdefault(name, {"trips": 0, "wins": 0, "net": Decimal(0)})
        row["trips"] += 1
        row["wins"] += int(net > 0)
        row["net"] += net
    day = now.astimezone(UTC).date().isoformat()
    start = next(
        (
            row.get("equity")
            for row in read_lines(Path(root) / "equity.jsonl")
            if row.get("day") == day
        ),
        None,
    )
    today = [net for (_, net), on in zip(nets, days, strict=True) if on == day]
    day_net = sum(today, Decimal(0))
    return {
        # Models mix up money and basis points when both are bare numbers, so each says which.
        "units": {
            "net": "quote currency (USDT on most desks), summed over round trips",
            "*_bps": "basis points of equity: the limits and net_bps_of_equity",
        },
        "today": {
            "day": day,
            "round_trips": len(today),
            "net": str(day_net),
            "net_bps_of_equity": _bps_of(day_net, start),
            "equity_at_day_start": start,
        },
        "limits": {
            "max_risk_per_trade_bps": str(limits.max_risk_per_trade_bps),
            "daily_loss_limit_bps": str(limits.daily_loss_limit_bps),
            "max_drawdown_bps": (
                None if limits.max_drawdown_bps is None else str(limits.max_drawdown_bps)
            ),
        },
        "in_force": overrides.record(now),
        "alarms": alarms,
        "round_trips": {
            "closed": len(nets),
            "last_20_wins": sum(1 for _, n in recent if n > 0),
            "last_20_net": str(sum((n for _, n in recent), Decimal(0))),
            "losing_streak": streak,
            "per_instrument_last_20": {
                k: {**v, "net": str(v["net"])} for k, v in sorted(per.items())
            },
        },
        "at": now.astimezone(UTC).isoformat(),
    }


def _bps_of(amount: Decimal, equity: Any) -> str | None:
    try:
        base = Decimal(str(equity))
    except (InvalidOperation, ValueError):
        return None
    if not base.is_finite() or base <= 0:
        return None
    return str((amount / base * 10_000).quantize(Decimal("0.01")))


def _moment(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
