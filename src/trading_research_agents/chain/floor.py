"""Layer 0: the cost floor.

Before a hypothesis is written, two numbers are put side by side: the plausible magnitude of the
signal and the round trip of the cheapest channel available. If the first is smaller than the
second there is nothing to measure, and the idea stops here for the price of a subtraction.

This is the cheapest test in the chain and the one most often skipped. A signal worth 3.0 bps
with a permutation p-value of zero is still dead against a 9.0 bps round trip.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trading_research_agents.envelope.model import Channel, Envelope
from trading_research_agents.ledger.entry import LedgerEntry, ReversalPoint
from trading_research_agents.ledger.vocabulary import Axis, Transferability, Verdict

LAYER = 0


class FloorOutcome(StrEnum):
    CLEARED = "cleared"
    STOP = "stop"


@dataclass(frozen=True)
class FloorCheck:
    """The two numbers, the channel they were compared on, and what follows."""

    plausible_magnitude_bps: Decimal
    channel: Channel
    outcome: FloorOutcome
    margin_bps: Decimal
    measured_margin_bps: Decimal | None

    @property
    def stopped(self) -> bool:
        return self.outcome is FloorOutcome.STOP

    @property
    def clears_measured_cost(self) -> bool | None:
        """Whether the signal also clears what the channel actually cost, when that was measured.

        A pass on the quoted schedule that fails here is a warning, not a verdict: the quote is
        the declared floor, and a layer-0 stop is only ever issued against a declared parameter.
        """
        if self.measured_margin_bps is None:
            return None
        return self.measured_margin_bps > 0

    def to_entry(
        self,
        *,
        entry_id: str,
        subject: str,
        recorded_at: datetime,
        envelope: Envelope | None = None,
        evidence: tuple[str, ...] = (),
    ) -> LedgerEntry:
        """A CLOSED row on the cost axis. Only a stop is recorded; a pass is not a verdict."""
        if not self.stopped:
            raise ValueError("a hypothesis that clears the cost floor has nothing to record yet")
        c = self.channel
        note = (
            f"{self.plausible_magnitude_bps} bps plausible against a {c.round_trip_bps} bps "
            f"round trip on {c.key}"
        )
        if self.measured_round_trip_bps is not None:
            note += f" ({self.measured_round_trip_bps} bps measured)"
        return LedgerEntry(
            entry_id=entry_id,
            subject=subject,
            verdict=Verdict.CLOSED,
            recorded_at=recorded_at,
            layer=LAYER,
            axis_of_death=(Axis.COST,),
            transferability=Transferability.ENVELOPE_BOUND,
            # The verdict reverses for an operator whose round trip is below the signal.
            reversal_point=ReversalPoint(
                parameter="c_rt", value=self.plausible_magnitude_bps, unit="bps", comparison="<"
            ),
            evidence=evidence,
            envelope=envelope.snapshot() if envelope is not None else (),
            note=note,
        )

    @property
    def measured_round_trip_bps(self) -> Decimal | None:
        return self.channel.measured_round_trip_bps


def cost_floor(
    envelope: Envelope, plausible_magnitude_bps: Decimal, *, venue: str | None = None
) -> FloorCheck:
    """Compare a plausible signal magnitude with the cheapest declared round trip.

    The magnitude is a bound, not a measurement. Where no bound can be given the hypothesis is
    not yet specific enough to test, and that is a layer-0 result of its own; it is refused here
    rather than guessed.
    """
    if not isinstance(plausible_magnitude_bps, Decimal):
        raise TypeError("plausible_magnitude_bps must be a Decimal; costs are never floats")
    if plausible_magnitude_bps <= 0:
        raise ValueError("bound the plausible magnitude first; a non-positive bound is not a test")

    channel = envelope.cheapest(venue)
    margin = plausible_magnitude_bps - channel.round_trip_bps
    measured_margin = None
    if channel.measured_round_trip_bps is not None:
        measured_margin = plausible_magnitude_bps - channel.measured_round_trip_bps

    stopped = plausible_magnitude_bps < channel.round_trip_bps
    outcome = FloorOutcome.STOP if stopped else FloorOutcome.CLEARED
    return FloorCheck(
        plausible_magnitude_bps=plausible_magnitude_bps,
        channel=channel,
        outcome=outcome,
        margin_bps=margin,
        measured_margin_bps=measured_margin,
    )
