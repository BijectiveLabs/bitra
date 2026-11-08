"""`[event_guard]`: whether the typed judge's reading of the day's headlines may hold a new entry.

The judge reads; the system decides. An adverse event (by default a hack, an outage, a delisting or
a restriction by a regulator, a court or a government) that concerns the instrument's asset, or the
whole market, at or above `hold_at` holds the entry. In `paper`, the default, it only records what
it would have held, so one desk keeps both arms on the same days: the rule alone, and the rule
with the guard. `hold` holds, and fails closed when the headlines could not be read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MODES = ("paper", "hold")
DEFAULT_KINDS = ("hack", "outage", "delisting", "restriction")
KEYS = ("mode", "hold_at", "window_h", "kinds")


@dataclass(frozen=True)
class EventGuard:
    """The mode, the probability that holds, how far back headlines are read, and which kinds."""

    mode: str
    hold_at: float
    window_h: int = 24
    kinds: tuple[str, ...] = DEFAULT_KINDS

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"[event_guard].mode is one of {list(MODES)}, not {self.mode!r}")
        if not 0 < self.hold_at <= 1:
            raise ValueError("[event_guard].hold_at is a probability between 0 and 1")
        if self.window_h < 1:
            raise ValueError("[event_guard].window_h is at least one hour")
        if not self.kinds:
            raise ValueError("[event_guard].kinds names at least one kind of event")

    @property
    def holds(self) -> bool:
        return self.mode == "hold"

    def record(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "hold_at": self.hold_at,
            "window_h": self.window_h,
            "kinds": list(self.kinds),
        }


def event_guard(table: Mapping[str, Any] | None) -> EventGuard | None:
    """The guard `[event_guard]` declares; None when the table is absent."""
    if table is None:
        return None
    unknown = sorted(set(table) - set(KEYS))
    if unknown:
        raise ValueError(f"[event_guard]: unknown {unknown}; have {list(KEYS)}")
    if "hold_at" not in table:
        raise ValueError(
            "[event_guard].hold_at is the probability that holds an entry; it is declared, so a "
            "threshold is never applied without someone having chosen it"
        )
    kinds = table.get("kinds", DEFAULT_KINDS)
    return EventGuard(
        mode=str(table.get("mode", "paper")),
        hold_at=float(table["hold_at"]),
        window_h=int(table.get("window_h", 24)),
        kinds=tuple(str(k) for k in kinds),
    )


__all__ = ["DEFAULT_KINDS", "EventGuard", "event_guard"]
