"""A single ledger row, validated on construction.

An entry that cannot be constructed cannot be written, so the consistency rules between a
verdict and its axes are enforced here rather than at review time. The rules are the ones the
research record adopted after eleven dossiers were found carrying the wrong label.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from trading_research_agent.ledger.vocabulary import (
    ENVELOPE_BOUND_AXES,
    UNIVERSAL_AXES,
    Axis,
    Transferability,
    Verdict,
)

LAST_LAYER = 8


class LedgerError(ValueError):
    """Raised when an entry, or an operation on the ledger, breaks a rule."""


@dataclass(frozen=True)
class ReversalPoint:
    """The declared parameter value at which an envelope-bound verdict reverses.

    A cost-axis verdict closed at an 11.0 bps round trip is not closed at 3.0 bps. Recording
    the reversal point is what lets a reader with a different fee schedule reuse the verdict.
    """

    parameter: str
    value: Decimal
    unit: str
    comparison: str = "<"

    def __post_init__(self) -> None:
        if not self.parameter.strip():
            raise LedgerError("reversal point needs a parameter name")
        if not isinstance(self.value, Decimal):
            raise LedgerError("reversal point value must be a Decimal, not a float")
        if self.comparison not in ("<", "<=", ">", ">="):
            raise LedgerError(f"unsupported comparison {self.comparison!r}")


@dataclass(frozen=True)
class LedgerEntry:
    """One row of the kill ledger."""

    entry_id: str
    subject: str
    verdict: Verdict
    recorded_at: datetime
    layer: int | None = None
    axis_of_death: tuple[Axis, ...] = ()
    blocking_axis: Axis | None = None
    transferability: Transferability | None = None
    reversal_point: ReversalPoint | None = None
    evidence: tuple[str, ...] = ()
    envelope: tuple[tuple[str, str], ...] = ()
    retracts: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        _validate(self)

    @property
    def final_axis(self) -> Axis | None:
        # With "Data, then Idea" the hypothesis died on Idea once the data defect was fixed.
        return self.axis_of_death[-1] if self.axis_of_death else None


def _validate(entry: LedgerEntry) -> None:
    if not entry.entry_id.strip():
        raise LedgerError("entry_id is required")
    if not entry.subject.strip():
        raise LedgerError("subject is required")
    _check_clock(entry.recorded_at)
    if entry.layer is not None and not 0 <= entry.layer <= LAST_LAYER:
        raise LedgerError(f"layer must be between 0 and {LAST_LAYER}, got {entry.layer}")
    if len(set(entry.axis_of_death)) != len(entry.axis_of_death):
        raise LedgerError("axis_of_death repeats an axis")
    if entry.retracts is not None and entry.verdict is not Verdict.WITHDRAWN:
        raise LedgerError("only a WITHDRAWN entry may retract another entry")

    verdict = entry.verdict
    if verdict.is_death:
        _validate_death(entry)
    elif verdict.is_live:
        _validate_live(entry)
    elif verdict.is_characterisation:
        _validate_characterisation(entry)
    else:
        _validate_withdrawal(entry)


def _check_clock(recorded_at: datetime) -> None:
    offset = recorded_at.utcoffset()
    if offset is None:
        raise LedgerError("recorded_at must be timezone-aware, in UTC")
    if offset != UTC.utcoffset(None):
        raise LedgerError("recorded_at must be in UTC")


def _validate_death(entry: LedgerEntry) -> None:
    if not entry.axis_of_death:
        raise LedgerError(f"{entry.verdict.value} requires an axis of death")
    if entry.blocking_axis is not None:
        raise LedgerError("a settled verdict carries no blocking axis")
    if Axis.POWER in entry.axis_of_death:
        # Power is not a death. An underpowered result is NOT PROVEN and stays open.
        raise LedgerError("POWER cannot be an axis of death; use NOT PROVEN with a blocking axis")
    if entry.transferability is None:
        raise LedgerError(f"{entry.verdict.value} requires a transferability classification")

    final = entry.axis_of_death[-1]
    universal = entry.transferability is Transferability.UNIVERSAL
    if final in UNIVERSAL_AXES and not universal:
        raise LedgerError(f"a death on {final.value} is UNIVERSAL")
    if final in ENVELOPE_BOUND_AXES and universal:
        raise LedgerError(f"a death on {final.value} is ENVELOPE-BOUND")

    if entry.verdict is Verdict.CLOSED:
        # CLOSED means the mechanism measured real and a declared envelope parameter blocked it.
        if entry.transferability is not Transferability.ENVELOPE_BOUND:
            raise LedgerError("CLOSED is by definition ENVELOPE-BOUND")
        if entry.reversal_point is None:
            raise LedgerError("CLOSED requires the reversal point of the blocking parameter")
    if entry.transferability is Transferability.UNIVERSAL and entry.reversal_point is not None:
        raise LedgerError("a UNIVERSAL verdict has no reversal point")


def _validate_live(entry: LedgerEntry) -> None:
    if entry.blocking_axis is None:
        raise LedgerError(f"{entry.verdict.value} requires a blocking axis")
    if entry.axis_of_death:
        raise LedgerError("a live hypothesis carries no axis of death")
    if entry.transferability is not None or entry.reversal_point is not None:
        raise LedgerError("a live hypothesis is not classified for transferability")


def _validate_characterisation(entry: LedgerEntry) -> None:
    if entry.axis_of_death or entry.blocking_axis is not None:
        raise LedgerError(f"{entry.verdict.value} carries no axis; it is not a hypothesis")
    if entry.transferability is not None or entry.reversal_point is not None:
        raise LedgerError(f"{entry.verdict.value} is not classified for transferability")


def _validate_withdrawal(entry: LedgerEntry) -> None:
    if not entry.retracts or not entry.retracts.strip():
        raise LedgerError("WITHDRAWN must name the entry it retracts")
    if entry.retracts == entry.entry_id:
        raise LedgerError("an entry cannot retract itself")
    if not entry.note.strip():
        raise LedgerError("a retraction states why")
    if entry.axis_of_death or entry.blocking_axis is not None or entry.transferability is not None:
        raise LedgerError("a retraction carries no axis or classification of its own")
