"""The source that never needs the network: what is already in the project's `data/`.

This is the last link of most chains. It answers from the datasets `tra data get` wrote and the
funding files beside them, so a cycle can still be assembled when every endpoint is rate
limited, and so an operator with no keys at all can run the whole pipeline over a recorded day.

It is honest about age: a recorded day is old, and the value says so. A live chain with a short
time to live will reject it, which is the point. An operator replaying a day sets the time to
live to cover the day, and then this source is the only one that answers.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trading_research_agents.data.candles import CandleError, candles_of
from trading_research_agents.data.sources.base import Fetched, SourceError

DEFAULT_INTERVAL_S = 60
DEFAULT_LIMIT = 240


class FileSource:
    """Datasets and funding files under one directory, read with no network and no keys."""

    def __init__(self, directory: Path, *, name: str = "", clock: Any = time.time) -> None:
        self.directory = Path(directory)
        self._name = name or f"file:{self.directory.as_posix()}"
        self.clock = clock

    @property
    def name(self) -> str:
        return self._name

    @property
    def tier(self) -> str:
        return "free"

    def kinds(self) -> tuple[str, ...]:
        return ("ohlcv", "funding")

    def fetch(self, kind: str, instrument: str, **options: Any) -> Fetched:
        if kind == "ohlcv":
            return self._ohlcv(instrument, **options)
        if kind == "funding":
            return self._funding(instrument)
        raise SourceError(f"{self.name} does not serve {kind}")

    def _newest(self, pattern: str) -> Path:
        if not self.directory.is_dir():
            raise SourceError(f"{self.directory} is not a directory")
        found = sorted(self.directory.glob(pattern))
        if not found:
            raise SourceError(f"nothing matching {pattern} in {self.directory}")
        return max(found, key=lambda p: p.stat().st_mtime)

    def _ohlcv(self, instrument: str, **options: Any) -> Fetched:
        interval_s = int(options.get("interval_s", DEFAULT_INTERVAL_S))
        limit = int(options.get("limit", DEFAULT_LIMIT))
        if interval_s <= 0 or limit <= 0:
            raise SourceError("interval_s and limit are positive")
        path = self._newest(f"{instrument}-*.parquet")
        try:
            from trading_research_agents.data.parquet import read_trades
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise SourceError("reading datasets needs the [data] extra (pyarrow)") from exc
        try:
            candles = candles_of(read_trades(path), interval_s * 1_000_000_000)
        except (CandleError, OSError, ValueError) as exc:
            raise SourceError(f"{path.name}: {exc}") from exc
        if not candles:
            raise SourceError(f"{path.name} holds no trades")
        window = candles[-limit:]
        return Fetched(
            kind="ohlcv",
            instrument=instrument,
            value=window,
            source=self.name,
            tier=self.tier,
            fetched_at=self.clock(),
            as_of_ns=window[-1].start_ns + interval_s * 1_000_000_000,
            detail={
                "candles": len(window),
                "interval_s": interval_s,
                "last_close": str(window[-1].close),
                "file": path.name,
            },
        )

    def _funding(self, instrument: str) -> Fetched:
        path = self.directory / f"{instrument}-funding.json"
        if not path.is_file():
            raise SourceError(f"{path.name} does not exist")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceError(f"{path.name}: {exc}") from exc
        rates = raw.get("rates") if isinstance(raw, dict) else None
        if not isinstance(rates, dict) or not rates:
            raise SourceError(f"{path.name} carries no rates")
        try:
            settlements = sorted(int(k) for k in rates)
            last = settlements[-1]
            rate = Decimal(str(rates[str(last)]))
        except (ValueError, InvalidOperation, KeyError) as exc:
            raise SourceError(f"{path.name}: {exc}") from exc
        return Fetched(
            kind="funding",
            instrument=instrument,
            value=rate,
            source=self.name,
            tier=self.tier,
            fetched_at=self.clock(),
            as_of_ns=last,
            detail={"rate": str(rate), "settlements": len(settlements), "file": path.name},
        )
