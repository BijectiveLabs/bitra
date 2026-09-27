"""The promotion ladder. Up slowly, with evidence and a signature; down instantly, without either.

    T0 research  ->  T1 paper  ->  T2 shadow  ->  T3 canary  ->  T4 production

A strategy does not reach a tier because somebody is confident about it. Each step up needs a
receipt naming the evidence (a layer-5 verdict on the trades the previous tier produced) and an
operator. A step down needs only a reason, and it happens the moment a control says so. The
asymmetry is the design. Canary is reachable when a live venue plugin exists; production is
not reachable in this release, and the ladder says so rather than pretending.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import Any


class Tier(IntEnum):
    RESEARCH = 0
    PAPER = 1
    SHADOW = 2
    CANARY = 3
    PRODUCTION = 4


LIVE_TIERS = frozenset({Tier.CANARY, Tier.PRODUCTION})


class LadderError(RuntimeError):
    """The ladder refuses the move."""


@dataclass(frozen=True)
class Receipt:
    """Why a promotion was allowed: the evidence it rests on and who signed it."""

    from_tier: Tier
    to_tier: Tier
    evidence: str
    signed_by: str
    at: datetime


@dataclass(frozen=True)
class Demotion:
    from_tier: Tier
    to_tier: Tier
    reason: str
    at: datetime


class Ladder:
    """State on disk, rewritten whole on every change. A ladder with no file is at T0."""

    def __init__(
        self, path: Path, *, live_available: bool = False, production_available: bool = False
    ) -> None:
        self.path = path
        self.production_available = production_available
        self.live_available = live_available
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"tier": int(Tier.RESEARCH), "history": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise LadderError(f"{self.path}: unreadable ladder state: {exc}") from exc
        if not isinstance(data, dict) or "tier" not in data:
            raise LadderError(f"{self.path}: ladder state is not a ladder")
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    @property
    def tier(self) -> Tier:
        return Tier(int(self._state["tier"]))

    @property
    def history(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._state.get("history", []))

    def promote(self, *, evidence: str, signed_by: str, at: datetime | None = None) -> Receipt:
        """One step up. Needs evidence, a signature, and a venue that exists for the tier."""
        if not evidence.strip():
            raise LadderError("a promotion names its evidence")
        if not signed_by.strip():
            raise LadderError("a promotion is signed by the operator who allowed it")
        current = self.tier
        if current is Tier.PRODUCTION:
            raise LadderError("already at production")
        target = Tier(current + 1)
        if target in LIVE_TIERS and not self.live_available:
            raise LadderError(
                f"{target.name} needs a live venue plugin, and none is registered; the ladder "
                f"stops at {Tier.SHADOW.name}"
            )
        if target is Tier.PRODUCTION and not self.production_available:
            raise LadderError(
                "PRODUCTION is not reachable in this release; it needs the operational review "
                "a canary session is meant to produce"
            )
        when = at or datetime.now(UTC)
        _utc(when)
        receipt = Receipt(current, target, evidence.strip(), signed_by.strip(), when)
        self._state["tier"] = int(target)
        self._state.setdefault("history", []).append(
            {
                "kind": "promotion",
                "from": current.name,
                "to": target.name,
                "evidence": receipt.evidence,
                "signed_by": receipt.signed_by,
                "at": when.isoformat(),
            }
        )
        self._save()
        return receipt

    def demote(
        self, *, reason: str, to: Tier | None = None, at: datetime | None = None
    ) -> Demotion:
        """Any number of steps down, on a reason alone. Never refused for lack of a signature."""
        if not reason.strip():
            raise LadderError("a demotion states its reason")
        current = self.tier
        if current is Tier.RESEARCH:
            raise LadderError("already at research; nothing below it")
        target = Tier(current - 1) if to is None else to
        if target >= current:
            raise LadderError("a demotion goes down")
        when = at or datetime.now(UTC)
        _utc(when)
        demotion = Demotion(current, target, reason.strip(), when)
        self._state["tier"] = int(target)
        self._state.setdefault("history", []).append(
            {
                "kind": "demotion",
                "from": current.name,
                "to": target.name,
                "reason": demotion.reason,
                "at": when.isoformat(),
            }
        )
        self._save()
        return demotion


def _utc(when: datetime) -> None:
    if when.utcoffset() is None or when.utcoffset() != UTC.utcoffset(None):
        raise LadderError("timestamps on the ladder are UTC")
