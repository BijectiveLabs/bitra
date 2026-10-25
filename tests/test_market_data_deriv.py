from __future__ import annotations

from decimal import Decimal

import pytest

from trading_research_agents.market_data.bars import MarketDataError
from trading_research_agents.market_data.days import NS_PER_S
from trading_research_agents.market_data.deriv import (
    MAX_AGE_S,
    DerivPoint,
    Known,
    Series,
    Timeline,
    Unknown,
    detail_of,
)

T0 = 1_751_328_000 * NS_PER_S


def point(series: Series, at_s: int, value: str, received_s: int = 0) -> DerivPoint:
    return DerivPoint(series, T0 + at_s * NS_PER_S, T0 + received_s * NS_PER_S, Decimal(value))


def test_a_value_is_known_from_its_own_time_on() -> None:
    line = Timeline(Series.OPEN_INTEREST, [point(Series.OPEN_INTEREST, 0, "10")])
    found = line.at(T0)
    assert isinstance(found, Known) and found.value == Decimal("10") and found.age_s == 0
    assert isinstance(line.at(T0 - 1), Unknown)


def test_a_value_past_its_age_is_unknown_not_carried() -> None:
    line = Timeline(Series.OPEN_INTEREST, [point(Series.OPEN_INTEREST, 0, "10")])
    limit = MAX_AGE_S[Series.OPEN_INTEREST]
    assert isinstance(line.at(T0 + limit * NS_PER_S), Known)
    stale = line.at(T0 + (limit + 1) * NS_PER_S)
    assert isinstance(stale, Unknown) and "stale" in stale.reason


def test_the_newest_value_at_or_before_the_moment_is_used() -> None:
    line = Timeline(
        Series.FUNDING,
        [point(Series.FUNDING, 3600, "0.0002"), point(Series.FUNDING, 0, "-0.0001")],
    )
    assert [p.as_of_ns for p in line.points] == [T0, T0 + 3600 * NS_PER_S]
    found = line.at(T0 + 3599 * NS_PER_S)
    assert isinstance(found, Known) and found.value == Decimal("-0.0001")


def test_two_values_for_one_moment_keep_the_later_received() -> None:
    line = Timeline(
        Series.MARK,
        [point(Series.MARK, 0, "100", received_s=5), point(Series.MARK, 0, "101", received_s=9)],
    )
    assert len(line) == 1
    found = line.at(T0)
    assert isinstance(found, Known) and found.value == Decimal("101")


def test_liquidations_are_events_not_a_level() -> None:
    with pytest.raises(MarketDataError, match="events"):
        Timeline(Series.LIQUIDATION, [])


def test_a_timeline_holds_one_series() -> None:
    with pytest.raises(MarketDataError, match="one series"):
        Timeline(Series.MARK, [point(Series.INDEX, 0, "1")])


def test_values_are_checked_by_series() -> None:
    assert point(Series.FUNDING, 0, "-0.001").value < 0
    with pytest.raises(MarketDataError, match="not negative"):
        point(Series.OPEN_INTEREST, 0, "-1")
    with pytest.raises(MarketDataError, match="positive"):
        point(Series.MARK, 0, "0")
    with pytest.raises(MarketDataError, match="finite"):
        point(Series.FUNDING, 0, "NaN")


def test_detail_is_sorted_and_readable() -> None:
    made = DerivPoint(
        Series.OPEN_INTEREST, T0, T0, Decimal(1), detail_of(timeframe="5m", measured="amount")
    )
    assert made.detail == (("measured", "amount"), ("timeframe", "5m"))
    assert made.get("timeframe") == "5m" and made.get("absent") is None
    with pytest.raises(MarketDataError, match="sorted"):
        DerivPoint(Series.FUNDING, T0, T0, Decimal(0), (("b", "1"), ("a", "2")))
