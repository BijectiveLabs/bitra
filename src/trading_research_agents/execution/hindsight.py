"""Hindsight: what the trades each gate refused would have done, next to the ones it let through.

A gate earns its place when what it refuses would have done worse than what it admits. Every
directional decision in a desk's record is replayed on the candles that followed it, with the
levels the record holds and the desk's hold limit, and grouped by the gate that stopped it. The
same replay on the opposite side asks the other question: whether the deciding model's side is
worth more than its reverse.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from trading_research_agents.data.schema import Candle

ADMITTED = "admitted"
BPS = Decimal(10_000)
NS_PER_S = 1_000_000_000
# Fewer decisions than this on either side of a comparison, or fewer days for the side check, and
# the report says so instead of calling a winner.
MIN_EACH = 10
MIN_DAYS = 5
CLEAR = 0.05
RESAMPLES = 4000
SEED = 7

Fetch = Callable[[str, int, int], Sequence[Candle]]


class HindsightError(ValueError):
    """A record or a price path that cannot be replayed."""


@dataclass(frozen=True)
class Decision:
    cycle: str
    instrument: str
    at: datetime
    side: int
    gate: str
    stop_bps: Decimal | None
    take_bps: Decimal | None
    round_trip_bps: Decimal | None

    @property
    def day(self) -> str:
        return self.at.date().isoformat()


@dataclass(frozen=True)
class Outcome:
    decision: Decision
    gross_bps: Decimal
    ended: str
    opposite_gross_bps: Decimal
    round_trip_bps: Decimal

    @property
    def net_bps(self) -> Decimal:
        return self.gross_bps - self.round_trip_bps

    @property
    def opposite_net_bps(self) -> Decimal:
        return self.opposite_gross_bps - self.round_trip_bps


def gate_of(record: Mapping[str, Any]) -> str:
    """The gate that stopped a cycle, or ADMITTED when it wrote an intent the venue took."""
    placed = record.get("placed")
    if record.get("intent") and (not placed or placed.get("sent")):
        return ADMITTED
    said = str(record.get("stopped_by") or "")
    if not said:
        refusals = record.get("refusals") or []
        said = str(refusals[-1]) if refusals else ""
    head = said.split(":", 1)[0].strip()
    head = head.split(" at ", 1)[0]
    if head.startswith("validator"):
        return "validators"
    if head == "cost gate":
        return "cost"
    return head or "no reason recorded"


def _number(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def _levels(record: Mapping[str, Any]) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Mid, stop and take as the record holds them: the sent intent, else the exits, else the
    analyst's own named levels."""
    intent = record.get("intent") or {}
    exits = record.get("exits") or {}
    analyst = record.get("analyst") or {}
    seen = record.get("seen") or {}
    mid = next(
        (m for m in map(_number, (intent.get("mid"), exits.get("entry"), seen.get("mid"))) if m),
        None,
    )
    for stop, take in (
        (intent.get("stop_price"), intent.get("take_price")),
        (exits.get("stop_price"), exits.get("take_price")),
        (analyst.get("stop_price"), analyst.get("target_price")),
    ):
        if _number(stop) and _number(take):
            return mid, _number(stop), _number(take)
    return mid, None, None


def decision_of(
    record: Mapping[str, Any], cost: Mapping[str, Any] | None = None
) -> Decision | None:
    """One cycle as a decision to replay, or None when it took no side."""
    analyst = record.get("analyst") or {}
    side = {"long": 1, "short": -1}.get(str(analyst.get("target")))
    finished = record.get("finished_at")
    if side is None or not finished or record.get("held"):
        return None
    mid, stop, take = _levels(record)
    stop_bps = take_bps = None
    if mid and stop and take:
        stop_bps = abs(mid - stop) / mid * BPS
        take_bps = abs(take - mid) / mid * BPS
        if (take - mid) * side <= 0 or (mid - stop) * side <= 0:
            stop_bps = take_bps = None
    verdict = record.get("cost") or {}
    priced = (verdict.get("cost") or {}).get("round_trip_bps") or (cost or {}).get("round_trip_bps")
    return Decision(
        cycle=str(record.get("cycle_id")),
        instrument=str(record.get("instrument")),
        at=datetime.fromisoformat(str(finished)).astimezone(UTC),
        side=side,
        gate=gate_of(record),
        stop_bps=stop_bps,
        take_bps=take_bps,
        round_trip_bps=_number(priced),
    )


