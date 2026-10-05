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
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.data.schema import Candle
from trading_research_agents.data.sources.base import BookLevel, Fetched, OrderBook
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


INPUTS = "inputs.json.gz"
INPUTS_SCHEMA = 1


def inputs_record(
    snapshot: Snapshot,
    context: Mapping[int, Fetched] | None = None,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything a cycle's briefs were computed from, as fetched, so they can be computed again."""
    return {
        "schema": INPUTS_SCHEMA,
        "cycle_id": snapshot.cycle,
        "instrument": snapshot.instrument,
        "values": {kind: _fetched(f) for kind, f in sorted(snapshot.values.items())},
        "context": {str(s): _fetched(f) for s, f in sorted((context or {}).items())},
        "settings": dict(settings or {}),
    }


def read_inputs(raw: Mapping[str, Any]) -> tuple[Snapshot, dict[int, Fetched], dict[str, Any]]:
    """The snapshot, the higher timeframes and the settings an inputs record was written from."""
    if raw.get("schema") != INPUTS_SCHEMA:
        raise ValueError(f"inputs schema {raw.get('schema')!r} is not {INPUTS_SCHEMA}")
    snapshot = Snapshot(cycle=str(raw["cycle_id"]), instrument=str(raw["instrument"]))
    for row in raw.get("values", {}).values():
        snapshot.put(_unfetched(row))
    context = {int(s): _unfetched(row) for s, row in raw.get("context", {}).items()}
    return snapshot, context, dict(raw.get("settings") or {})


def _fetched(f: Fetched) -> dict[str, Any]:
    return {
        "kind": f.kind,
        "instrument": f.instrument,
        "source": f.source,
        "tier": f.tier,
        "fetched_at": f.fetched_at,
        "as_of_ns": f.as_of_ns,
        "detail": dict(f.detail),
        "value": _value(f.value),
    }


def _value(value: Any) -> Any:
    if isinstance(value, OrderBook):
        return {
            "bids": [[str(level.price), str(level.size)] for level in value.bids],
            "asks": [[str(level.price), str(level.size)] for level in value.asks],
        }
    if isinstance(value, list | tuple) and all(isinstance(c, Candle) for c in value):
        return [
            [
                c.start_ns,
                str(c.open),
                str(c.high),
                str(c.low),
                str(c.close),
                str(c.volume),
                c.trades,
            ]
            for c in value
        ]
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"no record form for a {type(value).__name__}")


def _unfetched(row: Mapping[str, Any]) -> Fetched:
    kind, raw = str(row["kind"]), row["value"]
    value: Any
    if kind == "ohlcv":
        value = [
            Candle(
                start_ns=int(c[0]),
                open=Decimal(c[1]),
                high=Decimal(c[2]),
                low=Decimal(c[3]),
                close=Decimal(c[4]),
                volume=Decimal(c[5]),
                trades=int(c[6]),
            )
            for c in raw
        ]
    elif kind == "orderbook":
        value = OrderBook(
            bids=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in raw["bids"]),
            asks=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in raw["asks"]),
        )
    else:
        value = Decimal(str(raw))
    return Fetched(
        kind=kind,
        instrument=str(row["instrument"]),
        value=value,
        source=str(row["source"]),
        tier=str(row["tier"]),
        fetched_at=float(row["fetched_at"]),
        as_of_ns=int(row["as_of_ns"]),
        detail=dict(row.get("detail") or {}),
    )
