from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal


class EnvelopeError(ValueError):
    """Raised when an envelope is incomplete or contradicts itself."""


# Market types a channel can carry fees of its own for, besides the base (perpetual) fees.
MARKET_FEES = ("spot",)


@dataclass(frozen=True)
class Channel:
    """One way of getting in and out of a position on one venue.

    Fees are per side, in basis points, as quoted by the venue for the declared tier. The
    measured round trip, when present, is what execution actually cost end to end on this
    channel; it is normally higher than the quote, and the difference is spread, slippage and
    the fills that never happened.
    """

    venue: str
    name: str
    entry_fee_bps: Decimal
    exit_fee_bps: Decimal
    measured_round_trip_bps: Decimal | None = None
    # A chain's own cost of a round trip, in quote currency, for venues where settlement is paid
    # rather than netted out of the fee. Priced as a component like any other.
    gas_quote: Decimal | None = None
    # Fees for another market type on the same venue, as (market, entry, exit): a spot pair is
    # usually charged more than the perpetual on the same coin, and one channel prices one fee.
    market_fees: tuple[tuple[str, Decimal, Decimal], ...] = ()
    # Which market's fees this channel carries, once `for_market` has chosen; empty is the base.
    market: str = ""

    def __post_init__(self) -> None:
        if not self.venue.strip() or not self.name.strip():
            raise EnvelopeError("a channel needs a venue and a name")
        for label, value in (
            ("entry_fee_bps", self.entry_fee_bps),
            ("exit_fee_bps", self.exit_fee_bps),
        ):
            if not isinstance(value, Decimal):
                raise EnvelopeError(f"{label} must be a Decimal, not {type(value).__name__}")
            if value < 0:
                raise EnvelopeError(f"{label} cannot be negative")
        if self.measured_round_trip_bps is not None:
            if not isinstance(self.measured_round_trip_bps, Decimal):
                raise EnvelopeError("measured_round_trip_bps must be a Decimal")
            if self.measured_round_trip_bps < 0:
                raise EnvelopeError("measured_round_trip_bps cannot be negative")
        if self.gas_quote is not None and (
            not isinstance(self.gas_quote, Decimal) or self.gas_quote < 0
        ):
            raise EnvelopeError("gas_quote must be a non-negative Decimal")
        for market, entry, exit_ in self.market_fees:
            if market not in MARKET_FEES:
                raise EnvelopeError(
                    f"[channel.{market}] is not a market type with fees of its own; "
                    f"have {list(MARKET_FEES)}"
                )
            if not all(isinstance(v, Decimal) and v >= 0 for v in (entry, exit_)):
                raise EnvelopeError(f"[channel.{market}] fees are non-negative numbers")

    def for_market(self, market: str) -> Channel:
        """This channel priced for `market`: its own fees when declared, the base ones otherwise."""
        for name, entry, exit_ in self.market_fees:
            if name == market:
                # The measured round trip was measured on the base market, so it does not carry.
                return replace(
                    self,
                    entry_fee_bps=entry,
                    exit_fee_bps=exit_,
                    measured_round_trip_bps=None,
                    market_fees=(),
                    market=name,
                )
        return self

    @property
    def key(self) -> str:
        return f"{self.venue}/{self.name}"

    @property
    def roles(self) -> tuple[str, str]:
        """How this channel gets in and out: maker or taker per side, from its own name.

        `maker-taker` is an entry that rests on the book and an exit that crosses it, and that is
        the difference between the two fees beside it. A name that does not say is `undeclared`,
        and code that has to know refuses rather than guessing on the cheap side.
        """
        halves = self.name.lower().split("-")
        if len(halves) == 2 and all(half in ("maker", "taker") for half in halves):
            return halves[0], halves[1]
        return "undeclared", "undeclared"

    @property
    def entry_role(self) -> str:
        return self.roles[0]

    @property
    def exit_role(self) -> str:
        return self.roles[1]

    @property
    def round_trip_bps(self) -> Decimal:
        return self.entry_fee_bps + self.exit_fee_bps


@dataclass(frozen=True)
class Envelope:
    """The declared set of channels, plus the latency band the operator actually runs at."""

    channels: tuple[Channel, ...]
    order_latency_ms: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.channels:
            raise EnvelopeError("an envelope declares at least one channel")
        keys = [c.key for c in self.channels]
        if len(set(keys)) != len(keys):
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            raise EnvelopeError(f"duplicate channel: {', '.join(dupes)}")
        if self.order_latency_ms is not None and self.order_latency_ms < 0:
            raise EnvelopeError("order_latency_ms cannot be negative")

    def venues(self) -> tuple[str, ...]:
        seen: list[str] = []
        for c in self.channels:
            if c.venue not in seen:
                seen.append(c.venue)
        return tuple(seen)

    def cheapest(self, venue: str | None = None) -> Channel:
        """The channel with the lowest quoted round trip, optionally restricted to one venue."""
        pool = [c for c in self.channels if venue is None or c.venue == venue]
        if not pool:
            raise EnvelopeError(f"no channel declared for venue {venue!r}")
        return min(pool, key=lambda c: c.round_trip_bps)

    def snapshot(self) -> tuple[tuple[str, str], ...]:
        """A flat, string-only view for the ledger, so a verdict carries its envelope with it.

        Keys are channel-scoped, values are the decimal text, so nothing is lost in JSON.
        """
        pairs: list[tuple[str, str]] = []
        for c in self.channels:
            pairs.append((f"{c.key}/round_trip_bps", str(c.round_trip_bps)))
            if c.measured_round_trip_bps is not None:
                pairs.append((f"{c.key}/measured_round_trip_bps", str(c.measured_round_trip_bps)))
        if self.order_latency_ms is not None:
            pairs.append(("order_latency_ms", str(self.order_latency_ms)))
        return tuple(pairs)
