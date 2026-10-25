"""Funding, open interest, mark, index and long/short as the venue published them, and what was
known of each at a given moment.

A derivatives figure is attached to a decision only if it had been published by then and is
young enough to still mean what it says. Past its age it is unknown, and the reason says so;
it is never carried forward as if it were current, because a funding rate from yesterday read as
today's is a wrong number that looks right.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from trading_research_agents.market_data.bars import MarketDataError
from trading_research_agents.market_data.days import NS_PER_S


class Series(StrEnum):
    FUNDING = "funding"
    OPEN_INTEREST = "open_interest"
    MARK = "mark"
    INDEX = "index"
    LONG_SHORT = "long_short"
    LIQUIDATION = "liquidation"


LEVELS = (Series.FUNDING, Series.OPEN_INTEREST, Series.MARK, Series.INDEX, Series.LONG_SHORT)
# Funding settles every one to eight hours, so a settled rate is current until the next one; open
# interest and long/short are bucketed by the venue; mark and index move every second.
MAX_AGE_S: Mapping[Series, int] = {
    Series.FUNDING: 9 * 3600,
    Series.OPEN_INTEREST: 15 * 60,
    Series.MARK: 120,
    Series.INDEX: 120,
    Series.LONG_SHORT: 2 * 3600,
}
_POSITIVE = (Series.MARK, Series.INDEX, Series.LONG_SHORT, Series.LIQUIDATION)


def detail_of(**fields: str) -> tuple[tuple[str, str], ...]:
    """Detail as the point stores it: keys sorted, so equal details are equal tuples."""
    return tuple(sorted(fields.items()))


@dataclass(frozen=True, slots=True)
class DerivPoint:
    series: Series
    as_of_ns: int
    received_ns: int
    value: Decimal
    detail: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.as_of_ns <= 0 or self.received_ns < 0:
            raise MarketDataError("a point carries the venue's time and when it was received")
        if not isinstance(self.value, Decimal) or not self.value.is_finite():
            raise MarketDataError(f"{self.series} value must be a finite Decimal")
        if self.series in _POSITIVE and self.value <= 0:
            raise MarketDataError(f"{self.series} is positive, got {self.value}")
        if self.series is Series.OPEN_INTEREST and self.value < 0:
            raise MarketDataError(f"open interest is not negative, got {self.value}")
        keys = [key for key, _ in self.detail]
        if keys != sorted(set(keys)):
            raise MarketDataError("detail keys are unique and sorted; build them with detail_of")

    def get(self, key: str) -> str | None:
        return next((value for name, value in self.detail if name == key), None)


@dataclass(frozen=True, slots=True)
class Known:
    value: Decimal
    as_of_ns: int
    age_s: float


@dataclass(frozen=True, slots=True)
class Unknown:
    reason: str


class Timeline:
    """One level series, sorted by the venue's time, read as of a moment."""

    def __init__(
        self, series: Series, points: Iterable[DerivPoint], *, max_age_s: int | None = None
    ) -> None:
        if series not in LEVELS:
            raise MarketDataError(f"{series} is a stream of events, not a level to read as of")
        by_time: dict[int, DerivPoint] = {}
        for point in points:
            if point.series is not series:
                raise MarketDataError(f"a {series} timeline holds one series, got {point.series}")
            held = by_time.get(point.as_of_ns)
            if held is None or point.received_ns >= held.received_ns:
                by_time[point.as_of_ns] = point
        self.series = series
        self.max_age_s = MAX_AGE_S[series] if max_age_s is None else max_age_s
        self.points = tuple(by_time[at] for at in sorted(by_time))
        self._times = [point.as_of_ns for point in self.points]

    def __len__(self) -> int:
        return len(self.points)

    def at(self, at_ns: int) -> Known | Unknown:
        """The newest value published at or before `at_ns`, if it is not past its age."""
        index = bisect_right(self._times, at_ns)
        if index == 0:
            return Unknown(f"{self.series}: nothing published at or before {at_ns}")
        point = self.points[index - 1]
        age_s = (at_ns - point.as_of_ns) / NS_PER_S
        if age_s > self.max_age_s:
            return Unknown(
                f"{self.series}: stale, the last value is {age_s:.0f}s old and the most a "
                f"value may be is {self.max_age_s}s"
            )
        return Known(point.value, point.as_of_ns, age_s)
