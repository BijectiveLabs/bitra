from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from trading_research_agents.data.series import Event
from trading_research_agents.market_data.compact import compact_day
from trading_research_agents.market_data.days import DAY_NS, NS_PER_MINUTE, midnight_ns
from trading_research_agents.market_data.text import (
    TEXT,
    TextItem,
    TextLog,
    read_text_range,
    write_text_part,
)

pytest.importorskip("pyarrow")

DAY = date(2025, 7, 1)
T0 = midnight_ns(DAY)
HOUR = 60 * NS_PER_MINUTE


def headline(minute: int, what: str) -> Event:
    return Event(when_ns=T0 + minute * NS_PER_MINUTE, what=what, source="coindesk")


def test_a_headline_is_archived_once_at_first_sight(tmp_path: Path) -> None:
    log = TextLog(tmp_path, "coindesk", now_ns=T0)
    first = log.observe([headline(1, "exchange halts withdrawals")], now_ns=T0 + 2 * NS_PER_MINUTE)
    again = log.observe(
        [headline(1, "exchange halts withdrawals"), headline(3, "token listed")],
        now_ns=T0 + 5 * NS_PER_MINUTE,
    )
    assert [i.text for i in first] == ["exchange halts withdrawals"]
    assert [i.text for i in again] == ["token listed"]
    assert first[0].first_seen_ns == T0 + 2 * NS_PER_MINUTE
    assert first[0].published_ns == T0 + NS_PER_MINUTE


def test_the_publisher_time_is_kept_but_never_orders_the_stream(tmp_path: Path) -> None:
    items = [
        TextItem("coindesk", T0 + 10 * NS_PER_MINUTE, T0 + 9 * NS_PER_MINUTE, "later seen", "c"),
        # Published before the other, seen after it: a back-dated headline.
        TextItem("coindesk", T0 + 20 * NS_PER_MINUTE, T0 + 1 * NS_PER_MINUTE, "back-dated", "c"),
    ]
    write_text_part(items, tmp_path, feed="coindesk", hour_start_ns=T0)
    read = read_text_range(tmp_path, "coindesk", DAY, DAY)
    assert [i.text for i in read] == ["later seen", "back-dated"]
    assert read[1].published_ns == T0 + NS_PER_MINUTE


def test_a_restart_reads_what_was_already_archived(tmp_path: Path) -> None:
    seen = TextItem("coindesk", T0 + 2 * NS_PER_MINUTE, T0 + NS_PER_MINUTE, "already", "c")
    write_text_part([seen], tmp_path, feed="coindesk", hour_start_ns=T0)
    log = TextLog(tmp_path, "coindesk", now_ns=T0 + HOUR)
    assert log.observe([headline(1, "already")], now_ns=T0 + HOUR + 1) == []


def test_a_text_day_compacts_like_any_stream(tmp_path: Path) -> None:
    for hour in (0, 3):
        item = TextItem("coindesk", T0 + hour * HOUR + 5, None, f"h{hour}", "c")
        write_text_part([item], tmp_path, feed="coindesk", hour_start_ns=T0 + hour * HOUR)
    result = compact_day(tmp_path, "text", "coindesk", TEXT, DAY, now_ns=T0 + DAY_NS + HOUR)
    assert result.written and result.removed == 2
    assert [i.text for i in read_text_range(tmp_path, "coindesk", DAY, DAY)] == ["h0", "h3"]
