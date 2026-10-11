"""Where intents go. A simulated venue fills by the execution rules; live venues are plugins.

The simulated venue is the same object for replay, paper and shadow. It fills an entry at the
first print after the measured latency, never at the print that fired the signal, and it prices
the round trip with the cost identity from the operator's envelope. Nothing here places an
order anywhere. A live venue satisfies the same protocol from `trading/venues/`, and the
runtime cannot tell the two apart except by one flag: a live venue routes exits through
itself instead of settling them against the bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from trading_research_agents.data.schema import Direction, Side, Trade
from trading_research_agents.envelope.model import Channel
from trading_research_agents.execution.cost import CostBreakdown, FundingSchedule, realised_cost
from trading_research_agents.execution.fill import BPS


class VenueError(RuntimeError):
    """The venue cannot act on the intent as given."""


@dataclass(frozen=True)
class Intent:
    """What the runtime asks the venue to do, after the controls admitted it."""

    ts_ns: int
    instrument: str
    direction: Direction
    size: Decimal
    reason: str


@dataclass(frozen=True)
class Fill:
    ts_ns: int
    price: Decimal
    size: Decimal
    direction: Direction


class Venue(Protocol):
    @property
    def label(self) -> str: ...

    @property
    def routes_exits(self) -> bool:
        """True when a stop or take-profit must be sent as an order rather than settled."""
        ...

    @property
    def pending(self) -> Intent | None: ...

    def observe(self, trade: Trade) -> None: ...

    def submit(self, intent: Intent) -> Fill | None: ...

    def poll(self, trade: Trade) -> Fill | None: ...

    def cost_of(
        self, direction: Direction, entry_ts_ns: int, exit_ts_ns: int, spread_cost_bps: Decimal
    ) -> CostBreakdown: ...


class SimulatedVenue:
    """Fills against the print stream by the execution rules. Sends nothing anywhere."""

    label = "simulated"
    routes_exits = False

    def __init__(
        self,
        *,
        channel: Channel,
        funding: FundingSchedule,
        latency_ns: int,
        slippage_bps: Decimal,
    ) -> None:
        if latency_ns < 0:
            raise VenueError("latency cannot be negative")
        if not isinstance(slippage_bps, Decimal) or slippage_bps < 0:
            raise VenueError("slippage_bps must be a non-negative Decimal")
        self.channel = channel
        self.funding = funding
        self.latency_ns = latency_ns
        self.slippage_bps = slippage_bps
        self._pending: Intent | None = None
        self._last: Trade | None = None

    def observe(self, trade: Trade) -> None:
        self._last = trade

    def submit(self, intent: Intent) -> Fill | None:
        """Queue the intent; it fills on the first later print at or after the latency."""
        if self._pending is not None:
            raise VenueError("an intent is already pending; one order at a time")
        self._pending = intent
        return None

    def poll(self, trade: Trade) -> Fill | None:
        """Called by the runtime on every print. Marks the market and fills a pending intent."""
        self._last = trade
        if self._pending is None:
            return None
        if trade.ts_ns < self._pending.ts_ns + self.latency_ns:
            return None
        intent, self._pending = self._pending, None
        side = Side.BUY if intent.direction is Direction.LONG else Side.SELL
        # Crossing the spread pays slippage on the adverse side of the print that arrived.
        factor = self.slippage_bps / BPS
        price = trade.price * (1 + factor) if side is Side.BUY else trade.price * (1 - factor)
        return Fill(ts_ns=trade.ts_ns, price=price, size=intent.size, direction=intent.direction)

    def cost_of(
        self, direction: Direction, entry_ts_ns: int, exit_ts_ns: int, spread_cost_bps: Decimal
    ) -> CostBreakdown:
        return realised_cost(
            channel=self.channel,
            direction=direction,
            entry_ts_ns=entry_ts_ns,
            exit_ts_ns=exit_ts_ns,
            spread_cost_bps=spread_cost_bps,
            slippage_bps=self.slippage_bps,
            funding=self.funding,
        )

    @property
    def pending(self) -> Intent | None:
        return self._pending
