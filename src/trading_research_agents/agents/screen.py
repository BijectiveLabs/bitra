"""One pass over the instruments a desk watches, before any of them costs a roster of models.

A cycle is expensive and an instrument that has not moved is not worth one. So a pass takes the
cheap look first: one data pull per instrument, the same `[trigger]` the operator already declared
applied to each, and an order that puts the instrument with the most movement against its own spread
at the front. What comes out is one or two instruments the cycle then runs in full, and a record of
every instrument that was looked at and why it was not chosen.

The screener role may narrow that list and say why, from the same figures. It may not add an
instrument, reorder the run, or say anything about a direction or a size: by the time it is asked,
code has already decided which instruments are admissible and in what order.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from trading_research_agents.agents import market_screener
from trading_research_agents.agents.briefs import bars_of
from trading_research_agents.agents.cited import figures_of as figures_of_row
from trading_research_agents.agents.cited import survived
from trading_research_agents.agents.progress import (
    DONE,
    FAILED,
    HELD,
    RUNNING,
    Watch,
    announce,
    silent,
)
from trading_research_agents.agents.roles import RoleError, prompt
from trading_research_agents.agents.scope import brief_for, dropped
from trading_research_agents.agents.technical_analyst.tools import features
from trading_research_agents.data.sources.chain import SourceChain
from trading_research_agents.execution.desk import Desk
from trading_research_agents.execution.regime import RegimeRule, label
from trading_research_agents.execution.targets import intents_today
from trading_research_agents.execution.trigger import Trigger, last_seen, spread_of, worth_asking
from trading_research_agents.execution.universe import Candidate, Screened, Universe, order, take
from trading_research_agents.llm.completer import Completer
from trading_research_agents.records import COMPACT
from trading_research_agents.store.snapshot import Snapshot, cycle_id, take_snapshot

BPS = Decimal(10_000)
# A correlation on ten bars is a number with no claim behind it.
MIN_CORRELATION_POINTS = 20
# The screener answers the same schema in a session and in a cycle, but it is given different
# figures and asked a different question, so the doctrine is per brief rather than per role.
LIVE_PROMPT = "market_screener_live"
# What the look costs: the book to price the spread, the bars to see the window's range. Funding and
# open interest are a cycle's business, not a screen's.
KINDS = ("orderbook", "ohlcv")
# What every other instrument's correlation is measured against.
REFERENCE = "BTCUSDT"


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
    """What code can say about an instrument from the bars of one look, for the screener."""
    try:
        candles = list(snapshot.get("ohlcv").value or ())
    except KeyError:
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
    wake = worth_asking(
        trigger,
        mid=figures["mid"],
        spread_bps=figures["spread_bps"],
        seen=last_seen(root, instrument=instrument),
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
    )


def sweep(
    universe: Universe,
    chains: Mapping[str, SourceChain],
    *,
    trigger: Trigger,
    root: Path,
    now: datetime,
    max_round_trips_per_day: int | None = None,
    options: Mapping[str, Mapping[str, Any]] | None = None,
    ask: Callable[[list[Candidate]], tuple[list[Candidate], list[str]]] | None = None,
    desk: Desk | None = None,
    held: Callable[[], tuple[Any, str]] | None = None,
    watch: Watch = silent,
    open_now: Collection[str] = (),
    full: str = "",
    listed: Mapping[str, Mapping[str, Any]] | None = None,
    rule: RegimeRule | None = None,
) -> Screened:
    """Look at every instrument, keep what the trigger admits, and take the top of the order."""
    out = Screened(at=cycle_id(now))
    if universe.idle:
        return out
    moves: dict[str, list[float]] = {}
    out.candidates = []
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

    if ask is not None and any(c.admitted for c in out.candidates):
        out.asked_screener = True
        announce(watch, "market_screener", RUNNING)
        try:
            narrowed, said = ask([c for c in out.candidates if c.admitted])
        except Exception as exc:
            announce(watch, "market_screener", FAILED, exc.__class__.__name__)
            raise
        announce(watch, "market_screener", DONE, f"kept {len(narrowed)}")
        out.refusals.extend(said)
        answered = {c.instrument: c for c in narrowed}
        out.candidates = [
            answered.get(c.instrument, c) if c.admitted else c for c in out.candidates
        ]
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
    anchor = moves.get(REFERENCE)
    if not anchor:
        return candidates
    out = []
    for candidate in candidates:
        if candidate.instrument == REFERENCE:
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


def screener_brief(candidates: list[Candidate], *, mechanism: str, at: str) -> dict[str, Any]:
    """What the screener is given: the figures code computed, and the mechanism it screens for.

    A figure that does not apply is left out rather than sent as null. On the first look at an
    instrument there is no previous mid, so there is no move and no pressure - and a null in a brief
    reads as a measurement that failed, which is a different thing and one a reader will hold
    against the instrument.
    """
    rows: list[dict[str, Any]] = []
    for c in candidates:
        row: dict[str, Any] = {"instrument": c.instrument, "first_look": c.moved_bps is None}
        for name, value in (
            ("spread_bps", c.spread_bps),
            ("range_bps", c.range_bps),
            ("moved_bps", c.moved_bps),
            ("pressure", c.pressure),
        ):
            if value is not None:
                row[name] = str(value)
        if c.age_s is not None:
            row["age_s"] = c.age_s
        row.update(c.features)
        rows.append(row)
    return {
        "as_of": at,
        "mechanism": mechanism,
        "basis": (
            "every figure here was computed by code from one snapshot per instrument and the "
            "venue's 24-hour ticker; where first_look is true this desk has not looked at that "
            "instrument before, so it has no move and no pressure to report and neither is missing"
        ),
        "candidates": rows,
    }


def choose(
    candidates: list[Candidate],
    said: market_screener.LiveScreen,
    brief: Mapping[str, Any],
) -> tuple[list[Candidate], list[str]]:
    """Apply the screener's order. A pick must be an admitted instrument, cite at least one figure
    from that instrument's own row that holds, and appear once; anything else is refused by name.
    An instrument it does not pick is not taken this pass."""
    rows = {str(r.get("instrument")): r for r in brief.get("candidates") or []}
    known = {c.instrument for c in candidates}
    refusals: list[str] = []
    ranked: dict[str, tuple[int, str, str]] = {}
    for pick in said.picks:
        name = pick.instrument
        if name not in known:
            refusals.append(f"market_screener: picked an instrument it was not given: {name!r}")
            continue
        if name in ranked:
            refusals.append(f"market_screener: picked {name} twice; the first stands")
            continue
        kept, lost = survived(pick.figures_used, figures_of_row(rows.get(name, {})))
        for gone in lost:
            refusals.append(f"market_screener: {name}: discarded a citation, {gone}")
        if not kept:
            refusals.append(
                f"market_screener: {name} picked on no figure from its own row; not taken"
            )
            continue
        ranked[name] = (len(ranked) + 1, pick.setup, pick.reason)
    dropped_by = {
        str(row.get("instrument")): str(row.get("reason") or "excluded")
        for row in said.excluded
        if isinstance(row, dict)
    }
    out: list[Candidate] = []
    for candidate in candidates:
        name = candidate.instrument
        if name in ranked:
            rank, setup, reason = ranked[name]
            out.append(
                replace(
                    candidate,
                    kept_by_screener=True,
                    pick_rank=rank,
                    setup=setup,
                    screener_said=f"#{rank} {setup}: {reason}",
                )
            )
            continue
        why = dropped_by.get(name, "not picked by the screener")
        refusals.append(f"market_screener: {name} not picked, {why}")
        out.append(replace(candidate, kept_by_screener=False, screener_said=why))
    return out, refusals


def asking(
    completer: Completer, *, mechanism: str, at: str
) -> Callable[[list[Candidate]], tuple[list[Candidate], list[str]]]:
    """The screener as one call, with its own scope filter and its own failure kept local."""

    def ask(candidates: list[Candidate]) -> tuple[list[Candidate], list[str]]:
        brief = screener_brief(candidates, mechanism=mechanism, at=at)
        extra = dropped(market_screener.ROLE, brief)
        refusals = (
            [f"market_screener: not this role's to read, removed: {list(extra)}"] if extra else []
        )
        try:
            asked = brief_for(market_screener.ROLE, brief)
            done = completer.complete(
                system=prompt(LIVE_PROMPT),
                user=json.dumps(asked, sort_keys=True, default=str, separators=COMPACT),
                schema=dict(market_screener.LIVE_SCHEMA),
            )
            said = market_screener.parse_live(done.text)
        except (RoleError, ValueError, RuntimeError, OSError, TimeoutError) as exc:
            # A screener that cannot answer does not stop the pass: code's own order stands.
            return candidates, [
                *refusals,
                f"market_screener: {exc.__class__.__name__}: {exc}; code's order stands",
            ]
        chosen, said_about = choose(candidates, said, asked)
        return chosen, [*refusals, *said_about]

    return ask
