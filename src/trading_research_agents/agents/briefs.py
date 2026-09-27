"""What each agent is handed: figures code computed from one snapshot, and nothing else.

An agent that adds up bars itself will eventually add them up wrong, and nobody will know which
call it happened on. So the arithmetic is here, in one place, with tests, and the model receives
the result. That also keeps the prompt short, which is most of what a token bill is made of.

Every brief says where its numbers came from and how old they are, because an agent that cannot
see the age of a price cannot be careful with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading_research_agents.agents.technical_analyst.tools import Features, features
from trading_research_agents.data.schema import Candle, Direction
from trading_research_agents.data.sources.base import OrderBook
from trading_research_agents.envelope.model import Channel
from trading_research_agents.execution.pretrade import (
    PreTradeCost,
    PreTradeError,
    horizon_seconds,
    pre_trade_cost,
)
from trading_research_agents.store.snapshot import Snapshot
from trading_research_agents.strategy.core import Bar

NS_PER_S = 1_000_000_000


class BriefError(ValueError):
    """The snapshot does not carry what this brief needs. The caller decides what that means."""


def bars_of(candles: list[Candle], interval_s: int) -> list[Bar]:
    """Candles to the runtime's bars, so one set of feature code serves replay and live alike."""
    interval_ns = interval_s * NS_PER_S
    out = []
    for candle in candles:
        out.append(
            Bar(
                start_ns=candle.start_ns,
                end_ns=candle.start_ns + interval_ns,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                trades=max(candle.trades, 1),
            )
        )
    return out


@dataclass(frozen=True)
class DataBrief:
    """What this cycle is working from: every value, its source, its tier and its age."""

    instrument: str
    values: dict[str, dict[str, Any]]
    refused: dict[str, str]
    stale: list[str]

    def record(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "values": self.values,
            "refused": self.refused,
            "stale": self.stale,
        }


def data_brief(snapshot: Snapshot, *, now: float | None = None) -> DataBrief:
    record = snapshot.record(now)
    return DataBrief(
        instrument=snapshot.instrument,
        values={
            kind: {
                "source": value["source"],
                "tier": value["tier"],
                "age_s": value["age_s"],
            }
            for kind, value in record["values"].items()
        },
        refused=dict(record["refused"]),
        stale=list(record["stale"]),
    )


@dataclass(frozen=True)
class TechnicalBrief:
    """The figures the technical analyst decides from, and the market state around them."""

    instrument: str
    interval_s: int
    bars: int
    features: Features
    spread_bps: str | None
    funding_rate: str | None
    age_s: float
    mechanism: str

    def record(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "interval_s": self.interval_s,
            "bars": self.bars,
            "mechanism": self.mechanism,
            "age_s": self.age_s,
            "spread_bps": self.spread_bps,
            "funding_rate": self.funding_rate,
            **self.features.record(),
        }


def technical_brief(
    snapshot: Snapshot,
    *,
    mechanism: str,
    interval_s: int = 60,
    lookback_bars: int = 60,
    now: float | None = None,
) -> TechnicalBrief:
    """Features from the closed candles in the snapshot, with the spread and funding beside them."""
    if lookback_bars < 2:
        raise BriefError("lookback_bars is at least 2")
    try:
        fetched = snapshot.get("ohlcv")
    except KeyError as exc:
        raise BriefError(str(exc)) from exc
    candles = list(fetched.value)
    if len(candles) < 2:
        raise BriefError(f"{snapshot.instrument}: fewer than two candles in the snapshot")
    window = bars_of(candles[-lookback_bars:], interval_s)
    computed = features(window)
    if computed is None:
        raise BriefError(f"{snapshot.instrument}: the candles carry no usable features")
    spread = None
    if "orderbook" in snapshot.values:
        book = snapshot.get("orderbook").value
        spread = str(book.spread_bps.quantize(Decimal("0.01")))
    funding = None
    if "funding" in snapshot.values:
        funding = str(snapshot.get("funding").value)
    return TechnicalBrief(
        instrument=snapshot.instrument,
        interval_s=interval_s,
        bars=len(window),
        features=computed,
        spread_bps=spread,
        funding_rate=funding,
        age_s=fetched.age_s(now),
        mechanism=mechanism,
    )


@dataclass(frozen=True)
class CostBrief:
    """What a round trip would cost right now, per component, at the declared size."""

    instrument: str
    direction: str
    size: str
    cost: PreTradeCost

    def record(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "direction": self.direction,
            "size": self.size,
            **self.cost.record(),
        }


def cost_brief(
    snapshot: Snapshot,
    *,
    channel: Channel,
    size: Decimal,
    direction: Direction,
    horizon_bars: int,
    bar_interval_s: int = 60,
    settlement_interval_s: int = 28_800,
    latency_slippage_bps: Decimal = Decimal(0),
) -> CostBrief:
    """The pre-trade cost from this snapshot's book, refused outright when there is no book."""
    if "orderbook" not in snapshot.values:
        raise BriefError(
            f"{snapshot.instrument}: no book in the snapshot, so no cost can be priced; "
            f"{snapshot.refused.get('orderbook', 'the chain did not run')}"
        )
    book = snapshot.get("orderbook").value
    if not isinstance(book, OrderBook):
        raise BriefError(f"{snapshot.instrument}: the book in the snapshot is not a book")
    funding = snapshot.get("funding").value if "funding" in snapshot.values else None
    try:
        cost = pre_trade_cost(
            channel=channel,
            book=book,
            size=size,
            direction=direction,
            horizon_s=horizon_seconds(horizon_bars, bar_interval_s),
            funding_rate=funding,
            settlement_interval_s=settlement_interval_s,
            latency_slippage_bps=latency_slippage_bps,
        )
    except PreTradeError as exc:
        raise BriefError(f"{snapshot.instrument}: {exc}") from exc
    return CostBrief(
        instrument=snapshot.instrument,
        direction=direction.value,
        size=str(size),
        cost=cost,
    )
