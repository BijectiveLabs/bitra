"""One pass over the instruments a desk watches, before any of them costs a roster of models.

A cycle is expensive and an instrument that has not moved is not worth one. So a pass takes the
cheap look first: one data pull per instrument, the same `[trigger]` the operator already declared
applied to each, and an order that puts the instrument with the most movement against its own spread
at the front. What comes out is one or two instruments the cycle then runs in full, and a record of
every instrument that was looked at and why it was not chosen.

No model is asked here. Code decides which instruments are admissible and in what order: the room
the instrument's own paths draw over the hold against its spread first, then the move already seen,
then the order the operator wrote.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from trading_research_agents.agents.briefs import bars_of, base_of
from trading_research_agents.agents.progress import (
    DONE,
    HELD,
    RUNNING,
    Watch,
    announce,
    silent,
)
from trading_research_agents.agents.structure_lead.tools import features
from trading_research_agents.data.sources.chain import SourceChain
from trading_research_agents.execution.desk import Desk
from trading_research_agents.execution.regime import RegimeRule, label
from trading_research_agents.execution.targets import intents_today
from trading_research_agents.execution.trigger import (
    Trigger,
    last_seen,
    previous_looks,
    spread_of,
    worth_asking,
)
from trading_research_agents.execution.universe import Candidate, Screened, Universe, order, take
from trading_research_agents.forecast.paths import PathError, PathSettings, read_paths
from trading_research_agents.store.snapshot import Snapshot, cycle_id, take_snapshot

BPS = Decimal(10_000)
# A correlation on ten bars is a number with no claim behind it.
MIN_CORRELATION_POINTS = 20
# What the look costs: the book to price the spread, the bars to see the window's range. Funding and
# open interest are a cycle's business, not a screen's.
KINDS = ("orderbook", "ohlcv")
# The hold the room ahead is read over, and how many paths read it: a cheap look, not a cycle.
HOLD_BARS = 24
SCREEN_PATHS = 300
# What every other instrument's correlation is measured against: Bitcoin, whatever the venue
# calls it or settles it in.
REFERENCE_BASE = "BTC"


def figures_of(snapshot: Snapshot, *, now: float | None = None) -> dict[str, Any]:
    """The free figures of one instrument: the touch, the window's range, and how old they are."""
    bid = ask = None
    age: float | None = None
    try:
        book = snapshot.get("orderbook")
    except KeyError:
        book = None
    if book is not None:
        bids, asks = getattr(book.value, "bids", ()), getattr(book.value, "asks", ())
        if bids and asks:
            bid, ask = Decimal(str(bids[0].price)), Decimal(str(asks[0].price))
        age = book.age_s(now)
    mid, spread = spread_of(bid, ask)

    band: Decimal | None = None
    try:
        candles = snapshot.get("ohlcv")
    except KeyError:
        candles = None
    if candles is not None:
        rows = list(candles.value or ())
        age = max(age or 0.0, candles.age_s(now))
        if rows and mid is None:
            mid = Decimal(str(rows[-1].close))
        if rows and mid and mid > 0:
            high = max(Decimal(str(r.high)) for r in rows)
            low = min(Decimal(str(r.low)) for r in rows)
            band = ((high - low) / mid * BPS).quantize(Decimal("0.01"))
    return {"mid": mid, "spread_bps": spread, "range_bps": band, "age_s": age}


def returns_of(snapshot: Snapshot) -> list[float]:
    """Bar-to-bar returns of the window, for comparing one instrument's moves with another's."""
    try:
        rows = list(snapshot.get("ohlcv").value or ())
    except KeyError:
        return []
    closes = [float(r.close) for r in rows if float(r.close) > 0]
    return [(b - a) / a for a, b in pairwise(closes) if a > 0]


def correlation(left: Sequence[float], right: Sequence[float]) -> Decimal | None:
    """Pearson correlation over the overlap, or nothing when there is not enough of one.

    Plain arithmetic on the bars both instruments already carried into this pass, so it costs
    nothing and says where it came from. Under twenty points it says nothing at all, because a
    correlation on ten bars is a number with no claim behind it.
    """
    n = min(len(left), len(right))
    if n < MIN_CORRELATION_POINTS:
        return None
    a, b = list(left)[-n:], list(right)[-n:]
    mean_a, mean_b = sum(a) / n, sum(b) / n
    top = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    left_var = sum((x - mean_a) ** 2 for x in a)
    right_var = sum((y - mean_b) ** 2 for y in b)
    if left_var <= 0 or right_var <= 0:
        return None
    return Decimal(str(top / (left_var * right_var) ** 0.5)).quantize(Decimal("0.001"))


