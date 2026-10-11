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
from trading_research_agents.data.sources.base import Fetched, OrderBook
from trading_research_agents.envelope.model import Channel
from trading_research_agents.execution.pretrade import (
    PreTradeCost,
    PreTradeError,
    horizon_seconds,
    pre_trade_cost,
)
from trading_research_agents.execution.regime import RegimeRule, label
from trading_research_agents.store.snapshot import Snapshot
from trading_research_agents.strategy.core import Bar

NS_PER_S = 1_000_000_000
# Depth beyond the first levels is mostly orders nobody expects to fill; the imbalance that moves a
# short-horizon price sits near the touch.
IMBALANCE_LEVELS = 10


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
    regime: str = "mixed"
    book_imbalance: str | None = None
    timeframes: Mapping[str, Mapping[str, Any]] | None = None
    open_interest: Mapping[str, Any] | None = None
    readings: Mapping[str, Mapping[str, Any]] | None = None

    def record(self) -> dict[str, Any]:
        out = {
            "instrument": self.instrument,
            "interval_s": self.interval_s,
            "bars": self.bars,
            "mechanism": self.mechanism,
            "age_s": self.age_s,
            "spread_bps": self.spread_bps,
            "funding_rate": self.funding_rate,
            **self.features.record(),
            "regime": self.regime,
        }
        if self.book_imbalance is not None:
            out["book_imbalance"] = self.book_imbalance
        if self.timeframes:
            out["timeframes"] = {k: dict(v) for k, v in self.timeframes.items()}
        if self.open_interest:
            out["open_interest"] = dict(self.open_interest)
        if self.readings:
            out["readings"] = {k: dict(v) for k, v in self.readings.items()}
        return out


def technical_brief(
    snapshot: Snapshot,
    *,
    mechanism: str,
    interval_s: int = 60,
    lookback_bars: int = 60,
    now: float | None = None,
    rule: RegimeRule | None = None,
    context: Mapping[int, Fetched] | None = None,
    previous_open_interest: Mapping[str, Any] | None = None,
    readings: Mapping[str, Mapping[str, Any]] | None = None,
) -> TechnicalBrief:
    """Features from the closed candles in the snapshot, with the market state around them.

    `context` is the higher timeframes' candles, `previous_open_interest` the open interest the last
    snapshot of this instrument recorded, and `readings` what the operator's thresholds say on each
    reading analyst's series. All of it is optional; a missing piece is left out, never estimated.
    """
    rule = rule or RegimeRule()
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
    imbalance = None
    if "orderbook" in snapshot.values:
        book = snapshot.get("orderbook").value
        spread = str(book.spread_bps.quantize(Decimal("0.01")))
        imbalance = book_imbalance(book)
    funding = None
    if "funding" in snapshot.values:
        funding = str(snapshot.get("funding").value)
    frames = {
        interval_name(seconds): frame
        for seconds, candles in sorted((context or {}).items())
        if (frame := timeframe_figures(candles, seconds, rule, lookback_bars, now)) is not None
    }
    held = None
    if "open_interest" in snapshot.values and previous_open_interest:
        held = open_interest_change(snapshot.get("open_interest"), previous_open_interest)
    return TechnicalBrief(
        instrument=snapshot.instrument,
        interval_s=interval_s,
        bars=len(window),
        features=computed,
        spread_bps=spread,
        funding_rate=funding,
        age_s=fetched.age_s(now),
        mechanism=mechanism,
        regime=label(computed.trend_strength, computed.window_move_bps, rule),
        book_imbalance=imbalance,
        timeframes=frames or None,
        open_interest=held,
        readings=dict(readings) if readings else None,
    )


def interval_name(seconds: int) -> str:
    for size, unit in ((86_400, "d"), (3_600, "h"), (60, "m")):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def timeframe_figures(
    fetched: Fetched, seconds: int, rule: RegimeRule, lookback_bars: int, now: float | None
) -> dict[str, Any] | None:
    """One higher timeframe, reduced to where it is going and where the close sits in it."""
    candles = list(fetched.value)[-lookback_bars:]
    computed = features(bars_of(candles, seconds)) if len(candles) >= 2 else None
    if computed is None:
        return None
    shown = computed.record()
    return {
        "bars": computed.bars,
        "window_move_bps": shown["window_move_bps"],
        "zscore": shown["zscore"],
        "position_in_range": shown["position_in_range"],
        "trend_strength": shown["trend_strength"],
        "regime": label(computed.trend_strength, computed.window_move_bps, rule),
        "age_s": fetched.age_s(now),
    }


