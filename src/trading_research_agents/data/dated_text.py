"""Dated text: events and notes, read from a file or declared in news.toml, and what falls inside a
horizon.
"""

from __future__ import annotations

import csv
import json
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from trading_research_agents.data.series import NS_PER_S, SeriesError, _duration, _ts_ns, moment


@dataclass(frozen=True)
class Event:
    when_ns: int
    what: str
    source: str

    def record(self) -> dict[str, Any]:
        return {
            "when": moment(self.when_ns).isoformat(),
            "what": self.what,
            "source": self.source,
        }


def read_events(path: Path) -> tuple[Event, ...]:
    """A calendar: JSON list or CSV with `when` (ISO 8601 with timezone) and `what`."""
    if not path.is_file():
        raise SeriesError(f"events file {path} does not exist")
    text = path.read_text(encoding="utf-8")
    rows: Iterable[dict[str, Any]]
    if path.suffix.lower() == ".json":
        loaded = json.loads(text)
        if not isinstance(loaded, list):
            raise SeriesError("an events file is a list of objects")
        rows = loaded
    else:
        rows = csv.DictReader(text.splitlines())
    events = []
    for number, row in enumerate(rows, start=1):
        try:
            when = _ts_ns(str(row["when"]).strip(), "iso8601")
        except (KeyError, ValueError) as exc:
            raise SeriesError(f"events row {number}: {exc}") from exc
        events.append(Event(when, str(row.get("what", "")).strip(), str(row.get("source", ""))))
    return tuple(sorted(events, key=lambda e: e.when_ns))


@dataclass(frozen=True)
class NewsSpec:
    """One dated-text file a project declares: a release, a statement, a note the desk wrote, or a
    feed the desk fetches into it."""

    name: str
    file: str
    source: str
    fetch: Mapping[str, Any] | None = None
    refresh: timedelta = timedelta(minutes=15)
    keep: timedelta = timedelta(days=7)


def load_news_map(path: Path) -> tuple[NewsSpec, ...]:
    """`news.toml`: one `[[news]]` per dated-text file the operator wants a role to read.

    Dated text is its own class of data, declared the way a series is, so a desk can keep a
    curated file of its own beside a vendor's export and neither becomes the format the other
    has to match.

        [[news]]
        name = "policy"
        file = "news/policy.csv"     # columns: when, what[, source]
        source = "operator export"
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SeriesError(f"{path}: {exc}") from exc
    entries = raw.get("news")
    if not isinstance(entries, list) or not entries:
        raise SeriesError(f"{path}: a [[news]] entry naming a file is required")
    specs = []
    for number, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or not entry.get("file"):
            raise SeriesError(f"{path}: news entry {number} names its file")
        name = str(entry.get("name", "")).strip() or f"news {number}"
        fetch = entry.get("fetch")
        if fetch is not None and (
            not isinstance(fetch, dict) or fetch.get("from") not in ("rss", "http")
        ):
            raise SeriesError(
                f'{name}: fetch is a table, {{ from = "rss" }} or {{ from = "http" }}'
            )
        try:
            refresh = _duration(str(entry.get("refresh", "15m")), "refresh")
            keep = _duration(str(entry.get("keep", "7d")), "keep")
        except (ValueError, SeriesError) as exc:
            raise SeriesError(f"{name}: {exc}") from exc
        specs.append(
            NewsSpec(
                name=name,
                file=str(entry["file"]),
                source=str(entry.get("source", "")).strip(),
                fetch=fetch,
                refresh=refresh,
                keep=keep,
            )
        )
    return tuple(specs)


def load_news(path: Path) -> tuple[Event, ...]:
    """Every dated-text file a `news.toml` names, read as one list of events."""
    base = path.resolve().parent
    out: list[Event] = []
    for spec in load_news_map(path):
        target = Path(spec.file)
        for event in read_events(target if target.is_absolute() else base / target):
            # A declared source is a fallback, never an overwrite: the row knows better.
            out.append(
                Event(event.when_ns, event.what, event.source or spec.source)
                if spec.source
                else event
            )
    return tuple(sorted(out, key=lambda e: e.when_ns))


def upcoming(events: Sequence[Event], now_ns: int, *, horizon: timedelta) -> list[Event]:
    end = now_ns + int(horizon.total_seconds() * NS_PER_S)
    return [e for e in events if now_ns <= e.when_ns <= end]
