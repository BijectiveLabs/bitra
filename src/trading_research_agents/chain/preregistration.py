"""Layer 3: the pre-registration.

Writing down the metric, the horizon, the universe, the era, the cost model, the power and the
kill thresholds before seeing any data removes the freedom to move the goalposts afterwards.
That is the whole mechanism. A threshold written as "clearly positive" is not a threshold; the
form required here is a numeric bound and the verdict it fires.

A registration may be amended, narrowly: an instrument corrected, a threshold tightened, a
measurement recorded as unavailable. It may never loosen a threshold or move a horizon, and
both the original and the amendment are kept.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from trading_research_agent.ledger.vocabulary import Verdict

LAYER = 3

COST_TERMS: tuple[str, ...] = ("fee", "spread_cost", "slippage", "funding_accrual")
KILL_VERDICTS: frozenset[Verdict] = frozenset({Verdict.FALSIFIED, Verdict.NOT_PROVEN})
COMPARISONS: tuple[str, ...] = ("<", "<=", ">", ">=")


class PreregistrationError(ValueError):
    """Raised when a registration or an amendment breaks a rule."""


@dataclass(frozen=True)
class KillCriterion:
    """A metric, a numeric bound, and the verdict that fires when the bound is crossed."""

    metric: str
    comparison: str
    threshold: Decimal
    verdict: Verdict

    def __post_init__(self) -> None:
        if not self.metric.strip():
            raise PreregistrationError("a kill criterion names its metric")
        if self.comparison not in COMPARISONS:
            raise PreregistrationError(f"unsupported comparison {self.comparison!r}")
        if not isinstance(self.threshold, Decimal):
            raise PreregistrationError("a threshold is a Decimal, not a float")
        if self.verdict not in KILL_VERDICTS:
            raise PreregistrationError(
                f"a kill criterion fires FALSIFIED or NOT PROVEN, not {self.verdict.value}"
            )

    def is_at_least_as_strict_as(self, other: KillCriterion) -> bool:
        """True when every value that fired `other` also fires this criterion.

        Tightening means the kill region grows. For a lower bound that is a higher threshold;
        for an upper bound, a lower one. Changing the metric, the direction or the verdict is
        not a tightening, it is a different criterion.
        """
        if self.metric != other.metric or self.verdict is not other.verdict:
            return False
        if self.comparison[0] != other.comparison[0]:
            return False
        if self.comparison[0] == "<":
            if self.threshold > other.threshold:
                return True
            return self.threshold == other.threshold and (
                self.comparison == "<=" or other.comparison == "<"
            )
        if self.threshold < other.threshold:
            return True
        return self.threshold == other.threshold and (
            self.comparison == ">=" or other.comparison == ">"
        )


@dataclass(frozen=True)
class CostTerm:
    name: str
    source: str

    def __post_init__(self) -> None:
        if self.name not in COST_TERMS:
            raise PreregistrationError(
                f"unknown cost term {self.name!r}; expected one of {COST_TERMS}"
            )
        if not self.source.strip():
            raise PreregistrationError(f"cost term {self.name!r} needs a source")


@dataclass(frozen=True)
class CostModel:
    """Every term of the round trip, each with the source it will be measured from."""

    terms: tuple[CostTerm, ...]

    def __post_init__(self) -> None:
        names = [t.name for t in self.terms]
        missing = [n for n in COST_TERMS if n not in names]
        if missing:
            # Net from day one. A model that omits a term has a term equal to zero, silently.
            raise PreregistrationError(f"cost model is missing {', '.join(missing)}")
        if len(set(names)) != len(names):
            raise PreregistrationError("cost model repeats a term")


@dataclass(frozen=True)
class SealedEra:
    name: str
    start: date
    end: date | None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise PreregistrationError("a sealed era needs a name")
        if self.end is not None and self.end < self.start:
            raise PreregistrationError(f"sealed era {self.name!r} ends before it starts")


@dataclass(frozen=True)
class Era:
    """The research range, and the held-out ranges that stay sealed until a single gate."""

    research_start: date
    research_end: date
    sealed: tuple[SealedEra, ...]

    def __post_init__(self) -> None:
        if self.research_end < self.research_start:
            raise PreregistrationError("research era ends before it starts")
        if not self.sealed:
            raise PreregistrationError("at least one held-out era is sealed before research begins")
        for s in self.sealed:
            end = s.end or date.max
            if s.start <= self.research_end and end >= self.research_start:
                raise PreregistrationError(f"sealed era {s.name!r} overlaps the research era")


@dataclass(frozen=True)
class Universe:
    instruments: tuple[str, ...]
    rule: str

    def __post_init__(self) -> None:
        if not self.instruments:
            raise PreregistrationError("a universe names at least one instrument")
        if len(set(self.instruments)) != len(self.instruments):
            raise PreregistrationError("a universe lists each instrument once")
        if not self.rule.strip():
            raise PreregistrationError("a universe states the rule that produced it")


@dataclass(frozen=True)
class Power:
    """The sample required to detect the claimed effect, declared before the test runs."""

    required_sample: int
    effect_bps: Decimal
    basis: str

    def __post_init__(self) -> None:
        if self.required_sample <= 0:
            raise PreregistrationError("required sample is a positive count")
        if not isinstance(self.effect_bps, Decimal):
            raise PreregistrationError("effect size is a Decimal, in basis points")
        if self.effect_bps <= 0:
            raise PreregistrationError("the claimed effect is a positive magnitude")
        if not self.basis.strip():
            raise PreregistrationError("power states how the sample size was derived")


@dataclass(frozen=True)
class Preregistration:
    """The nine elements. Not valid until all nine are present and well formed."""

    name: str
    mechanism: str
    claim: str
    metric: str
    horizon: timedelta
    universe: Universe
    era: Era
    cost_model: CostModel
    power: Power
    kill_criteria: tuple[KillCriterion, ...]
    registered_at: datetime
    amends: str | None = None
    amendment_reason: str = ""

    def __post_init__(self) -> None:
        for label in ("name", "mechanism", "claim", "metric"):
            if not getattr(self, label).strip():
                raise PreregistrationError(f"{label} is required")
        _one_sentence(self.mechanism)
        if self.horizon <= timedelta(0):
            raise PreregistrationError("horizon is a positive duration")
        if not self.kill_criteria:
            raise PreregistrationError("at least one numeric kill criterion is required")
        offset = self.registered_at.utcoffset()
        if offset is None or offset != UTC.utcoffset(None):
            raise PreregistrationError("registered_at must be timezone-aware, in UTC")
        if self.amends is not None and not self.amendment_reason.strip():
            raise PreregistrationError("an amendment states why")
        if self.amends is None and self.amendment_reason.strip():
            raise PreregistrationError("only an amendment carries an amendment reason")

    def seal(self) -> str:
        """SHA-256 of the canonical registration, so a later reader can prove nothing moved."""
        payload = json.dumps(_canonical(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("ascii")).hexdigest()


def validate_mechanism(text: str) -> None:
    """The form of a mechanism memo: one sentence, on one line, long enough to name a party.

    Only the form. Whether the sentence names somebody who is structurally forced to lose is
    a question for a reader, or for the data at layer 5; it is not decided here.
    """
    _one_sentence(text)


def _one_sentence(text: str) -> None:
    body = text.strip()
    if "\n" in body:
        raise PreregistrationError("the mechanism memo is one sentence, on one line")
    if ". " in body.rstrip("."):
        raise PreregistrationError("the mechanism memo is one sentence")
    if len(body.split()) < 6:
        raise PreregistrationError("the mechanism memo names who is forced to lose, and why")


def _canonical(p: Preregistration) -> dict[str, Any]:
    return {
        "name": p.name,
        "mechanism": p.mechanism,
        "claim": p.claim,
        "metric": p.metric,
        "horizon_seconds": p.horizon.total_seconds(),
        "universe": {"instruments": list(p.universe.instruments), "rule": p.universe.rule},
        "era": {
            "research_start": p.era.research_start.isoformat(),
            "research_end": p.era.research_end.isoformat(),
            "sealed": [
                {
                    "name": s.name,
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat() if s.end else None,
                }
                for s in p.era.sealed
            ],
        },
        "cost_model": [{"name": t.name, "source": t.source} for t in p.cost_model.terms],
        "power": {
            "required_sample": p.power.required_sample,
            "effect_bps": str(p.power.effect_bps),
            "basis": p.power.basis,
        },
        "kill_criteria": [
            {
                "metric": k.metric,
                "comparison": k.comparison,
                "threshold": str(k.threshold),
                "verdict": k.verdict.value,
            }
            for k in p.kill_criteria
        ],
        "registered_at": p.registered_at.isoformat(),
        "amends": p.amends,
        "amendment_reason": p.amendment_reason,
    }


def to_record(p: Preregistration) -> dict[str, Any]:
    record = _canonical(p)
    record["schema"] = 1
    record["seal"] = p.seal()
    return record


def from_record(record: dict[str, Any]) -> Preregistration:
    if record.get("schema") != 1:
        raise PreregistrationError(f"unsupported preregistration schema {record.get('schema')!r}")
    p = Preregistration(
        name=record["name"],
        mechanism=record["mechanism"],
        claim=record["claim"],
        metric=record["metric"],
        horizon=timedelta(seconds=record["horizon_seconds"]),
        universe=Universe(tuple(record["universe"]["instruments"]), record["universe"]["rule"]),
        era=Era(
            research_start=date.fromisoformat(record["era"]["research_start"]),
            research_end=date.fromisoformat(record["era"]["research_end"]),
            sealed=tuple(
                SealedEra(
                    s["name"],
                    date.fromisoformat(s["start"]),
                    date.fromisoformat(s["end"]) if s.get("end") else None,
                )
                for s in record["era"]["sealed"]
            ),
        ),
        cost_model=CostModel(tuple(CostTerm(t["name"], t["source"]) for t in record["cost_model"])),
        power=Power(
            required_sample=int(record["power"]["required_sample"]),
            effect_bps=Decimal(record["power"]["effect_bps"]),
            basis=record["power"]["basis"],
        ),
        kill_criteria=tuple(
            KillCriterion(
                k["metric"], k["comparison"], Decimal(k["threshold"]), Verdict(k["verdict"])
            )
            for k in record["kill_criteria"]
        ),
        registered_at=datetime.fromisoformat(record["registered_at"]),
        amends=record.get("amends"),
        amendment_reason=record.get("amendment_reason", ""),
    )
    if "seal" in record and record["seal"] != p.seal():
        # The file says one thing and its own hash says another. Somebody edited it in place.
        raise PreregistrationError("seal does not match the registration; it was altered")
    return p


def amend(
    original: Preregistration,
    *,
    amended_at: datetime,
    reason: str,
    kill_criteria: tuple[KillCriterion, ...] | None = None,
    cost_model: CostModel | None = None,
    universe: Universe | None = None,
) -> Preregistration:
    """A dated registration that supersedes nothing: the original stays, this one points at it.

    Permitted: tightening a kill threshold, adding a criterion, correcting a cost-term source,
    correcting the universe. Refused: loosening or dropping a criterion, changing the horizon,
    the metric, the claim or the mechanism. Those are a new hypothesis, not an amendment.
    """
    if not reason.strip():
        raise PreregistrationError("an amendment states why")
    new_criteria = original.kill_criteria if kill_criteria is None else kill_criteria
    for old in original.kill_criteria:
        matches = [k for k in new_criteria if k.metric == old.metric and k.verdict is old.verdict]
        if not any(k.is_at_least_as_strict_as(old) for k in matches):
            raise PreregistrationError(
                f"amendment loosens or drops the criterion on {old.metric!r}; "
                "a threshold may be tightened after data is seen, never loosened"
            )
    return replace(
        original,
        kill_criteria=new_criteria,
        cost_model=original.cost_model if cost_model is None else cost_model,
        universe=original.universe if universe is None else universe,
        registered_at=amended_at,
        amends=original.seal(),
        amendment_reason=reason,
    )
