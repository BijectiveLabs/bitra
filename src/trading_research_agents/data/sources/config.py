"""`data.toml`: which sources answer for which kind of data, in what order, and how fresh.

This is the file a client edits when a provider changes, a key expires, or a free tier runs
out. Nothing here has a default chain: a kind of data the operator did not declare is a kind of
data this project will not fetch, and that is on purpose.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from trading_research_agents.data.sources.base import KINDS, SourceError
from trading_research_agents.data.sources.chain import POLICIES, SourceChain
from trading_research_agents.data.sources.registry import build

UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
EXECUTION_VENUE = "venue"
PRACTICE = ("demo", "testnet")
# The figures an order is priced and carried on. They have to be the execution venue's own: another
# venue's book is not the book the order meets, and another venue's funding is not what it pays.
PRICED_ON_THE_VENUE = ("orderbook", "funding")


class DataConfigError(ValueError):
    """The declaration cannot be read, or asks for something that does not exist."""


def parse_duration(text: str) -> float:
    """`30s`, `5m`, `2h`, `1d` to seconds. A bare number is seconds."""
    raw = str(text).strip().lower()
    if not raw:
        raise DataConfigError("a duration is not empty")
    if raw[-1] in UNITS:
        head, unit = raw[:-1], UNITS[raw[-1]]
    else:
        head, unit = raw, 1.0
    try:
        value = float(head)
    except ValueError as exc:
        raise DataConfigError(f"{text!r} is not a duration") from exc
    if value <= 0:
        raise DataConfigError(f"{text!r} is not a positive duration")
    return value * unit


@dataclass(frozen=True)
class KindSettings:
    """One `[data.<kind>]` table as written, before any source is built."""

    kind: str
    chain: tuple[str, ...]
    ttl_s: float
    on_exhausted: str
    options: Mapping[str, Any]

    def record(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "chain": list(self.chain),
            "ttl_s": self.ttl_s,
            "on_exhausted": self.on_exhausted,
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class DataConfig:
    kinds: tuple[KindSettings, ...]
    settings: Mapping[str, Any]
    path: Path | None = None

    def for_kind(self, kind: str) -> KindSettings:
        for declared in self.kinds:
            if declared.kind == kind:
                return declared
        raise DataConfigError(f"no [data.{kind}] declared in {self.path or 'the config'}")

    def on_venue(self, venue: str, environment: str = "") -> DataConfig:
        """This declaration with `venue` read as the venue orders go to.

        Public data comes from the venue's live markets; the book comes from the environment the
        orders meet, since a demo or testnet API's book is its own. A desk that names `venue`
        changes venue by changing [run].venue_key and nothing in this file.
        """
        practice = environment if environment in PRACTICE else ""
        kinds = []
        for declared in self.kinds:
            if EXECUTION_VENUE not in declared.chain:
                kinds.append(declared)
                continue
            if not venue:
                raise DataConfigError(
                    f"[data.{declared.kind}].chain names `venue`, the venue orders go to, and "
                    "there is no strategy.toml here to say which one that is"
                )
            own = f"ccxt:{venue}"
            if practice and declared.kind == "orderbook":
                own = f"{own}@{practice}"
            chain = tuple(own if name == EXECUTION_VENUE else name for name in declared.chain)
            kinds.append(replace(declared, chain=chain))
        return replace(self, kinds=tuple(kinds))

    def off_venue(self, venue: str) -> list[str]:
        """The sources that would price an order on another venue's figures, as `kind: source`.

        A ccxt source names its venue; a plugin is built and asked for its `venue`. A plugin that
        does not say is counted here too, since an order priced on a book of unknown origin is
        priced on someone else's book until shown otherwise.
        """
        found = []
        for declared in self.kinds:
            if declared.kind not in PRICED_ON_THE_VENUE:
                continue
            for name in declared.chain:
                scheme, _, target = name.partition(":")
                scheme = scheme.strip().lower()
                if name == EXECUTION_VENUE or scheme == "file":
                    continue
                if scheme == "ccxt":
                    if target.partition("@")[0] != venue:
                        found.append(f"{declared.kind}: {name}")
                    continue
                try:
                    served = str(getattr(build(name, dict(self.settings)), "venue", "") or "")
                except (KeyError, SourceError) as exc:
                    raise DataConfigError(f"[data.{declared.kind}]: {exc}") from exc
                if not served:
                    found.append(f"{declared.kind}: {name} (it does not say which venue it serves)")
                elif served.strip().lower() != venue.strip().lower():
                    found.append(f"{declared.kind}: {name} (serves {served})")
        return found

    def chain(self, kind: str) -> SourceChain:
        """Build the chain for one kind. Sources are built here, not when the file is read."""
        declared = self.for_kind(kind)
        sources = []
        for name in declared.chain:
            if name == EXECUTION_VENUE:
                raise DataConfigError(
                    f"[data.{kind}].chain names `venue`, which is read against strategy.toml"
                )
            source = build(name, dict(self.settings))
            if kind not in source.kinds():
                raise DataConfigError(f"{name} does not serve {kind}")
            sources.append(source)
        return SourceChain(
            kind=kind,
            sources=tuple(sources),
            ttl_s=declared.ttl_s,
            on_exhausted=declared.on_exhausted,
        )

    def record(self) -> dict[str, Any]:
        return {
            "kinds": [k.record() for k in self.kinds],
            "path": None if self.path is None else str(self.path),
        }


def load_data_config(path: Path) -> DataConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DataConfigError(
            f"{path}: not here; `tra init` writes one beside strategy.toml, and a cycle reads its "
            "data through it"
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DataConfigError(f"{path}: {exc}") from exc
    data = raw.get("data")
    if not isinstance(data, dict) or not data:
        raise DataConfigError(f"{path}: a [data.<kind>] table is required")
    shared = {k: v for k, v in data.items() if not isinstance(v, dict)}
    shared.setdefault("root", str(path.resolve().parent))
    if isinstance(data.get("symbols"), dict):
        shared["symbols"] = data["symbols"]
    kinds: list[KindSettings] = []
    for kind, table in data.items():
        if not isinstance(table, dict) or kind == "symbols":
            continue
        if kind not in KINDS:
            raise DataConfigError(
                f"{path}: [data.{kind}] is not a kind of data; have {list(KINDS)}"
            )
        chain = table.get("chain")
        if not isinstance(chain, list) or not chain or not all(isinstance(c, str) for c in chain):
            raise DataConfigError(f"{path}: [data.{kind}].chain is a list of source names")
        policy = str(table.get("on_exhausted", "refuse"))
        if policy not in POLICIES:
            raise DataConfigError(f"{path}: [data.{kind}].on_exhausted is one of {list(POLICIES)}")
        options = {
            k: v
            for k, v in table.items()
            if k not in ("chain", "ttl", "on_exhausted") and not isinstance(v, dict)
        }
        if isinstance(table.get("params"), dict):
            # A venue's own spelling, handed to it verbatim. Quirks belong in the operator's file,
            # not in this package: Bybit publishes open interest in buckets and wants an
            # intervalTime, and no other venue should have to know that.
            options["params"] = dict(table["params"])
        kinds.append(
            KindSettings(
                kind=kind,
                chain=tuple(str(c) for c in chain),
                ttl_s=parse_duration(str(table.get("ttl", "60s"))),
                on_exhausted=policy,
                options=options,
            )
        )
    if not kinds:
        raise DataConfigError(f"{path}: no [data.<kind>] table names a chain")
    return DataConfig(kinds=tuple(kinds), settings=shared, path=path)
