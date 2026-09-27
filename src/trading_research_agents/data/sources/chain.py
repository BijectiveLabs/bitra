"""A chain of sources, tried in order, with every attempt written down.

One source is a single point of failure, and the failure that matters most is the cheapest
one: a rate limit on a free endpoint. So the operator declares an order, the chain walks it,
and what happened on the way is part of the result rather than a log line nobody reads.

Two rules decide the outcome. A value older than the declared time to live is not a value:
the chain keeps looking. When the chain is exhausted, the default is to refuse, because a
cycle that quietly runs on stale prices is worse than a cycle that does not run.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from trading_research_agents.data.sources.base import Fetched, Source, SourceError

POLICIES = ("refuse", "use_stale_with_flag")


class ChainExhaustedError(RuntimeError):
    """No source in the chain could answer inside the declared freshness."""


@dataclass(frozen=True)
class Attempt:
    kind: str
    source: str
    outcome: str
    detail: str = ""

    def record(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "outcome": self.outcome,
            "detail": self.detail,
        }


@dataclass
class ChainResult:
    fetched: Fetched | None
    attempts: list[Attempt] = field(default_factory=list)
    stale: bool = False

    @property
    def ok(self) -> bool:
        return self.fetched is not None


@dataclass(frozen=True)
class SourceChain:
    """The declared order for one kind of data, with its freshness and its fallback policy."""

    kind: str
    sources: tuple[Source, ...]
    ttl_s: float
    on_exhausted: str = "refuse"
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if not self.sources:
            raise ValueError(f"the chain for {self.kind} names no source")
        if self.ttl_s <= 0:
            raise ValueError("ttl is positive")
        if self.on_exhausted not in POLICIES:
            raise ValueError(f"on_exhausted is one of {list(POLICIES)}")
        for source in self.sources:
            if self.kind not in source.kinds():
                raise ValueError(f"{source.name} does not serve {self.kind}")

    def fetch(self, instrument: str, **options: Any) -> ChainResult:
        result = ChainResult(fetched=None)
        best_stale: Fetched | None = None
        now = self.clock()
        for source in self.sources:
            try:
                fetched = source.fetch(self.kind, instrument, **options)
            except SourceError as exc:
                result.attempts.append(Attempt(self.kind, source.name, "error", str(exc)))
                continue
            except (OSError, ValueError, KeyError, TypeError) as exc:
                # A vendor that answers with a shape we cannot read is a failed source, not a
                # crash: the next one in the chain gets its turn.
                result.attempts.append(
                    Attempt(self.kind, source.name, "error", f"{exc.__class__.__name__}: {exc}")
                )
                continue
            age = fetched.age_s(now)
            if age > self.ttl_s:
                result.attempts.append(
                    Attempt(self.kind, source.name, "stale", f"{age}s older than ttl {self.ttl_s}s")
                )
                if best_stale is None or age < best_stale.age_s(now):
                    best_stale = fetched
                continue
            result.attempts.append(Attempt(self.kind, source.name, "ok", f"age {age}s"))
            result.fetched = fetched
            return result
        if self.on_exhausted == "use_stale_with_flag" and best_stale is not None:
            result.fetched = best_stale
            result.stale = True
            return result
        raise ChainExhaustedError(
            f"{self.kind} for {instrument}: no source inside {self.ttl_s}s; "
            + "; ".join(f"{a.source} {a.outcome} ({a.detail})" for a in result.attempts)
        )


def chains_by_kind(chains: Sequence[SourceChain]) -> dict[str, SourceChain]:
    out: dict[str, SourceChain] = {}
    for chain in chains:
        if chain.kind in out:
            raise ValueError(f"two chains declared for {chain.kind}")
        out[chain.kind] = chain
    return out