def read_decisions(cycles: Path, *, since: datetime | None = None) -> list[Decision]:
    """Every directional decision under `cycles`, oldest first."""
    found = []
    for path in sorted(cycles.glob("*/cycle.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            brief = path.parent / "briefs" / "cost.json"
            cost = json.loads(brief.read_text(encoding="utf-8")) if brief.exists() else None
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        try:
            decision = decision_of(record, cost if isinstance(cost, dict) else None)
        except (ValueError, TypeError):
            continue
        if decision is not None and (since is None or decision.at >= since):
            found.append(decision)
    return sorted(found, key=lambda d: d.at)


def replay(
    decision: Decision, candles: Sequence[Candle], *, hold_s: int, bar_s: int, side: int = 0
) -> tuple[Decimal, str] | None:
    """Gross bps from the first bar after the decision to the stop, the take or the hold limit.

    A bar that reaches both levels counts as the stop: inside one bar the order is unknown, and
    the replay takes the worse reading. None when the candles do not cover the whole hold."""
    side = side or decision.side
    start = int(decision.at.timestamp()) * NS_PER_S
    bar_ns = bar_s * NS_PER_S
    path = [c for c in candles if c.start_ns >= start]
    if not path or path[0].start_ns - start > bar_ns:
        return None
    end = path[0].start_ns + hold_s * NS_PER_S
    window = [c for c in path if c.start_ns < end]
    if window[-1].start_ns + bar_ns < end:
        return None
    entry = window[0].open
    for candle in window:
        worst = candle.low if side > 0 else candle.high
        best = candle.high if side > 0 else candle.low
        against = (entry - worst) / entry * BPS * side
        towards = (best - entry) / entry * BPS * side
        if decision.stop_bps is not None and against >= decision.stop_bps:
            return -decision.stop_bps, "stop"
        if decision.take_bps is not None and towards >= decision.take_bps:
            return decision.take_bps, "take"
    return (window[-1].close - entry) / entry * BPS * side, "hold"


def price_path(
    fetch: Fetch, decision: Decision, *, hold_s: int, bar_s: int, page: int = 100
) -> list[Candle]:
    """The bars from the decision to the end of its hold, fetched a page at a time."""
    since_ms = int(decision.at.timestamp()) * 1000
    end_ms = since_ms + (hold_s + bar_s) * 1000
    bars: dict[int, Candle] = {}
    cursor = since_ms
    while cursor < end_ms:
        got = [c for c in fetch(decision.instrument, cursor, page) if c.start_ns >= cursor * 10**6]
        if not got:
            break
        for candle in got:
            bars[candle.start_ns] = candle
        cursor = got[-1].start_ns // 10**6 + bar_s * 1000
    return [bars[k] for k in sorted(bars)]


def _candle_row(c: Candle) -> list[str]:
    return [str(c.start_ns), str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume)]


def _candle(row: Sequence[str]) -> Candle:
    start, o, h, low, c, v = row
    return Candle(
        start_ns=int(start),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal(v),
    )


def _kept(stored: Path | None, hold_s: int, bar_s: int) -> list[Candle]:
    """A path kept by an earlier run for the same hold and bar; anything else is fetched again."""
    if stored is None or not stored.exists():
        return []
    try:
        saved = json.loads(stored.read_text(encoding="utf-8"))
        if saved.get("hold_s") != hold_s or saved.get("bar_s") != bar_s:
            return []
        return [_candle(row) for row in saved["candles"]]
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        return []


def outcomes(
    decisions: Sequence[Decision],
    fetch: Fetch,
    *,
    hold_s: int,
    bar_s: int,
    cache: Path | None = None,
    now: datetime | None = None,
) -> tuple[list[Outcome], dict[str, int]]:
    """Replay what can be replayed; count what could not, and why."""
    now = now or datetime.now(UTC)
    done: list[Outcome] = []
    skipped = {"hold not over": 0, "no candles": 0}
    known = [d.round_trip_bps for d in decisions if d.round_trip_bps is not None]
    for decision in decisions:
        if (now - decision.at).total_seconds() < hold_s + bar_s:
            skipped["hold not over"] += 1
            continue
        stored = None if cache is None else cache / f"{Path(decision.cycle).name}.json"
        candles = _kept(stored, hold_s, bar_s)
        fetched = not candles
        if fetched:
            try:
                candles = price_path(fetch, decision, hold_s=hold_s, bar_s=bar_s)
            except (OSError, RuntimeError, ValueError):
                candles = []
        mine = replay(decision, candles, hold_s=hold_s, bar_s=bar_s)
        other = replay(decision, candles, hold_s=hold_s, bar_s=bar_s, side=-decision.side)
        if mine is None or other is None:
            skipped["no candles"] += 1
            continue
        if stored is not None and fetched:
            stored.parent.mkdir(parents=True, exist_ok=True)
            stored.write_text(
                json.dumps(
                    {"hold_s": hold_s, "bar_s": bar_s, "candles": [_candle_row(c) for c in candles]}
                ),
                encoding="utf-8",
            )
        cost = decision.round_trip_bps
        if cost is None:
            cost = statistics.median(known) if known else Decimal(0)
        done.append(Outcome(decision, mine[0], mine[1], other[0], cost))
    return done, skipped


def _bps(value: float) -> str:
    return f"{value:.1f}"


def _summary(nets: Sequence[float]) -> dict[str, Any]:
    return {
        "decisions": len(nets),
        "mean_net_bps": _bps(statistics.mean(nets)) if nets else None,
        "median_net_bps": _bps(statistics.median(nets)) if nets else None,
        "won": f"{sum(1 for x in nets if x > 0) / len(nets):.2f}" if nets else None,
        "sum_net_bps": _bps(sum(nets)),
    }


def admitted_better(admitted: Sequence[float], refused: Sequence[float], seed: int = SEED) -> float:
    """One-sided permutation p: how often a random split of the same decisions makes the admitted
    group at least this much better than the refused one."""
    first = np.asarray(admitted, dtype=float)
    pool = np.concatenate([first, np.asarray(refused, dtype=float)])
    observed = first.mean() - pool[first.size :].mean()
    rng = np.random.default_rng(seed)
    hits = 0
    for _ in range(RESAMPLES):
        drawn = rng.permutation(pool)
        # A split that ties the observed one counts as at least as good, never as worse.
        if drawn[: first.size].mean() - drawn[first.size :].mean() >= observed - 1e-9:
            hits += 1
    return hits / RESAMPLES


def _verdict(admitted: Sequence[float], refused: Sequence[float]) -> tuple[str, str | None]:
    if len(admitted) < MIN_EACH or len(refused) < MIN_EACH:
        return "too few to tell", None
    p = admitted_better(admitted, refused)
    if p <= CLEAR:
        return "refused worse trades than it let through", f"{p:.3f}"
    if p >= 1 - CLEAR:
        return "refused better trades than it let through; review it", f"{p:.3f}"
    return "no clear difference yet", f"{p:.3f}"


def _independent(found: Sequence[Outcome], hold_s: int) -> int:
    """Decisions left once those on an instrument inside an earlier one's hold are dropped."""
    last: dict[str, datetime] = {}
    count = 0
    for o in sorted(found, key=lambda o: o.decision.at):
        before = last.get(o.decision.instrument)
        if before is None or (o.decision.at - before).total_seconds() >= hold_s:
            last[o.decision.instrument] = o.decision.at
            count += 1
    return count


def side_check(found: Sequence[Outcome], seed: int = SEED) -> dict[str, Any]:
    """The model's side against its reverse, resampled by whole days: one day's tape moves every
    decision on it together, so a day is the unit that is independent."""
    by_day: dict[str, list[float]] = {}
    for o in found:
        by_day.setdefault(o.decision.day, []).append(float(o.net_bps - o.opposite_net_bps))
    diffs = [x for xs in by_day.values() for x in xs]
    result: dict[str, Any] = {
        "days": len(by_day),
        "model_side": _summary([float(o.net_bps) for o in found]),
        "opposite_side": _summary([float(o.opposite_net_bps) for o in found]),
    }
    if len(by_day) < MIN_DAYS or len(diffs) < MIN_EACH:
        result["verdict"] = "too few to tell"
        return result
    rng = np.random.default_rng(seed)
    days = [np.asarray(xs, dtype=float) for xs in by_day.values()]
    means = sorted(
        float(np.concatenate([days[i] for i in rng.integers(0, len(days), len(days))]).mean())
        for _ in range(RESAMPLES)
    )
    low, high = means[int(0.025 * RESAMPLES)], means[int(0.975 * RESAMPLES) - 1]
    result["difference_bps"] = _bps(statistics.mean(diffs))
    result["interval_bps"] = [_bps(low), _bps(high)]
    if low > 0:
        result["verdict"] = "the model's side did better than its reverse"
    elif high < 0:
        result["verdict"] = "the reverse of the model's side did better; its calls carried no edge"
    else:
        result["verdict"] = "no clear difference yet"
    return result


def report(
    found: Sequence[Outcome], *, hold_s: int, bar_s: int, skipped: Mapping[str, int]
) -> dict[str, Any]:
    """Each gate's refusals beside what it admitted, the side check, and how much there is."""
    groups: dict[str, list[float]] = {}
    ended: dict[str, int] = {}
    for o in found:
        groups.setdefault(o.decision.gate, []).append(float(o.net_bps))
        ended[o.ended] = ended.get(o.ended, 0) + 1
    admitted = groups.get(ADMITTED, [])
    gates = []
    for gate, nets in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if gate == ADMITTED:
            continue
        verdict, p = _verdict(admitted, nets)
        gates.append({"gate": gate, **_summary(nets), "verdict": verdict, "p_admitted_better": p})
    refused = [x for gate, nets in groups.items() if gate != ADMITTED for x in nets]
    overall, p_overall = _verdict(admitted, refused)
    return {
        "basis": (
            "each decision replayed from the first bar after it, on its own stop and take where "
            "the record holds them, closed at the hold limit otherwise, net of the round trip the "
            "cycle priced; a bar that reaches both levels counts as the stop"
        ),
        "hold_s": hold_s,
        "bar_s": bar_s,
        "replayed": len(found),
        "independent": _independent(found, hold_s),
        "days": len({o.decision.day for o in found}),
        "skipped": dict(skipped),
        "ended": ended,
        ADMITTED: _summary(admitted),
        "gates": gates,
        "all_refused": {**_summary(refused), "verdict": overall, "p_admitted_better": p_overall},
        "side": side_check(found),
    }
