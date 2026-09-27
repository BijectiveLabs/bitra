"""The era seal: research never touches sealed data, and a seal is opened once.

Both halves are code rather than convention, because a rule that depends on remembering has a
decay rate, and the moment the temptation peaks is precisely the moment a marginal result needs
one more look. A held-out era opened twice is a research era with extra steps: the second look
is informed by the first, whatever it claims to be testing.

The one seal the research programme opened caught a mechanism that had inverted out of sample
after passing every gate before it. It could only catch it once.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from trading_research_agent.chain.preregistration import Era

NS_PER_S = 1_000_000_000


class EraLeakageError(RuntimeError):
    """A research path touched a row outside the research era. Do not catch and continue."""


class SealError(RuntimeError):
    """The seal cannot be opened as asked."""


def day_start_ns(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * NS_PER_S


def day_end_ns(day: date) -> int:
    """Exclusive end: the first nanosecond of the following day, so a boundary day belongs whole."""
    return day_start_ns(day + timedelta(days=1))


def era_of(ts_ns: int, era: Era) -> str:
    """'research', the sealed era's name, or 'outside' when the timestamp belongs to none."""
    if day_start_ns(era.research_start) <= ts_ns < day_end_ns(era.research_end):
        return "research"
    for sealed in era.sealed:
        end = None if sealed.end is None else day_end_ns(sealed.end)
        if ts_ns >= day_start_ns(sealed.start) and (end is None or ts_ns < end):
            return sealed.name
    return "outside"


def research_only(timestamps: Iterable[int], era: Era, *, who: str) -> None:
    """Raise if even one timestamp lies outside the research era.

    Raises rather than clipping. A silent clip would let an instrument run on less data than it
    asked for and return a result nobody could interpret. An empty input is valid; whether the
    empty input is the one the caller expected is the caller's problem, not this function's.
    """
    lo, hi = day_start_ns(era.research_start), day_end_ns(era.research_end)
    outside: dict[str, int] = {}
    for ts in timestamps:
        if lo <= ts < hi:
            continue
        name = era_of(ts, era)
        outside[name] = outside.get(name, 0) + 1
    if outside:
        parts = ", ".join(f"{n} row(s) in {name}" for name, n in sorted(outside.items()))
        raise EraLeakageError(
            f"era seal violated by {who!r}: {parts}. Research instruments do not touch held-out "
            "data; the seal is opened once, at the gate. Fix the filter upstream."
        )


@dataclass(frozen=True)
class SealReceipt:
    era: str
    opened_at: datetime
    specification_hash: str
    gates: tuple[str, ...]


class Seal:
    """Single-shot bookkeeping for one held-out era, persisted in a file written once.

    The specification hash and the acceptance gates are fixed at construction, before the seal
    can be opened, which is what makes the eventual `open` a verification rather than a
    declaration. This class cannot stop a caller from deleting the file; it can only make lying
    to it a deliberate act rather than an accident.
    """

    def __init__(
        self, era: str, *, specification_hash: str, gates: tuple[str, ...], path: Path
    ) -> None:
        if not era.strip():
            raise SealError("a seal names its era")
        if not gates:
            raise SealError(
                f"cannot seal {era!r} without acceptance gates fixed in advance; opening a seal "
                "and then deciding what counts as passing is not an out-of-sample test"
            )
        if len(specification_hash) != 64:
            raise SealError("specification_hash is the 64-character seal of a pre-registration")
        self.era = era
        self.specification_hash = specification_hash
        self.gates = tuple(gates)
        self.path = path

    def state(self) -> dict[str, object]:
        if not self.path.exists():
            return {"opened": False}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SealError(f"{self.path}: seal state is unreadable: {exc}") from exc
        if not isinstance(data, dict):
            raise SealError(f"{self.path}: seal state is not an object")
        return data

    def is_open(self) -> bool:
        return bool(self.state().get("opened", False))

    def open(self, specification_hash: str, *, opened_at: datetime) -> SealReceipt:
        """Open once, against the pre-registered specification. Already-open is checked first.

        The order matters: a second call is refused as a re-open even when it carries a different
        specification, so it is not possible to route around the single-shot rule by claiming a
        new specification.
        """
        current = self.state()
        if current.get("opened"):
            raise SealError(
                f"seal on {self.era!r} was already opened at {current.get('opened_at')} against "
                f"{str(current.get('specification_hash'))[:12]}...; a held-out era opened twice "
                "is a research era with extra steps"
            )
        if specification_hash != self.specification_hash:
            raise SealError(
                f"seal on {self.era!r} is pre-registered against "
                f"{self.specification_hash[:12]}...; this request carries "
                f"{specification_hash[:12]}..., which is not the pre-registered specification"
            )
        if opened_at.utcoffset() is None or opened_at.utcoffset() != UTC.utcoffset(None):
            raise SealError("opened_at must be timezone-aware, in UTC")
        receipt = SealReceipt(
            era=self.era,
            opened_at=opened_at,
            specification_hash=specification_hash,
            gates=self.gates,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "era": self.era,
            "opened": True,
            "opened_at": opened_at.isoformat(),
            "specification_hash": specification_hash,
            "gates": list(self.gates),
        }
        # Exclusive create: two processes racing to open the same seal cannot both succeed.
        with self.path.open("x", encoding="utf-8") as fh:
            fh.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
        return receipt
