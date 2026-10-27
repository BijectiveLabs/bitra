"""Fetch dated text into a declared news file: an RSS or Atom feed, or a JSON news API.

The readings still read only the local file, as `when,what,source` rows. This writes it: new
headlines are merged with what the file holds, anything older than the entry's `keep` is dropped,
and the text is reduced to a plain line before it is stored, because a headline is material a
model reads and never markup it renders.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
import urllib.parse
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from trading_research_agents.data.dated_text import Event, NewsSpec, read_events
from trading_research_agents.data.pull import Getter, Pulled, PullError, request
from trading_research_agents.data.series import NS_PER_S, TIME_UNITS, SeriesError

NEWS_KEYS = (
    "from",
    "url",
    "params",
    "rows",
    "ts",
    "ts_unit",
    "text",
    "source",
    "key_env",
    "key_param",
    "key_header",
    "key_prefix",
)
MAX_TEXT = 280
MAX_ROWS = 200
TAGS = re.compile(r"<[^>]+>")
SPACE = re.compile(r"\s+")
ATOM = "{http://www.w3.org/2005/Atom}"


def plain(text: str) -> str:
    """One line of text: markup and entities resolved, control characters gone, length capped."""
    line = "".join(c if c.isprintable() else " " for c in TAGS.sub(" ", html.unescape(text)))
    line = SPACE.sub(" ", line).strip()
    return line if len(line) <= MAX_TEXT else line[: MAX_TEXT - 3].rstrip() + "..."


def plan(fetch: Mapping[str, Any], name: str) -> dict[str, Any]:
    unknown = sorted(set(fetch) - set(NEWS_KEYS))
    if unknown:
        raise PullError(f"{name}: fetch has unknown {unknown}; have {list(NEWS_KEYS)}")
    how = dict(fetch)
    params = how.pop("params", None) or {}
    if not isinstance(params, Mapping):
        raise PullError(f"{name}: fetch.params is a table of query fields")
    url = str(how.get("url", ""))
    if params:
        query = urllib.parse.urlencode({str(k): str(v) for k, v in params.items()})
        url = f"{url}{'&' if '?' in url else '?'}{query}"
    if not url.startswith("https://"):
        raise PullError(f"{name}: fetch.url is an https address")
    how["url"] = url
    if how["from"] == "http":
        how.setdefault("rows", "")
        how.setdefault("ts", "published_at")
        how.setdefault("ts_unit", "iso8601")
        how.setdefault("text", "title")
    return how


def _when(text: str, unit: str = "iso8601") -> int:
    text = text.strip()
    if unit != "iso8601":
        return int(float(text) * TIME_UNITS[unit])
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # RSS stamps items the way e-mail does: "Sat, 18 Oct 2026 03:00:00 GMT".
        parsed = parsedate_to_datetime(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * NS_PER_S)


def _feed(body: bytes, declared: str, host: str, name: str) -> list[Event]:
    # A feed has no use for a document type; refusing one refuses entity expansion with it.
    if b"<!DOCTYPE" in body[:4096].upper() or b"<!ENTITY" in body.upper():
        raise PullError(f"{name}: the feed declares a document type, which is not read")
    try:
        root = ElementTree.fromstring(body)  # noqa: S314 - no document type is let through
    except ElementTree.ParseError as exc:
        raise PullError(f"{name}: the feed is not XML: {exc}") from None
    out: list[Event] = []
    channel = root.find("channel")
    if channel is not None:
        source = declared or plain(channel.findtext("title") or "") or host
        for item in channel.findall("item")[:MAX_ROWS]:
            title, stamp = item.findtext("title") or "", item.findtext("pubDate") or ""
            if title.strip() and stamp.strip():
                try:
                    out.append(Event(_when(stamp), plain(title), source))
                except (ValueError, TypeError, OverflowError):
                    continue
        return out
    source = declared or plain(root.findtext(f"{ATOM}title") or "") or host
    for entry in root.findall(f"{ATOM}entry")[:MAX_ROWS]:
        title = entry.findtext(f"{ATOM}title") or ""
        stamp = entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated") or ""
        if title.strip() and stamp.strip():
            try:
                out.append(Event(_when(stamp), plain(title), source))
            except (ValueError, TypeError, OverflowError):
                continue
    return out


def _field(row: Mapping[str, Any], path: str) -> Any:
    value: Any = row
    for step in [s for s in path.split(".") if s]:
        if not isinstance(value, Mapping) or step not in value:
            return None
        value = value[step]
    return value


def _api(body: bytes, how: Mapping[str, Any], fallback: str, name: str) -> list[Event]:
    try:
        loaded: Any = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PullError(f"{name}: the answer is not JSON: {exc}") from None
    rows = _field(loaded, str(how["rows"])) if how["rows"] else loaded
    if not isinstance(rows, list):
        raise PullError(f"{name}: {how['rows'] or 'the answer'} is not a list of rows")
    out: list[Event] = []
    for row in rows[:MAX_ROWS]:
        if not isinstance(row, Mapping):
            continue
        text, stamp = _field(row, str(how["text"])), _field(row, str(how["ts"]))
        if not text or stamp is None:
            continue
        source = _field(row, str(how["source"])) if how.get("source") else None
        try:
            out.append(
                Event(
                    _when(str(stamp), str(how["ts_unit"])),
                    plain(str(text)),
                    str(source or fallback),
                )
            )
        except (ValueError, TypeError, OverflowError, KeyError):
            continue
    return out


def pull_news(
    spec: NewsSpec,
    base: Path,
    *,
    env: Mapping[str, str] | None = None,
    get: Getter | None = None,
    now: datetime | None = None,
) -> Pulled:
    """Fetch, merge with the file, drop what is older than `keep`, write it back."""
    if spec.fetch is None:
        raise PullError(f"{spec.name}: no fetch declared; the file is kept by hand")
    how = plan(spec.fetch, spec.name)
    body = request(how, spec.name, env=env, get=get)
    host = urllib.parse.urlsplit(str(how["url"])).hostname or ""
    fresh = (
        _feed(body, spec.source, host, spec.name)
        if how["from"] == "rss"
        else _api(body, how, spec.source or host, spec.name)
    )
    path = base / spec.file if not Path(spec.file).is_absolute() else Path(spec.file)
    held: Sequence[Event] = ()
    if path.is_file():
        try:
            held = read_events(path)
        except SeriesError as exc:
            raise PullError(
                f"{spec.name}: the file on disk cannot be read, so it is not merged: {exc}"
            ) from exc
    cutoff = int(((now or datetime.now(UTC)).timestamp() - spec.keep.total_seconds()) * NS_PER_S)
    merged = {(e.when_ns, e.what): e for e in (*held, *fresh) if e.when_ns >= cutoff and e.what}
    before = sum(1 for e in held if e.when_ns >= cutoff)
    rows = sorted(merged.values(), key=lambda e: e.when_ns)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["when", "what", "source"])
    for event in rows:
        writer.writerow(
            [
                datetime.fromtimestamp(event.when_ns / NS_PER_S, tz=UTC).isoformat(),
                event.what,
                event.source,
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(out.getvalue(), encoding="utf-8")
    temporary.replace(path)
    last = rows[-1].when_ns if rows else None
    return Pulled(
        series=spec.name,
        added=len(rows) - before,
        rows=len(rows),
        last=None if last is None else datetime.fromtimestamp(last / NS_PER_S, tz=UTC).isoformat(),
    )


def news_due(spec: NewsSpec, base: Path, now: datetime) -> bool:
    """A fetched news file that is missing or older than its refresh interval."""
    if spec.fetch is None:
        return False
    path = base / spec.file if not Path(spec.file).is_absolute() else Path(spec.file)
    if not path.is_file():
        return True
    return now.timestamp() - path.stat().st_mtime >= spec.refresh.total_seconds()
