"""A response cache for language-model calls, so a replay asks no model and a record can be audited.

A call is keyed by what decides its answer: the role, the provider and model, the prompt's version,
the brief it was given (system prompt, user prompt and schema), which sample it is, and how it was
sampled. In `replay` a key the cache does not hold is an error, never a call, so a replay cannot
quietly pay a vendor and draw a new answer. In `record` a key the cache already holds is answered
from it, so a record never holds two answers for one key; a new sample is a new `sample_index`.
Each entry is one JSON file, written whole, and checked against its key when it is read.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from trading_research_agents.llm.completer import Completer, Completion
from trading_research_agents.records import write_json


class CacheMode(StrEnum):
    RECORD = "record"
    REPLAY = "replay"
    OFF = "off"


class CacheMissError(LookupError):
    """Replay asked for a call the cache does not hold."""


class CacheError(ValueError):
    """An entry on disk that does not match the key it is filed under."""


@dataclass(frozen=True)
class Sampling:
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None


@dataclass(frozen=True)
class CallIdentity:
    role: str
    provider: str
    model: str
    prompt_version: str
    sampling: Sampling = field(default_factory=Sampling)
    sample_index: int = 0

    def record(self) -> dict[str, Any]:
        return asdict(self)


def _canonical(value: Any) -> bytes:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text.encode("ascii")


def brief_sha256(system: str, user: str, schema: Mapping[str, Any]) -> str:
    body = {"schema": dict(schema), "system": system, "user": user}
    return hashlib.sha256(_canonical(body)).hexdigest()


def cache_key(identity: CallIdentity, brief: str) -> str:
    return hashlib.sha256(_canonical({**identity.record(), "brief_sha256": brief})).hexdigest()


def _identity(raw: Mapping[str, Any]) -> CallIdentity:
    return CallIdentity(
        role=str(raw["role"]),
        provider=str(raw["provider"]),
        model=str(raw["model"]),
        prompt_version=str(raw["prompt_version"]),
        sampling=Sampling(**raw["sampling"]),
        sample_index=int(raw["sample_index"]),
    )


class ResponseCache:
    """Entries under `root/<key[:2]>/<key>.json`."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.path(key)
        if not path.is_file():
            return None
        try:
            entry: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            request = entry["request"]
            brief = brief_sha256(request["system"], request["user"], request["schema"])
            identity = _identity(entry["identity"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CacheError(f"{path.name}: not a cache entry this version reads ({exc})") from exc
        if brief != entry.get("brief_sha256") or cache_key(identity, brief) != key:
            raise CacheError(f"{path.name} does not match its key; refused")
        return entry

    def put(self, key: str, entry: Mapping[str, Any]) -> Path:
        return write_json(self.path(key), dict(entry))


def _completion(raw: Mapping[str, Any]) -> Completion:
    return Completion(
        text=str(raw["text"]),
        model=str(raw["model"]),
        input_tokens=int(raw["input_tokens"]),
        output_tokens=int(raw["output_tokens"]),
        cached_tokens=int(raw.get("cached_tokens", 0)),
        cache_write_tokens=int(raw.get("cache_write_tokens", 0)),
        provider=str(raw.get("provider", "")),
    )


class CachedCompleter:
    """A `Completer` that answers from the cache, records into it, or neither, by mode."""

    def __init__(
        self,
        inner: Completer,
        cache: ResponseCache,
        identity: CallIdentity,
        *,
        mode: CacheMode,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self.inner = inner
        self.cache = cache
        self.identity = identity
        self.mode = mode
        self.clock = clock

    def complete(self, *, system: str, user: str, schema: Mapping[str, Any]) -> Completion:
        if self.mode is CacheMode.OFF:
            return self.inner.complete(system=system, user=user, schema=schema)
        brief = brief_sha256(system, user, schema)
        key = cache_key(self.identity, brief)
        found = self.cache.get(key)
        if found is not None:
            return _completion(found["response"])
        if self.mode is CacheMode.REPLAY:
            raise CacheMissError(
                f"no cached answer for {self.identity.role}, sample {self.identity.sample_index} "
                f"(key {key[:12]}); replay never calls a model"
            )
        made = self.inner.complete(system=system, user=user, schema=schema)
        entry = {
            "key": key,
            "identity": self.identity.record(),
            "brief_sha256": brief,
            "request": {"system": system, "user": user, "schema": dict(schema)},
            "response": asdict(made),
            "made_ns": self.clock(),
        }
        self.cache.put(key, entry)
        return made
