"""What each agent is handed: figures code computed from one snapshot, and nothing else.

An agent that adds up bars itself will eventually add them up wrong, and nobody will know which
call it happened on. So the arithmetic is here, in one place, with tests, and the model receives
the result. That also keeps the prompt short, which is most of what a token bill is made of.

Every brief says where its numbers came from and how old they are, because an agent that cannot
see the age of a price cannot be careful with it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading_research_agents.agents import macro_analyst, onchain_analyst, sentiment_analyst
from trading_research_agents.agents.macro_analyst.tools import macro_brief, returns_from_closes
from trading_research_agents.agents.onchain_analyst.tools import onchain_brief
from trading_research_agents.agents.sentiment_analyst.tools import sentiment_brief
from trading_research_agents.agents.technical_analyst.tools import Features, features
from trading_research_agents.data.schema import Candle, Direction
from trading_research_agents.data.series import Event, Series
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


def reference_returns(snapshot: Snapshot) -> tuple[list[int], list[float]]:
    """The instrument's own closes from this snapshot, for correlations measured on the operator's
    data rather than on a vendor's claim about it."""
    if "ohlcv" not in snapshot.values:
        return [], []
    candles = list(snapshot.get("ohlcv").value)
    times = [c.start_ns for c in candles]
    closes = [float(c.close) for c in candles]
    return times, returns_from_closes(times, closes)


def context_brief(
    role: str,
    snapshot: Snapshot,
    *,
    series: Sequence[Series] = (),
    events: Sequence[Event] = (),
    at_ns: int | None = None,
) -> dict[str, Any]:
    """The brief for one of the reading analysts: macro and news, on-chain, or sentiment.

    All three read series the operator mapped in, through the same engine and the same as-of rule,
    so a value is never used before the moment it was published. A role with nothing declared still
    gets a brief, and the brief says there was nothing: that is a state an operator must be able to
    see, not a gap to fill with a comfortable number.
    """
    moment = at_ns if at_ns is not None else _snapshot_time(snapshot)
    times, returns = reference_returns(snapshot)
    if role == macro_analyst.ROLE:
        brief = macro_brief(series, at_ns=moment, times_ns=times, returns=returns, events=events)
    elif role == onchain_analyst.ROLE:
        brief = onchain_brief(series, at_ns=moment, times_ns=times, returns=returns)
    elif role == sentiment_analyst.ROLE:
        brief = sentiment_brief(series, at_ns=moment, times_ns=times, returns=returns, notes=events)
    else:
        raise BriefError(f"{role} is not a reading analyst")
    brief["instrument"] = snapshot.instrument
    brief["declared_series"] = [s.name for s in series]
    return brief


# What a declared threshold means for each reading role's own vocabulary. The operator declares
# bullish and bearish; the role answers in its own words, and these are the same statement.
DECLARED_AS = {
    macro_analyst.ROLE: {"bullish": "risk-on", "bearish": "risk-off"},
    onchain_analyst.ROLE: {"bullish": "accumulation", "bearish": "distribution"},
    sentiment_analyst.ROLE: {"bullish": "positive", "bearish": "negative"},
}


def declared_state(role: str, brief: Mapping[str, Any]) -> dict[str, Any]:
    """What the operator's own thresholds say, before any model reads the same figures.

    This is the half of the answer that does not depend on a model: each series in the brief carries
    the state of the operator's declared threshold, and when they all point the same way that is the
    declared state. It is recorded beside what the model said, so an operator can see the two agree
    or not, and so the gate that comes next can rest on the declaration rather than on a sentence.
    """
    states = [
        str(row.get("state"))
        for row in brief.get("series", [])
        if row.get("basis") == "operator-declared threshold"
    ]
    named = sorted({s for s in states if s in ("bullish", "bearish")})
    if not named:
        return {"declared": "undeclared", "series_declared": len(states)}
    if len(named) > 1:
        return {"declared": "mixed", "states": named, "series_declared": len(states)}
    return {
        "declared": named[0],
        "as_role_says_it": DECLARED_AS.get(role, {}).get(named[0]),
        "series_declared": len(states),
    }


def _snapshot_time(snapshot: Snapshot) -> int:
    """The moment a cycle is deciding at: the newest market time any value in it carries."""
    if not snapshot.values:
        raise BriefError(f"{snapshot.instrument}: the snapshot is empty")
    return max(f.as_of_ns for f in snapshot.values.values())


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
