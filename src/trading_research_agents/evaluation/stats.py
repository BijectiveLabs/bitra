"""What a row did over the window, beside the null, in the figures a desk asks for first.

- independent bets: ideas on one market that do not overlap an earlier one on it;
- mean, median and sum of net bps; hit rate (net above zero); mean R;
- Sharpe of daily net bps (summed by exit day, a day without an exit counting as zero, 365 days);
- the maximum drawdown of cumulative R in exit order; the time in market; ideas per day;
- the 95 % interval of (mean net of the row - mean net of `random`), resampling whole entry days
  4,000 times with seed 7, because one day's tape moves every idea on it together;
- the per-date rank IC of a policy's score against the next bar's return, with its t statistic.

Below 30 independent bets or 10 days the row says "sample too small" instead of a verdict, and a
row whose knowledge cutoff falls after the window's start is "contaminated".
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import numpy as np

from trading_research_agents.evaluation.settle import Settlement, Unsettled
from trading_research_agents.execution.hindsight import spearman
from trading_research_agents.market_data.days import DAY_NS, day_of

MIN_BETS = 30
MIN_DAYS = 10
MIN_IC_DATES = 10
RESAMPLES = 4000
SEED = 7
DAYS_PER_YEAR = 365


@dataclass(frozen=True)
class Window:
    start_ns: int
    end_ns: int
    instruments: int

    @property
    def days(self) -> int:
        return (self.end_ns - self.start_ns) // DAY_NS


def _fmt(value: float, places: int = 2) -> str:
    return f"{value:.{places}f}"


def _opt(value: float | None, places: int) -> str | None:
    return None if value is None else _fmt(value, places)


def row_report(
    name: str,
    settled: Sequence[Settlement],
    unsettled: Sequence[Unsettled],
    window: Window,
    *,
    null: Sequence[Settlement] | None = None,
    ic: Sequence[tuple[str, float, float]] = (),
    contaminated: bool = False,
    null_row: bool = False,
) -> dict[str, Any]:
    nets = [float(s.net_bps) for s in settled]
    rs = [float(r) for s in settled if (r := s.r_multiple) is not None]
    bets = independent_bets(settled)
    against = None if null is None else versus(settled, null)
    return {
        "row": name,
        "ideas": len(settled),
        "unsettled": dict(sorted(Counter(u.reason for u in unsettled).items())),
        "independent_bets": bets,
        "mean_net_bps": _fmt(statistics.mean(nets)) if nets else None,
        "median_net_bps": _fmt(statistics.median(nets)) if nets else None,
        "hit_rate": _fmt(sum(1 for x in nets if x > 0) / len(nets), 3) if nets else None,
        "mean_r": _fmt(statistics.mean(rs), 3) if rs else None,
        "sum_net_bps": _fmt(sum(nets)),
        "sharpe_daily": _opt(daily_sharpe(settled, window), 3),
        "max_drawdown_r": _opt(drawdown_r(settled), 3),
        "time_in_market": _fmt(time_in_market(settled, window), 4),
        "ideas_per_day": _fmt(len(settled) / max(window.days, 1), 3),
        "against_random": against,
        "rank_ic": rank_ic(ic) if ic else None,
        "contaminated": contaminated,
        "verdict": verdict(
            bets, window.days, against, contaminated=contaminated, null_row=null_row
        ),
    }


def independent_bets(settled: Sequence[Settlement]) -> int:
    free_at: dict[str, int] = {}
    count = 0
    for s in sorted(settled, key=lambda s: (s.entry_ns, s.instrument)):
        if s.entry_ns >= free_at.get(s.instrument, s.entry_ns):
            free_at[s.instrument] = s.exit_ns
            count += 1
    return count


def daily_sharpe(settled: Sequence[Settlement], window: Window) -> float | None:
    days = window.days
    if days < 2:
        return None
    daily = [0.0] * days
    for s in settled:
        index = (s.exit_ns - window.start_ns) // DAY_NS
        daily[min(max(index, 0), days - 1)] += float(s.net_bps)
    spread = statistics.stdev(daily)
    if spread == 0:
        return None
    return statistics.mean(daily) / spread * math.sqrt(DAYS_PER_YEAR)


def drawdown_r(settled: Sequence[Settlement]) -> float | None:
    path = sorted((s.exit_ns, float(r)) for s in settled if (r := s.r_multiple) is not None)
    if not path:
        return None
    peak = total = worst = 0.0
    for _, step in path:
        total += step
        peak = max(peak, total)
        worst = max(worst, peak - total)
    return worst


def time_in_market(settled: Sequence[Settlement], window: Window) -> float:
    held = sum(
        max(0, min(s.exit_ns, window.end_ns) - max(s.entry_ns, window.start_ns)) for s in settled
    )
    return held / (window.instruments * (window.end_ns - window.start_ns))


def _by_day(settled: Sequence[Settlement]) -> dict[str, list[float]]:
    found: dict[str, list[float]] = defaultdict(list)
    for s in settled:
        found[day_of(s.entry_ns).isoformat()].append(float(s.net_bps))
    return found


def versus(settled: Sequence[Settlement], null: Sequence[Settlement]) -> dict[str, str] | None:
    mine, theirs = _by_day(settled), _by_day(null)
    if not mine or not theirs:
        return None
    days = sorted(set(mine) | set(theirs))
    table = np.array(
        [
            [
                sum(mine.get(d, [])),
                len(mine.get(d, [])),
                sum(theirs.get(d, [])),
                len(theirs.get(d, [])),
            ]
            for d in days
        ],
        dtype=float,
    )
    picks = np.random.default_rng(SEED).integers(0, len(days), size=(RESAMPLES, len(days)))
    drawn = table[picks].sum(axis=1)
    valid = (drawn[:, 1] > 0) & (drawn[:, 3] > 0)
    diffs = np.sort(drawn[valid, 0] / drawn[valid, 1] - drawn[valid, 2] / drawn[valid, 3])
    if diffs.size < RESAMPLES // 2:
        return None
    observed = statistics.mean(float(s.net_bps) for s in settled) - statistics.mean(
        float(s.net_bps) for s in null
    )
    return {
        "difference_bps": _fmt(observed),
        "low_bps": _fmt(float(diffs[int(0.025 * diffs.size)])),
        "high_bps": _fmt(float(diffs[int(0.975 * diffs.size) - 1])),
        "resamples": str(int(diffs.size)),
    }


def rank_ic(pairs: Sequence[tuple[str, float, float]]) -> dict[str, str | None]:
    by_date: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for day, score, after in pairs:
        by_date[day].append((score, after))
    ics = [
        spearman([s for s, _ in found], [a for _, a in found])
        for _, found in sorted(by_date.items())
        if len(found) >= 3
    ]
    if len(ics) < MIN_IC_DATES:
        return {"dates": str(len(ics)), "verdict": "too few dates"}
    mean = statistics.mean(ics)
    spread = statistics.stdev(ics)
    t = mean / (spread / math.sqrt(len(ics))) if spread > 0 else None
    return {"dates": str(len(ics)), "mean_ic": _fmt(mean, 4), "t_stat": _opt(t, 2)}


def verdict(
    bets: int,
    days: int,
    against: dict[str, str] | None,
    *,
    contaminated: bool,
    null_row: bool = False,
) -> str:
    if contaminated:
        return "contaminated: the policy may have seen this window"
    if null_row:
        return "the null"
    if bets < MIN_BETS or days < MIN_DAYS:
        return "sample too small"
    if against is None:
        return "no comparison with random"
    if Decimal(against["low_bps"]) > 0:
        return "beats random"
    if Decimal(against["high_bps"]) < 0:
        return "worse than random"
    return "no clear difference from random"