def book_imbalance(book: OrderBook, levels: int = IMBALANCE_LEVELS) -> str | None:
    """Resting bids against resting asks near the touch, in [-1, 1]; positive leans to the bid."""
    bids = sum((level.size for level in book.bids[:levels]), Decimal(0))
    asks = sum((level.size for level in book.asks[:levels]), Decimal(0))
    total = bids + asks
    if total <= 0:
        return None
    return str(((bids - asks) / total).quantize(Decimal("0.001")))


def open_interest_change(current: Fetched, previous: Mapping[str, Any]) -> dict[str, Any] | None:
    """The change since the last snapshot that recorded it, on the same field, or nothing.

    A venue that answered the same bucket twice has said nothing new, so that is left out rather
    than reported as a flat zero.
    """
    try:
        then_ns = int(previous.get("as_of") or 0)
    except (TypeError, ValueError):
        return None
    if then_ns <= 0 or current.as_of_ns <= then_ns:
        return None
    for name in ("amount", "value"):
        now_raw, then_raw = current.detail.get(name), previous.get(name)
        if now_raw is None or then_raw is None:
            continue
        try:
            before, after = Decimal(str(then_raw)), Decimal(str(now_raw))
        except ArithmeticError:
            # A record on disk is read back, not trusted; one that does not parse says nothing.
            return None
        if not before.is_finite() or not after.is_finite() or before <= 0:
            return None
        return {
            "change_pct": str(((after / before - 1) * 100).quantize(Decimal("0.01"))),
            "since_s": round((current.as_of_ns - then_ns) / NS_PER_S),
            "measured_in": "contracts" if name == "amount" else "quote",
        }
    return None


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
    _scope_rows(brief, series, snapshot.instrument)
    return brief


QUOTE_SUFFIXES = ("USDT", "USDC", "BUSD", "USD", "PERP")


def base_of(instrument: str) -> str:
    """The asset an instrument is on: BTCUSDT is BTC. A name with no known quote is its own base."""
    name = instrument.upper()
    if "/" in name:
        return name.partition("/")[0]
    for separator in ("-", "_"):
        if separator in name:
            return name.partition(separator)[0]
    for quote in QUOTE_SUFFIXES:
        if name.endswith(quote) and len(name) > len(quote):
            return name[: -len(quote)]
    return name


def _scope_rows(brief: dict[str, Any], series: Sequence[Series], instrument: str) -> None:
    """Mark each row with the asset it is about and how much it counts for this instrument."""
    specs = {s.spec.name: s.spec for s in series}
    here = base_of(instrument)
    for row in brief.get("series") or []:
        spec = specs.get(str(row.get("series")))
        if spec is None or not spec.asset:
            continue
        row["asset"] = spec.asset
        if spec.asset != here:
            row["counts"] = str(spec.elsewhere)


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
    rows = [
        row for row in brief.get("series", []) if row.get("basis") == "operator-declared threshold"
    ]
    states = [str(row.get("state")) for row in rows]
    named = sorted({s for s in states if s in ("bullish", "bearish")})
    score = _net_strength(rows)
    graded = bool(rows) and all(bool(row.get("graded")) for row in rows)
    if not named:
        return {"declared": "undeclared", "series_declared": len(states), "score": "0"}
    if len(named) > 1:
        # The gate still reads `mixed` and fails closed on it; conviction weighs the net.
        return {
            "declared": "mixed",
            "states": named,
            "series_declared": len(states),
            "score": str(score),
            "graded": graded,
        }
    return {
        "declared": named[0],
        "as_role_says_it": DECLARED_AS.get(role, {}).get(named[0]),
        "series_declared": len(states),
        "score": str(score),
        "graded": graded,
    }


def _net_strength(rows: Sequence[Mapping[str, Any]]) -> Decimal:
    """The mean signed strength of a role's declared series, between -1 and 1.

    A row written before strengths existed carries only its word, and is read as the full +1 or
    -1 it always meant. A series whose value is unknown says nothing and is left out.
    """
    found: list[Decimal] = []
    for row in rows:
        state = str(row.get("state"))
        if state not in ("bullish", "bearish", "neutral"):
            continue
        raw = row.get("strength")
        if raw is not None:
            value = Decimal(str(raw))
        else:
            value = {"bullish": Decimal(1), "bearish": Decimal(-1)}.get(state, Decimal(0))
        # A series about another asset is divided over the same count but weighed less, so a desk
        # reading only another asset's chain hears a quieter voice, not the same one.
        found.append(value * Decimal(str(row.get("counts", 1))))
    if not found:
        return Decimal(0)
    return (sum(found, Decimal(0)) / len(found)).quantize(Decimal("0.001"))


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
