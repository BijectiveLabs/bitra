"""A reading analyst's last answer, kept for as long as the figures it read have not changed.

The macro, on-chain and sentiment briefs are built from series that move daily on free feeds and
faster on paid ones. When a brief's figures are identical to the last one answered, asking again
buys the same reading at the price of a call. The key is the figures themselves, so new data, on any
feed at any speed, always asks again; the window only bounds how long an unchanged reading stands.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_research_agents.records import write_json

DIRECTORY = "reuse"
# What changes with the clock rather than with the data, and so says nothing about the reading.
VOLATILE = frozenset({"as_of", "age_s", "age_h", "instrument", "known_since", "counts"})
KEPT = 64


def key_of(brief: Mapping[str, Any]) -> str:
    """The figures of a brief, without the clock, as a short stable hash."""
    canonical = json.dumps(_figures(brief), sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def lookup(
    root: Path, role: str, key: str, *, window_s: float, now: datetime
) -> tuple[Any, float] | None:
    """The answer given to these exact figures, and its age, when it is inside the window."""
    if window_s <= 0:
        return None
    entry = _read(root, role).get(key)
    if not isinstance(entry, Mapping):
        return None
    try:
        age = (now - datetime.fromisoformat(str(entry["at"]))).total_seconds()
    except (KeyError, ValueError, TypeError):
        return None
    if age < 0 or age > window_s:
        return None
    return entry.get("answer"), age


def remember(root: Path, role: str, key: str, answer: Any, *, now: datetime) -> None:
    kept = _read(root, role)
    kept[key] = {"at": now.isoformat(), "answer": answer}
    newest = sorted(kept.items(), key=lambda kv: str(kv[1].get("at", "")), reverse=True)[:KEPT]
    write_json(_path(root, role), dict(newest))


def _figures(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _figures(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list | tuple):
        return [_figures(v) for v in value]
    return value


def _path(root: Path, role: str) -> Path:
    return root / DIRECTORY / f"{role}.json"


def _read(root: Path, role: str) -> dict[str, Any]:
    path = _path(root, role)
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