def measured(snapshot: Snapshot, interval_s: int, rule: RegimeRule) -> dict[str, Any]:
    """What code can say about an instrument from the bars of one look, kept in the screen."""
    try:
        candles = list(snapshot.get("ohlcv").value or ())
    except KeyError:
        return {}
    if any(candle.volume <= 0 for candle in candles):
        # The bar features weigh by volume; a venue that publishes prices without one (a
        # prediction outcome) is screened on its move alone.
        return {}
    computed = features(bars_of(candles, interval_s)) if len(candles) >= 2 else None
    if computed is None:
        return {}
    shown = computed.record()
    out = {
        name: shown[name]
        for name in (
            "window_move_bps",
            "zscore",
            "trend_strength",
            "volume_ratio",
            "position_in_range",
        )
        if shown.get(name) is not None
    }
    out["regime"] = label(computed.trend_strength, computed.window_move_bps, rule)
    return out


def look(
    instrument: str,
    chains: Mapping[str, SourceChain],
    *,
    trigger: Trigger,
    root: Path,
    now: datetime,
    max_round_trips_per_day: int | None = None,
    options: Mapping[str, Mapping[str, Any]] | None = None,
    moves: dict[str, list[float]] | None = None,
    listed: Mapping[str, Any] | None = None,
    rule: RegimeRule | None = None,
    looks: Mapping[str, tuple[Decimal, datetime]] | None = None,
    horizon_bars: int = HOLD_BARS,
) -> Candidate:
    """One instrument, looked at for the price of a data pull and no tokens at all."""
    snapshot = take_snapshot(instrument, chains, kinds=KINDS, options=options)
    figures = figures_of(snapshot)
    interval = int((options or {}).get("ohlcv", {}).get("interval_s", 60))
    found = measured(snapshot, interval, rule or RegimeRule())
    for name in ("quote_volume_24h", "change_24h_pct"):
        if listed and listed.get(name) is not None:
            found[name] = listed[name]
    if moves is not None:
        moves[instrument] = returns_of(snapshot)
    reasons = [f"data {kind}: {why}" for kind, why in sorted(snapshot.refused.items())]
    seen = last_seen(root, instrument=instrument, min_move_bps=trigger.min_move_bps)
    earlier = (looks or {}).get(instrument)
    if earlier is not None and (seen.at is None or earlier[1] > seen.at):
        seen = replace(seen, mid=earlier[0], at=earlier[1])
    wake = worth_asking(
        trigger,
        mid=figures["mid"],
        spread_bps=figures["spread_bps"],
        seen=seen,
        now=now,
        round_trips_today=intents_today(root, at=now, instrument=instrument),
        max_round_trips_per_day=max_round_trips_per_day,
    )
    if figures["mid"] is None:
        reasons.append("no two-sided book, so nothing here can be priced")
    return Candidate(
        instrument=instrument,
        admitted=wake.ask and figures["mid"] is not None,
        mid=figures["mid"],
        spread_bps=figures["spread_bps"],
        moved_bps=wake.moved_bps,
        range_bps=figures["range_bps"],
        age_s=figures["age_s"],
        reasons=(*reasons, *wake.reasons),
        features=found,
        paths_range_bps=paths_range(snapshot, horizon_bars),
    )


def paths_range(snapshot: Snapshot, horizon_bars: int) -> Decimal | None:
    """The range the instrument's own paths draw over the hold, or nothing when they cannot."""
    try:
        candles = list(snapshot.get("ohlcv").value or [])
        ohlc = [[float(c.open), float(c.high), float(c.low), float(c.close)] for c in candles]
        read = read_paths(
            ohlc,
            side="long",
            take_bps=None,
            stop_bps=None,
            horizon_bars=horizon_bars,
            settings=PathSettings(paths=SCREEN_PATHS),
        )
    except (KeyError, TypeError, AttributeError, PathError):
        return None
    return Decimal(f"{read.range_bps:.2f}")


