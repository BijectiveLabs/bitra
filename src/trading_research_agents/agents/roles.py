"""The three language-model roles, each one call, each returning a typed object or a RoleError.

A RoleError is a defect of form: the response did not fill the schema, or filled it with
something the gate cannot use. It is returned to the model as text for a revision. Nothing in
this module decides whether an idea lives or dies; that is the pipeline's job, and the gates'.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from importlib import resources
from typing import Any

from trading_research_agents.chain.preregistration import (
    COST_TERMS,
    CostModel,
    CostTerm,
    Era,
    KillCriterion,
    Power,
    Preregistration,
    SealedEra,
    Universe,
)
from trading_research_agents.ledger.vocabulary import Verdict
from trading_research_agents.llm.form import FormError

# One class for both halves of the same failure: a response that does not fit the schema and
# a response that fits it but says something a role may not say.
RoleError = FormError


def prompt(name: str) -> str:
    return (
        resources.files("trading_research_agents.agents.prompts")
        .joinpath(f"{name}.md")
        .read_text(encoding="ascii")
    )


def _parse(text: str, required: tuple[str, ...]) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RoleError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RoleError("response must be a JSON object")
    missing = [k for k in required if k not in data]
    if missing:
        raise RoleError(f"response lacks {', '.join(missing)}")
    return data


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise RoleError(f"{label} must be a decimal string")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise RoleError(f"{label} is not a number: {value!r}") from exc


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoleError(f"{label} must be a non-empty string")
    return value.strip()


# Layer 0: the bound


class BoundBasis(StrEnum):
    COMPARABLE = "comparable"
    DISPERSION = "dispersion"
    MECHANISM_SCALE = "mechanism_scale"
    DECLARED = "declared"
    NONE = "none"


@dataclass(frozen=True)
class Bound:
    magnitude_bps: Decimal | None
    basis: BoundBasis
    explanation: str

    @property
    def usable(self) -> bool:
        return self.magnitude_bps is not None and self.basis is not BoundBasis.NONE


BOUND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "magnitude_bps": {"type": "string"},
        "basis": {
            "type": "string",
            "enum": ["comparable", "dispersion", "mechanism_scale", "none"],
        },
        "explanation": {"type": "string"},
    },
    "required": ["magnitude_bps", "basis", "explanation"],
    "additionalProperties": False,
}


def parse_bound(text: str) -> Bound:
    data = _parse(text, ("magnitude_bps", "basis", "explanation"))
    try:
        basis = BoundBasis(str(data["basis"]))
    except ValueError as exc:
        raise RoleError(
            "basis must be one of comparable, dispersion, mechanism_scale, none"
        ) from exc
    if basis is BoundBasis.DECLARED:
        raise RoleError("basis 'declared' is reserved for the operator")
    explanation = _text(data["explanation"], "explanation")
    if basis is BoundBasis.NONE:
        return Bound(magnitude_bps=None, basis=basis, explanation=explanation)
    magnitude = _decimal(data["magnitude_bps"], "magnitude_bps")
    if magnitude <= 0:
        raise RoleError("magnitude_bps must be a positive number of basis points")
    return Bound(magnitude_bps=magnitude, basis=basis, explanation=explanation)


# Layer 2: the memo and its critique


@dataclass(frozen=True)
class Memo:
    mechanism: str
    forced_loser: str
    forcing_structure: str


MEMO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mechanism": {"type": "string"},
        "forced_loser": {"type": "string"},
        "forcing_structure": {"type": "string"},
    },
    "required": ["mechanism", "forced_loser", "forcing_structure"],
    "additionalProperties": False,
}


def parse_memo(text: str) -> Memo:
    data = _parse(text, ("mechanism", "forced_loser", "forcing_structure"))
    return Memo(
        mechanism=_text(data["mechanism"], "mechanism"),
        forced_loser=_text(data["forced_loser"], "forced_loser"),
        forcing_structure=_text(data["forcing_structure"], "forcing_structure"),
    )


@dataclass(frozen=True)
class Critique:
    accepted: bool
    objection: str


CRITIQUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"accepted": {"type": "boolean"}, "objection": {"type": "string"}},
    "required": ["accepted", "objection"],
    "additionalProperties": False,
}


def parse_critique(text: str) -> Critique:
    data = _parse(text, ("accepted", "objection"))
    if not isinstance(data["accepted"], bool):
        raise RoleError("accepted must be true or false")
    objection = data["objection"] if isinstance(data["objection"], str) else ""
    if not data["accepted"] and not objection.strip():
        raise RoleError("a rejection states its objection")
    return Critique(accepted=data["accepted"], objection=objection.strip())


# Layer 3: the registration draft

REGISTRATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "claim": {"type": "string"},
        "metric": {"type": "string"},
        "horizon_seconds": {"type": "integer"},
        "universe": {
            "type": "object",
            "properties": {
                "instruments": {"type": "array", "items": {"type": "string"}},
                "rule": {"type": "string"},
            },
            "required": ["instruments", "rule"],
            "additionalProperties": False,
        },
        "era": {
            "type": "object",
            "properties": {
                "research_start": {"type": "string"},
                "research_end": {"type": "string"},
                "sealed": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "start": {"type": "string"},
                            "end": {"type": ["string", "null"]},
                        },
                        "required": ["name", "start", "end"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["research_start", "research_end", "sealed"],
            "additionalProperties": False,
        },
        "cost_model": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": list(COST_TERMS)},
                    "source": {"type": "string"},
                },
                "required": ["name", "source"],
                "additionalProperties": False,
            },
        },
        "power": {
            "type": "object",
            "properties": {
                "required_sample": {"type": "integer"},
                "effect_bps": {"type": "string"},
                "basis": {"type": "string"},
            },
            "required": ["required_sample", "effect_bps", "basis"],
            "additionalProperties": False,
        },
        "kill_criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string"},
                    "comparison": {"type": "string", "enum": ["<", "<=", ">", ">="]},
                    "threshold": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["FALSIFIED", "NOT PROVEN"]},
                },
                "required": ["metric", "comparison", "threshold", "verdict"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "name",
        "claim",
        "metric",
        "horizon_seconds",
        "universe",
        "era",
        "cost_model",
        "power",
        "kill_criteria",
    ],
    "additionalProperties": False,
}


def _date(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(_text(value, label))
    except ValueError as exc:
        raise RoleError(f"{label} must be a date, YYYY-MM-DD") from exc


def _int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoleError(f"{label} must be an integer")
    return value


def parse_registration(text: str, *, mechanism: str, registered_at: datetime) -> Preregistration:
    """Build the sealed object from the draft. Validation errors come back as RoleError text."""
    d = _parse(text, tuple(REGISTRATION_SCHEMA["required"]))
    try:
        universe = Universe(
            tuple(_text(i, "instrument") for i in _list(d["universe"], "instruments")),
            _text(_field(d["universe"], "rule"), "universe.rule"),
        )
        era_raw = d["era"]
        sealed = tuple(
            SealedEra(
                _text(_field(s, "name"), "sealed era name"),
                _date(_field(s, "start"), "sealed era start"),
                None if _field(s, "end") is None else _date(_field(s, "end"), "sealed era end"),
            )
            for s in _list(era_raw, "sealed")
        )
        era = Era(
            _date(_field(era_raw, "research_start"), "research_start"),
            _date(_field(era_raw, "research_end"), "research_end"),
            sealed,
        )
        cost_model = CostModel(
            tuple(
                CostTerm(
                    _text(_field(t, "name"), "cost term"), _text(_field(t, "source"), "source")
                )
                for t in _list(d, "cost_model")
            )
        )
        power_raw = d["power"]
        power = Power(
            _int(_field(power_raw, "required_sample"), "required_sample"),
            _decimal(_field(power_raw, "effect_bps"), "effect_bps"),
            _text(_field(power_raw, "basis"), "power.basis"),
        )
        criteria = tuple(
            KillCriterion(
                _text(_field(k, "metric"), "criterion metric"),
                _text(_field(k, "comparison"), "comparison"),
                _decimal(_field(k, "threshold"), "threshold"),
                _verdict(_field(k, "verdict")),
            )
            for k in _list(d, "kill_criteria")
        )
        return Preregistration(
            name=_text(d["name"], "name"),
            mechanism=mechanism,
            claim=_text(d["claim"], "claim"),
            metric=_text(d["metric"], "metric"),
            horizon=timedelta(seconds=_int(d["horizon_seconds"], "horizon_seconds")),
            universe=universe,
            era=era,
            cost_model=cost_model,
            power=power,
            kill_criteria=criteria,
            registered_at=registered_at,
        )
    except ValueError as exc:
        # PreregistrationError and RoleError are both ValueErrors; the text is the feedback.
        raise RoleError(str(exc)) from exc


def _field(obj: Any, key: str) -> Any:
    if not isinstance(obj, Mapping) or key not in obj:
        raise RoleError(f"missing field {key!r}")
    return obj[key]


def _list(obj: Any, key: str) -> list[Any]:
    value = _field(obj, key)
    if not isinstance(value, list):
        raise RoleError(f"{key} must be a list")
    return value


def _verdict(value: Any) -> Verdict:
    try:
        return Verdict(str(value))
    except ValueError as exc:
        raise RoleError("verdict must be FALSIFIED or NOT PROVEN") from exc
