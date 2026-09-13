"""The schema the model fills and the object the pipeline gets. Form only; no decision here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_research_agent.agent.form import (
    _enum,
    _list,
    _text,
    parse_object,
)

ROLE = "auditor"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "area": {
                        "type": "string",
                        "enum": ["tokens", "latency", "trading", "replay", "agents"],
                    },
                    "severity": {"type": "string", "enum": ["info", "warn", "fail"]},
                    "detail": {"type": "string"},
                },
                "required": ["area", "severity", "detail"],
                "additionalProperties": False,
            },
        },
        "verdict": {"type": "string", "enum": ["sound", "questioned", "unsupported"]},
        "summary": {"type": "string"},
    },
    "required": ["findings", "verdict", "summary"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Auditor:
    findings: list[Any]
    verdict: str
    summary: str


def parse(text: str) -> Auditor:
    data = parse_object(text, SCHEMA)
    findings = _list(data.get("findings"), "findings")
    verdict = _enum(data.get("verdict"), "verdict", ("sound", "questioned", "unsupported"))
    summary = _text(data.get("summary"), "summary")
    return Auditor(findings=findings, verdict=verdict, summary=summary)