def sweep(
    universe: Universe,
    chains: Mapping[str, SourceChain],
    *,
    trigger: Trigger,
    root: Path,
    now: datetime,
    max_round_trips_per_day: int | None = None,
    options: Mapping[str, Mapping[str, Any]] | None = None,
    desk: Desk | None = None,
    held: Callable[[], tuple[Any, str]] | None = None,
    watch: Watch = silent,
    open_now: Collection[str] = (),
    full: str = "",
    listed: Mapping[str, Mapping[str, Any]] | None = None,
    rule: RegimeRule | None = None,
    screens: Path | None = None,
) -> Screened:
    """Look at every instrument, keep what the trigger admits, and take the top of the order."""
    out = Screened(at=cycle_id(now))
    if universe.idle:
        return out
    moves: dict[str, list[float]] = {}
    out.candidates = []
    looks = {} if screens is None else previous_looks(screens)
    for instrument in universe.instruments:
        announce(watch, instrument, RUNNING, "one data pull")
        candidate = look(
            instrument,
            chains,
            trigger=trigger,
            root=root,
            now=now,
            max_round_trips_per_day=max_round_trips_per_day,
            options=options,
            moves=moves,
            listed=(listed or {}).get(instrument),
            rule=rule,
            looks=looks,
        )
        # The book comes first, whatever the trigger said: an open position or a full desk is the
        # reason nothing can be sent, and "not moved enough" would hide it (seen live 2026-10-05,
        # when two open positions were reported as a quiet market).
        if instrument in open_now:
            # The venue hand would refuse a second entry on it anyway; asking the roster about it
            # first would only spend tokens on a decision that cannot be sent.
            candidate = replace(
                candidate,
                admitted=False,
                reasons=(
                    "a position is open at the venue; the next entry waits for its exits",
                    *candidate.reasons,
                ),
            )
        elif full:
            # The desk is at its declared count; any cycle now would be refused at the desk step
            # after paying the roster, and the calls it spent count against a provider's rate.
            candidate = replace(candidate, admitted=False, reasons=(full, *candidate.reasons))
        out.candidates.append(candidate)
        announce(
            watch,
            instrument,
            DONE if candidate.admitted else HELD,
            "admitted" if candidate.admitted else (candidate.reasons or ("held",))[0],
        )
    if desk is not None and desk.correlation_above is not None and held is not None:
        out.candidates = _uncorrelated(out.candidates, desk, held, moves, out)
    out.candidates = _with_btc(out.candidates, moves)
    out.code_order = tuple(
        c.instrument for c in order(out.candidates, universe.instruments) if c.admitted
    )
    for candidate in out.candidates:
        for reason in candidate.reasons:
            out.refusals.append(f"{candidate.instrument}: {reason}")

    if universe.max_intents_per_day is not None:
        written = intents_today(root, at=now, instrument=None)
        if written >= universe.max_intents_per_day:
            out.refusals.append(
                f"desk: {written} intent(s) written today across every instrument and the "
                f"declared limit is {universe.max_intents_per_day}; nothing found this pass "
                "could be acted on"
            )
            return out

    out.candidates = order(out.candidates, universe.instruments)
    out.chosen = take(universe, out.candidates)
    return out


def _uncorrelated(
    candidates: list[Candidate],
    desk: Desk,
    held: Callable[[], tuple[Any, str]],
    moves: dict[str, list[float]],
    out: Screened,
) -> list[Candidate]:
    """Refuse a candidate that has been moving with something the desk already holds.

    Two names that move together are one trade in two places, and a per-trade limit applied twice
    is not the limit anyone declared. The figure is computed from the bars this pass already pulled,
    so it costs nothing and an operator can see where it came from.
    """
    try:
        open_now, _ = held()
    except Exception as exc:
        out.refusals.append(f"desk: could not read what is open: {exc.__class__.__name__}: {exc}")
        return candidates
    names = [row.instrument for row in open_now if row.instrument in moves]
    if not names:
        return candidates
    above = desk.correlation_above
    if above is None:
        return candidates
    fresh: list[Candidate] = []
    for candidate in candidates:
        if not candidate.admitted or candidate.instrument in names:
            fresh.append(candidate)
            continue
        worst, against = None, ""
        for name in names:
            measured = correlation(moves.get(candidate.instrument, []), moves.get(name, []))
            if measured is not None and (worst is None or abs(measured) > abs(worst)):
                worst, against = measured, name
        if worst is None:
            fresh.append(candidate)
            continue
        if abs(worst) >= above:
            reason = (
                f"has been moving with {against} at {worst} over the window, at or above the "
                f"declared {above}; one trade in two names is not two trades"
            )
            # Recorded once, through the candidate's own reasons, like every other hold.
            fresh.append(
                replace(
                    candidate,
                    admitted=False,
                    correlation=worst,
                    correlated_with=against,
                    reasons=(*candidate.reasons, reason),
                )
            )
            continue
        fresh.append(replace(candidate, correlation=worst, correlated_with=against))
    return fresh


def _with_btc(candidates: list[Candidate], moves: Mapping[str, list[float]]) -> list[Candidate]:
    """Each instrument's correlation with BTC over this pass's bars: how much of its move is the
    market's and how much its own."""
    reference = next((name for name in moves if base_of(name) == REFERENCE_BASE), None)
    anchor = moves.get(reference) if reference else None
    if not anchor:
        return candidates
    out = []
    for candidate in candidates:
        if candidate.instrument == reference:
            out.append(candidate)
            continue
        measured_r = correlation(moves.get(candidate.instrument, []), anchor)
        if measured_r is None:
            out.append(candidate)
            continue
        out.append(
            replace(
                candidate, features={**candidate.features, "correlation_to_btc": str(measured_r)}
            )
        )
    return out
