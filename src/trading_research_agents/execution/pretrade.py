"""What a trade costs before it exists, with each term labelled by how much we actually know.

The realised identity in `cost.py` answers a question about the past: this trade happened, what
did it cost. A cycle needs the question answered the other way round, before an order exists,
and then the honest answer has four terms with three different standings. The fee is declared by
the venue. The spread is measured on the book we are holding. The impact is arithmetic on that
book. The latency slippage is an estimate the operator wrote down, and calling it anything else
would be a lie that eventually costs money.

So every component carries its own certainty, and the total is never described as real. The gate
is a comparison: a claimed edge must clear the total plus the margin the operator declared.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from trading_research_agents.data.schema import Direction, Side
from trading_research_agents.data.sources.base import OrderBook
from trading_research_agents.envelope.model import Channel
from trading_research_agents.execution.book import BookError, Level, half_spread_bps, walk_the_book
from trading_research_agents.execution.cost import BPS, NS_PER_S

QUANTUM = Decimal("0.0001")


class Certainty(StrEnum):
    """How a number came to be. The order is from strongest to weakest on purpose."""

    DECLARED = "declared"
    MEASURED = "measured"
    COMPUTED = "computed"
    ESTIMATED = "estimated"


class PreTradeError(ValueError):
    """The cost cannot be assembled from what is on hand. Never a silent zero."""


@dataclass(frozen=True)
class CostComponent:
    name: str
    bps: Decimal
    certainty: Certainty
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.bps, Decimal) or not self.bps.is_finite():
            raise PreTradeError(f"{self.name} must be a finite Decimal, got {self.bps!r}")

    def record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bps": str(self.bps.quantize(QUANTUM)),
            "certainty": self.certainty.value,
            "source": self.source,
        }


@dataclass(frozen=True)
class PreTradeCost:
    """The components and their sum. Nothing here is called real."""

    components: tuple[CostComponent, ...]
    size: Decimal
    mid: Decimal

    def __post_init__(self) -> None:
        if not self.components:
            raise PreTradeError("a cost with no components is not a cost")

    @property
    def round_trip_bps(self) -> Decimal:
        return sum((c.bps for c in self.components), Decimal(0))

    @property
    def estimated_bps(self) -> Decimal:
        """How much of the total rests on an estimate rather than on a measurement."""
        return sum(
            (c.bps for c in self.components if c.certainty is Certainty.ESTIMATED), Decimal(0)
        )

    def record(self) -> dict[str, Any]:
        return {
            "round_trip_bps": str(self.round_trip_bps.quantize(QUANTUM)),
            "estimated_bps": str(self.estimated_bps.quantize(QUANTUM)),
            "size": str(self.size),
            "mid": str(self.mid),
            "components": [c.record() for c in self.components],
        }


@dataclass(frozen=True)
class PreTradeVerdict:
    """What the gate decided, and the arithmetic it decided on."""

    admitted: bool
    edge_bps: Decimal
    cost: PreTradeCost
    margin_bps: Decimal
    reason: str
    edge_certainty: Certainty = Certainty.DECLARED
    edge_source: str = "operator declaration"

    @property
    def net_bps(self) -> Decimal:
        return self.edge_bps - self.cost.round_trip_bps

    def record(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "edge_bps": str(self.edge_bps.quantize(QUANTUM)),
            "edge_certainty": self.edge_certainty.value,
            "edge_source": self.edge_source,
            "net_bps": str(self.net_bps.quantize(QUANTUM)),
            "margin_bps": str(self.margin_bps.quantize(QUANTUM)),
            "reason": self.reason,
            "cost": self.cost.record(),
        }


def _levels(book: OrderBook, side: Side) -> list[Level]:
    source = book.asks if side is Side.BUY else book.bids
    return [Level(price=level.price, qty=level.size) for level in source]


def impact_bps(book: OrderBook, size: Decimal, side: Side) -> Decimal:
    """What walking the visible book at this size costs against the mid, in basis points."""
    try:
        walk = walk_the_book(_levels(book, side), size, side, book.mid)
    except BookError as exc:
        raise PreTradeError(f"impact at size {size}: {exc}") from exc
    # The half spread is charged separately, so the impact is what is left beyond the touch.
    touch = half_spread_bps(book.bids[0].price, book.asks[0].price)
    return max(walk.slippage_bps - touch, Decimal(0))


def settlements_in(horizon_s: int, interval_s: int) -> int:
    """How many funding settlements a position of this horizon can be charged for."""
    if horizon_s <= 0 or interval_s <= 0:
        raise PreTradeError("horizon and settlement interval are positive")
    # A horizon shorter than one interval can still span a settlement, so round up: the cost of
    # being wrong about this is paying a charge the report said would not come.
    return -(-horizon_s // interval_s)


def interval_seconds(text: str) -> int | None:
    """A venue's settlement interval as ccxt writes it ("8h", "4h", "1h") to seconds.

    None for anything else, "0h" included: a dated contract settles no funding, and an interval
    this cannot read is left to the declared one rather than guessed at.
    """
    raw = text.strip().lower()
    units = {"h": 3600, "m": 60}
    if len(raw) < 2 or raw[-1] not in units or not raw[:-1].isdigit():
        return None
    seconds = int(raw[:-1]) * units[raw[-1]]
    return seconds or None


def pre_trade_cost(
    *,
    channel: Channel,
    book: OrderBook,
    size: Decimal,
    direction: Direction,
    horizon_s: int,
    funding_rate: Decimal | None = None,
    settlement_interval_s: int = 28_800,
    latency_slippage_bps: Decimal = Decimal(0),
    interval_source: str = "[run].settlement_interval_s",
) -> PreTradeCost:
    """Assemble the cost of a round trip that has not happened yet, component by component."""
    if not isinstance(size, Decimal) or size <= 0:
        raise PreTradeError("size is a positive Decimal")
    if not isinstance(latency_slippage_bps, Decimal) or latency_slippage_bps < 0:
        raise PreTradeError("latency_slippage_bps is a non-negative Decimal")
    entry_side = Side.BUY if direction is Direction.LONG else Side.SELL
    touch = half_spread_bps(book.bids[0].price, book.asks[0].price)
    entry_impact = impact_bps(book, size, entry_side)

    components = [
        CostComponent(
            name="fee_round_trip",
            bps=channel.entry_fee_bps + channel.exit_fee_bps,
            certainty=Certainty.DECLARED,
            source=f"envelope channel {channel.key}"
            + (f", its {channel.market} fees" if channel.market else ""),
        ),
        CostComponent(
            name="spread_cost",
            bps=touch * 2,
            certainty=Certainty.MEASURED,
            source="half spread of this snapshot's book, both legs",
        ),
        CostComponent(
            name="impact",
            bps=entry_impact * 2,
            certainty=Certainty.COMPUTED,
            source=(
                f"walking {len(_levels(book, entry_side))} visible levels at size {size}; "
                "the exit leg is assumed symmetric"
            ),
        ),
    ]
    if channel.gas_quote is not None and channel.gas_quote > 0:
        notional = size * book.mid
        if notional <= 0:
            raise PreTradeError("a notional of zero cannot carry a gas cost")
        components.append(
            CostComponent(
                name="gas",
                bps=(channel.gas_quote / notional) * BPS,
                certainty=Certainty.DECLARED,
                source=(
                    f"{channel.gas_quote} of quote currency per round trip on {channel.key}, "
                    "declared by the operator"
                ),
            )
        )
    if funding_rate is not None:
        if not isinstance(funding_rate, Decimal) or not funding_rate.is_finite():
            raise PreTradeError("funding_rate is a finite Decimal or None")
        charges = settlements_in(horizon_s, settlement_interval_s)
        signed = Decimal(direction.sign) * funding_rate * BPS * charges
        source = (
            f"{charges} settlement(s) of {settlement_interval_s}s ({interval_source}) inside a "
            f"{horizon_s}s horizon, charged at the rate that stands now"
        )
        if signed < 0:
            # The rate is the one standing now and it can turn before the settlement comes, so a
            # payment the position would receive is never counted against what it pays.
            source += f"; a receipt of {(-signed).quantize(QUANTUM)} bps is not counted"
        components.append(
            CostComponent(
                name="funding_accrual",
                bps=max(signed, Decimal(0)),
                certainty=Certainty.DECLARED,
                source=source,
            )
        )
    if latency_slippage_bps:
        components.append(
            CostComponent(
                name="latency_slippage",
                bps=latency_slippage_bps * 2,
                certainty=Certainty.ESTIMATED,
                source="operator declaration in [run].slippage_bps, both legs",
            )
        )
    return PreTradeCost(components=tuple(components), size=size, mid=book.mid)


def admit(
    *,
    edge_bps: Decimal,
    cost: PreTradeCost,
    margin_bps: Decimal = Decimal(0),
    edge_certainty: Certainty = Certainty.DECLARED,
    edge_source: str = "operator declaration",
) -> PreTradeVerdict:
    """The gate: a claimed edge clears the cost plus the operator's margin, or it does not.

    Nothing is negotiated here. A claim below the bar produces a verdict with the arithmetic in
    it, and the cycle records that verdict instead of an order.
    """
    if not isinstance(edge_bps, Decimal) or not edge_bps.is_finite():
        raise PreTradeError("edge_bps is a finite Decimal")
    if not isinstance(margin_bps, Decimal) or margin_bps < 0:
        raise PreTradeError("margin_bps is a non-negative Decimal")
    total = cost.round_trip_bps
    required = total + margin_bps
    if edge_bps >= required:
        reason = (
            f"{edge_bps.quantize(QUANTUM)} bps claimed clears {total.quantize(QUANTUM)} bps cost "
            f"plus {margin_bps.quantize(QUANTUM)} bps margin"
        )
        return PreTradeVerdict(
            True, edge_bps, cost, margin_bps, reason, edge_certainty, edge_source
        )
    reason = (
        f"{edge_bps.quantize(QUANTUM)} bps claimed is below {required.quantize(QUANTUM)} bps "
        f"required ({total.quantize(QUANTUM)} bps cost plus {margin_bps.quantize(QUANTUM)} bps "
        "margin)"
    )
    return PreTradeVerdict(False, edge_bps, cost, margin_bps, reason, edge_certainty, edge_source)


def horizon_seconds(bars: int, bar_interval_s: int) -> int:
    """A horizon in bars to seconds, for the funding charge a position can meet."""
    if bars <= 0 or bar_interval_s <= 0:
        raise PreTradeError("bars and bar_interval_s are positive")
    return bars * bar_interval_s


def book_from_levels(bids: Sequence[tuple[str, str]], asks: Sequence[tuple[str, str]]) -> OrderBook:
    """A book from decimal strings, for tests and for reading one back from a snapshot file."""
    from trading_research_agents.data.sources.base import BookLevel

    return OrderBook(
        bids=tuple(BookLevel(Decimal(p), Decimal(q)) for p, q in bids),
        asks=tuple(BookLevel(Decimal(p), Decimal(q)) for p, q in asks),
    )


__all__ = [
    "BPS",
    "NS_PER_S",
    "Certainty",
    "CostComponent",
    "PreTradeCost",
    "PreTradeError",
    "PreTradeVerdict",
    "admit",
    "book_from_levels",
    "horizon_seconds",
    "impact_bps",
    "interval_seconds",
    "pre_trade_cost",
    "settlements_in",
]
