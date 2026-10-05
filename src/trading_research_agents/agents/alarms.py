"""What went wrong in a cycle, in a vocabulary something can be woken by.

An operator running this on a server is not watching it. The failure that matters to them is the one
that stops the desk quietly: a provider rate limits the roster halfway through, the run keeps going
through the motions, and nobody finds out until someone reads a log. The auditors already exist and
already read these records - they were only ever asked by hand.

So a cycle's own record is read for anomalies, in a closed vocabulary, and a scheduled run wakes the
technical auditor when one fires. What it produces is what it always produces: findings tied to
figures, and proposals a person applies. Nothing here takes an action, changes a limit or stops a
run, because an auditor that can act is a second desk nobody declared.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# One word per kind of thing that goes wrong, so a cooldown and a report can both be keyed by it.
STOPPED = "stopped"
PROVIDER = "provider"
DATA = "data"
VENUE = "venue"
KINDS = (STOPPED, PROVIDER, DATA, VENUE)


@dataclass(frozen=True)
class Alarm:
    """One anomaly a cycle recorded, by kind, with the record's own words."""

    kind: str
    detail: str
    at: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"an alarm's kind is one of {list(KINDS)}")

    def record(self) -> dict[str, str]:
        return {"kind": self.kind, "detail": self.detail, "at": self.at}


def alarms_of(record: Mapping[str, Any]) -> tuple[Alarm, ...]:
    """Every anomaly in one cycle's record, in a fixed order, without repeating a kind.

    A refusal is not an anomaly: a gate saying no is the software working. What is here is the
    cycle failing to finish, a model call that did not come back, a data chain that ran out, and a
    venue that would not take the order.
    """
    at = str(record.get("cycle_id", ""))
    found: list[Alarm] = []
    seen: set[str] = set()

    def add(kind: str, detail: str) -> None:
        if kind in seen or not detail:
            return
        seen.add(kind)
        found.append(Alarm(kind=kind, detail=detail, at=at))

    stopped = record.get("stopped_by")
    if stopped:
        add(STOPPED, str(stopped))

    for key in ("agents", "validators"):
        failed = (record.get(key) or {}).get("failed") if isinstance(record.get(key), dict) else {}
        if isinstance(failed, dict) and failed:
            first = sorted(failed.items())[0]
            add(PROVIDER, f"{first[0]}: {first[1]}")

    for refusal in record.get("refusals") or []:
        text = str(refusal)
        if text.startswith("data "):
            add(DATA, text)
        elif text.startswith("venue:"):
            add(VENUE, text)

    placed = record.get("placed")
    if isinstance(placed, dict) and placed.get("sent") is False:
        add(VENUE, str(placed.get("error") or placed.get("refused") or "the venue refused"))
    return tuple(found)


@dataclass
class Watch:
    """Whether an alarm should wake the auditor now, given what has already been reported.

    One cooldown per kind, not one overall: a provider failing every cycle should be reported once
    an hour, and a venue refusing an hour later is a different thing that deserves its own look.
    """

    cooldown_s: int = 1800
    last: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.cooldown_s < 0:
            raise ValueError("[audit].alarm_cooldown_s is not negative")

    def due(self, alarms: tuple[Alarm, ...], now: float) -> tuple[Alarm, ...]:
        """The alarms worth waking the auditor for, and they are marked as reported."""
        out = []
        for alarm in alarms:
            before = self.last.get(alarm.kind)
            if before is not None and now - before < self.cooldown_s:
                continue
            self.last[alarm.kind] = now
            out.append(alarm)
        return tuple(out)
