"""A cache with an expiry date, for saving a provider's quota and nothing else.

The rule that makes a cache safe here: it may only return a value that is still inside its own
time to live. A cache that hands back whatever it has is how a run ends up trading on last
hour's book while every log line says the fetch succeeded.

Entries are plain JSON files under one directory, so a client can look at them, delete them, and
know exactly what was kept.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CacheError(OSError):
    """The cache directory cannot be used."""


def key_for(parts: Mapping[str, Any]) -> str:
    """A stable name for a request: the same parameters always give the same file."""
    canonical = json.dumps(dict(sorted(parts.items())), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


@dataclass
class FileCache:
    """Values under `directory`, each with the moment it was written and its time to live."""

    directory: Path
    clock: Callable[[], float] = time.time

    def _path(self, key: str) -> Path:
        if not key or "/" in key or "\\" in key:
            raise CacheError(f"not a cache key: {key!r}")
        return self.directory / f"{key}.json"

    def get(self, key: str, ttl_s: float) -> Any | None:
        """The value if it was written less than `ttl_s` ago, otherwise None."""
        if ttl_s <= 0:
            return None
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            written = float(entry["written_at"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None
        if self.clock() - written > ttl_s:
            return None
        return entry.get("value")

    def put(self, key: str, value: Any) -> Path:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"written_at": self.clock(), "value": value}, sort_keys=True, default=str
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise CacheError(f"{path}: {exc}") from exc
        return path

    def sweep(self, ttl_s: float) -> int:
        """Delete every entry past its time to live. Returns how many went."""
        if not self.directory.is_dir():
            return 0
        gone = 0
        now = self.clock()
        for path in self.directory.glob("*.json"):
            try:
                written = float(json.loads(path.read_text(encoding="utf-8"))["written_at"])
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                path.unlink(missing_ok=True)
                gone += 1
                continue
            if now - written > ttl_s:
                path.unlink(missing_ok=True)
                gone += 1
        return gone
