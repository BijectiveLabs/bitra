"""When an equity underlying's exchange trades, for markets that trade its token around the clock.

A stock perpetual or a tokenized stock quotes at three in the morning and on a Sunday, but the price
it tracks is made on the exchange during its session. Outside it the reference stops moving, the
spread widens, and the next open can gap past any stop. The desk's rule: no new entry on an equity
underlying while its exchange is closed, and none whose hold would run past the session's close.

The session is the US regular one, 09:30 to 16:00 in New York, Monday to Friday, read in the
exchange's own time zone so daylight saving is the time zone database's, not this module's.
Exchange holidays are the operator's to declare: no calendar here claims to know them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

EXCHANGE = ZoneInfo("America/New_York")
OPENS = time(9, 30)
CLOSES = time(16, 0)


class SessionError(ValueError):
    """The session rule is configured in a way that cannot be applied."""


def _local(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise SessionError("a session is judged on an aware time, never a naive one")
    return now.astimezone(EXCHANGE)


def is_open(now: datetime, closed: Sequence[date] = ()) -> bool:
    """Whether the regular session is trading at `now`."""
    local = _local(now)
    if local.weekday() >= 5 or local.date() in closed:
        return False
    return OPENS <= local.time() < CLOSES


def closes_at(now: datetime, closed: Sequence[date] = ()) -> datetime | None:
    """The end of the session trading at `now`, in UTC; None when none is trading."""
    if not is_open(now, closed):
        return None
    local = _local(now)
    return datetime.combine(local.date(), CLOSES, tzinfo=EXCHANGE).astimezone(UTC)


def refusal(now: datetime, hold: timedelta, closed: Sequence[date] = ()) -> str:
    """Why an entry on an equity underlying is refused at `now` for a hold this long, or nothing."""
    end = closes_at(now, closed)
    if end is None:
        return (
            "the underlying's exchange is closed (the US regular session is 09:30 to 16:00 New "
            "York time, Monday to Friday, less the dates in [execution].equity_closed)"
        )
    if now + hold > end:
        local = end.astimezone(EXCHANGE)
        return (
            f"a hold of {int(hold.total_seconds() // 60)} minutes would run past the session's "
            f"close at {local:%H:%M} New York time, and the next open can gap past the stop"
        )
    return ""
