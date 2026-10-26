"""`[record]` in `data.toml`: what the recorder keeps, for which markets, at what pace.

```toml
[record]
venue = "<the venue ccxt names>"
instruments = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
streams = ["trades", "book", "liquidations"]   # where the venue supports them
book_sample_ms = 1000
book_levels = 50
requests_per_minute = 300                      # this process's share of the venue's limit
context = ["BTCUSDT", "ETHUSDT"]               # always recorded, for cross-asset features
text = ["coindesk"]                            # feeds from sources/news.toml, or "all"

[record.large_trade_quote]
BTCUSDT = 100000
```

An unknown key is refused with the closest name: a setting spelt wrong is a setting not applied.
"""

from __future__ import annotations

import difflib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trading_research_agents.market_data.bars import MarketDataError

STREAMS = ("trades", "book", "liquidations")
KEYS = (
    "venue",
    "instruments",
    "streams",
    "book_sample_ms",
    "book_levels",
    "requests_per_minute",
    "context",
    "text",
    "large_trade_quote",
    "grace_ms",
    "close_after_ms",
)
ALL = "all"


@dataclass(frozen=True)
class RecordConfig:
    venue: str
    instruments: tuple[str, ...]
    streams: tuple[str, ...] = STREAMS
    book_sample_ms: int = 1000
    book_levels: int = 50
    requests_per_minute: int | None = None
    context: tuple[str, ...] = ()
    text: tuple[str, ...] = ()
    large_trade_quote: Mapping[str, Decimal] = field(default_factory=dict)
    grace_ms: int = 2000
    close_after_ms: int = 5000

    @property
    def markets(self) -> tuple[str, ...]:
        """Every market recorded: the instruments, then the context ones not already among them."""
        return tuple(dict.fromkeys((*self.instruments, *self.context)))

    @classmethod
    def from_table(cls, table: Mapping[str, Any]) -> RecordConfig:
        unknown = sorted(set(table) - set(KEYS))
        if unknown:
            hints = [
                f"{name} (did you mean {close[0]}?)" if close else name
                for name in unknown
                for close in [difflib.get_close_matches(name, KEYS, n=1)]
            ]
            raise MarketDataError(f"[record] does not read {', '.join(hints)}; have {list(KEYS)}")
        venue = str(table.get("venue") or "").strip()
        instruments = _names(table.get("instruments"), "instruments")
        if not venue or not instruments:
            raise MarketDataError("[record] names a venue and its instruments")
        streams = _names(table.get("streams", list(STREAMS)), "streams")
        stray = sorted(set(streams) - set(STREAMS))
        if stray:
            raise MarketDataError(
                f"[record].streams: {stray} is not a stream; have {list(STREAMS)}"
            )
        return cls(
            venue=venue,
            instruments=instruments,
            streams=streams,
            book_sample_ms=_count(table, "book_sample_ms", 1000, low=100),
            book_levels=_count(table, "book_levels", 50, low=1),
            requests_per_minute=(
                _count(table, "requests_per_minute", 1, low=1)
                if "requests_per_minute" in table
                else None
            ),
            context=_names(table.get("context", list(instruments)), "context"),
            text=_text(table.get("text", [])),
            large_trade_quote=_quotes(table.get("large_trade_quote", {})),
            grace_ms=_count(table, "grace_ms", 2000, low=0),
            close_after_ms=_count(table, "close_after_ms", 5000, low=0),
        )


def load_record_config(path: Path) -> RecordConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MarketDataError(f"{path}: {exc}") from exc
    table = raw.get("record")
    if not isinstance(table, dict):
        raise MarketDataError(f"{path}: no [record] table; it names what the recorder keeps")
    return RecordConfig.from_table(table)


def _names(raw: Any, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(v, str) and v.strip() for v in raw):
        raise MarketDataError(f"[record].{key} is a list of names")
    return tuple(dict.fromkeys(v.strip() for v in raw))


def _text(raw: Any) -> tuple[str, ...]:
    if raw == ALL:
        return (ALL,)
    return _names(raw, "text")


def _count(table: Mapping[str, Any], key: str, default: int, *, low: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise MarketDataError(f"[record].{key} is a whole number of at least {low}")
    return value


def _quotes(raw: Any) -> dict[str, Decimal]:
    if not isinstance(raw, dict):
        raise MarketDataError("[record.large_trade_quote] is a table of instrument to quote amount")
    out: dict[str, Decimal] = {}
    for name, value in raw.items():
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            raise MarketDataError(f"[record.large_trade_quote].{name} is not a number") from None
        if not amount.is_finite() or amount <= 0:
            raise MarketDataError(f"[record.large_trade_quote].{name} is a positive amount")
        out[str(name)] = amount
    return out
