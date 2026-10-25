"""UTC days as exact nanosecond spans, without a float or a time zone database in the way.

An archive is cut by UTC day, and a bar belongs to the day its start falls in. Converting through
`datetime.timestamp()` goes through a float, which is exact for whole seconds today and a source
of off-by-one errors the day someone passes it a nanosecond; integer arithmetic on the epoch is
exact always.
"""

from __future__ import annotations

from datetime import date, timedelta

NS_PER_S = 1_000_000_000
NS_PER_MINUTE = 60 * NS_PER_S
DAY_NS = 86_400 * NS_PER_S
EPOCH = date(1970, 1, 1)


def midnight_ns(day: date) -> int:
    """The first nanosecond of `day`, UTC."""
    return (day - EPOCH).days * DAY_NS


def day_of(ns: int) -> date:
    """The UTC day a nanosecond falls in."""
    return EPOCH + timedelta(days=ns // DAY_NS)
