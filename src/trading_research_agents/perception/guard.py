"""The strongest adverse event among the judged headlines, for one asset: arithmetic, by the system.

The judge gives a probability for each kind of event and for whom a headline concerns. Combining
them is arithmetic, which the vendor says to keep in code: an event's weight is the probability of
the kind times how much it concerns the asset or the whole market, whichever is larger.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from trading_research_agents.data.dated_text import Event
from trading_research_agents.features.text import KINDS, TextEvent
from trading_research_agents.market_data.archive import HOUR_NS
from trading_research_agents.market_data.days import NS_PER_S, day_of
from trading_research_agents.market_data.text import (
    TextItem,
    TextLog,
    append_text_part,
    read_text_range,
)


@dataclass(frozen=True)
class Adverse:
    """The heaviest adverse event read, or none: its weight, its kind and the event itself."""

    weight: float
    kind: str | None
    event: TextEvent | None


def unknown_kinds(kinds: Iterable[str]) -> list[str]:
    return sorted(set(kinds) - set(KINDS))


def heaviest(events: Sequence[TextEvent], asset: str, kinds: Sequence[str]) -> Adverse:
    found = Adverse(0.0, None, None)
    for event in events:
        for kind in kinds:
            weight = event.kinds.get(kind, 0.0) * event.relevance(asset)
            if weight > found.weight:
                found = Adverse(weight, kind, event)
    return found


def first_seen_within(
    root: Path | None, feed: str, events: Sequence[Event], *, now_ns: int, window_h: int
) -> list[TextItem]:
    """The headlines first seen in the last `window_h` hours, after archiving the new ones.

    Which headlines are recent is decided here, from when the system first saw each one, never
    from the publisher's date and never by asking the judge, which the vendor says is unreliable
    at comparing dates and times. With no archive, every headline is read as seen now.
    """
    if root is None:
        return [TextItem(feed, now_ns, e.when_ns, e.what, e.source) for e in events]
    log = TextLog(root, feed, now_ns=now_ns)
    fresh = log.observe(events, now_ns=now_ns)
    if fresh:
        append_text_part(fresh, root, feed=feed, hour_start_ns=now_ns - now_ns % HOUR_NS)
    since = now_ns - window_h * 3600 * NS_PER_S
    held = read_text_range(root, feed, day_of(since) - timedelta(days=1), day_of(now_ns))
    return [item for item in held if since < item.first_seen_ns <= now_ns]


__all__ = ["Adverse", "first_seen_within", "heaviest", "unknown_kinds"]
