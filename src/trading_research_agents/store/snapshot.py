"""The snapshot: what the engine saw at one moment, and where every number came from.

One cycle, one file. An agent is handed figures built from this file and never from a vendor,
so two things follow that matter more than they look. A cycle can be replayed exactly, because
the inputs are on disk. And an auditor can say what the agent knew and how old it was, without
asking anyone to remember.

Nothing here decides. A snapshot records what was fetched, what was tried and failed, what came
back stale, and what was refused outright.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_research_agents.data.sources.base import Fetched
from trading_research_agents.data.sources.chain import Attempt, ChainExhaustedError, SourceChain
from trading_research_agents.records import write_json

CYCLE_FORMAT = "%Y-%m-%dT%H-%M-%SZ"


def cycle_id(at: datetime | None = None, label: str = "") -> str:
    """The id of a cycle: its own start, to the second, in UTC, safe as a directory name.

    A desk that watches more than one instrument runs more than one cycle in the same second, so the
    id carries what it was about as well as when it started. The moment is always the first twenty
    characters, whatever follows it.
    """
    moment = at or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("a cycle id comes from a timezone-aware moment")
    stamp = moment.astimezone(UTC).strftime(CYCLE_FORMAT)
    safe = "".join(c for c in label if c.isalnum() or c in "-_")
    return f"{stamp}-{safe}" if safe else stamp


@dataclass
class Snapshot:
    """The values one cycle may use, with the attempts that produced them."""

    cycle: str
    instrument: str
    values: dict[str, Fetched] = field(default_factory=dict)
    attempts: list[Attempt] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)

    def put(self, fetched: Fetched, *, stale: bool = False) -> None:
        self.values[fetched.kind] = fetched
        if stale and fetched.kind not in self.stale:
            self.stale.append(fetched.kind)

    def refuse(self, kind: str, reason: str) -> None:
        self.refused[kind] = reason

    def get(self, kind: str) -> Fetched:
        if kind not in self.values:
            reason = self.refused.get(kind, "not fetched")
            raise KeyError(f"{kind} is not in the snapshot: {reason}")
        return self.values[kind]

    @property
    def usable(self) -> bool:
        """A snapshot with nothing refused and nothing stale is one a decision may rest on."""
        return not self.refused and not self.stale

    def record(self, now: float | None = None) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle,
            "instrument": self.instrument,
            "values": {kind: f.record(now) for kind, f in sorted(self.values.items())},
            "attempts": [a.record() for a in self.attempts],
            "stale": list(self.stale),
            "refused": dict(self.refused),
        }

    def write(self, path: Path, now: float | None = None) -> Path:
        return write_json(path, self.record(now))


def take_snapshot(
    instrument: str,
    chains: Mapping[str, SourceChain],
    *,
    cycle: str | None = None,
    kinds: Sequence[str] | None = None,
    options: Mapping[str, Mapping[str, Any]] | None = None,
) -> Snapshot:
    """Walk one chain per kind and keep what answered, with every attempt on the record.

    A kind whose chain is exhausted is refused, not guessed, and the snapshot says which kinds
    were refused. The caller decides whether a cycle can proceed on what is left.
    """
    snapshot = Snapshot(cycle=cycle or cycle_id(), instrument=instrument)
    wanted = list(kinds) if kinds is not None else list(chains)
    per_kind = dict(options or {})
    for kind in wanted:
        chain = chains.get(kind)
        if chain is None:
            snapshot.refuse(kind, "no chain declared")
            continue
        try:
            result = chain.fetch(instrument, **dict(per_kind.get(kind, {})))
        except ChainExhaustedError as exc:
            snapshot.refuse(kind, str(exc))
            continue
        snapshot.attempts.extend(result.attempts)
        if result.fetched is not None:
            snapshot.put(result.fetched, stale=result.stale)
    return snapshot
