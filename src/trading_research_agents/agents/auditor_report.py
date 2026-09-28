"""What an auditor may say, and what code does with it before anyone reads it.

Both auditors answer in the same shape: findings, each one tied to figures from the brief, each one
carrying a proposal, and a proposal may name one configuration key with a value to change it to.
Nothing is applied. An auditor writes a report; a person decides.

Two checks run over every finding before it reaches the report. Each citation has to name a figure
the brief actually carried, with its value - the same rule the validators are held to. And a
proposed change has to name a table the project's own configuration has, so an auditor cannot
propose editing something that does not exist. What fails either check is discarded and the reason
is kept beside the report, because a discarded finding is itself worth seeing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from trading_research_agents.agents.cited import cites
from trading_research_agents.llm.form import _enum, _text, parse_object

KEY = re.compile(r"^\[(?P<table>[a-z_]+(?:\.[a-z_<>]+)*)\](?:\.(?P<field>[a-z_]+))?$")
CONFIDENCE = ("low", "medium", "high")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "nothing_to_report": {"type": "boolean"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "what": {"type": "string"},
                    "cites": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "figure": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["figure", "value"],
                            "additionalProperties": False,
                        },
                    },
                    "proposal": {"type": "string"},
                    "change": {
                        "type": ["object", "null"],
                        "properties": {
                            "key": {"type": "string"},
                            "from": {"type": "string"},
                            "to": {"type": "string"},
                        },
                        "required": ["key", "from", "to"],
                        "additionalProperties": False,
                    },
                    "confidence": {"type": "string", "enum": list(CONFIDENCE)},
                },
                "required": ["what", "cites", "proposal", "change", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "nothing_to_report", "findings"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Change:
    key: str
    from_value: str
    to_value: str

    def record(self) -> dict[str, Any]:
        return {"key": self.key, "from": self.from_value, "to": self.to_value}


@dataclass(frozen=True)
class Finding:
    what: str
    cites: tuple[tuple[str, str], ...]
    proposal: str
    confidence: str
    change: Change | None = None

    def record(self) -> dict[str, Any]:
        return {
            "what": self.what,
            "cites": [{"figure": f, "value": v} for f, v in self.cites],
            "proposal": self.proposal,
            "change": None if self.change is None else self.change.record(),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class Report:
    role: str
    summary: str
    nothing_to_report: bool
    findings: tuple[Finding, ...] = ()
    discarded: tuple[str, ...] = ()

    def record(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "summary": self.summary,
            "nothing_to_report": self.nothing_to_report,
            "findings": [f.record() for f in self.findings],
            "discarded": list(self.discarded),
        }


def parse(text: str, *, role: str) -> Report:
    """The answer as written, before any of it is checked."""
    data = parse_object(text, SCHEMA)
    found = data.get("findings")
    if not isinstance(found, list):
        raise ValueError("findings must be a list")
    return Report(
        role=role,
        summary=_text(data.get("summary"), "summary"),
        nothing_to_report=bool(data.get("nothing_to_report")),
        findings=tuple(_finding(row, number) for number, row in enumerate(found, start=1)),
    )


def _finding(row: Any, number: int) -> Finding:
    if not isinstance(row, dict):
        raise ValueError(f"finding {number} must be an object")
    raw = row.get("cites")
    if not isinstance(raw, list):
        raise ValueError(f"finding {number} cites a list of figures")
    change = row.get("change")
    return Finding(
        what=_text(row.get("what"), f"finding {number} what"),
        cites=tuple(
            (
                _text(c.get("figure"), f"finding {number} figure"),
                _text(c.get("value"), f"finding {number} value"),
            )
            for c in raw
            if isinstance(c, dict)
        ),
        proposal=_text(row.get("proposal"), f"finding {number} proposal"),
        confidence=_enum(row.get("confidence"), f"finding {number} confidence", CONFIDENCE),
        change=(
            None
            if change is None
            else Change(
                key=_text(change.get("key"), f"finding {number} change key"),
                from_value=str(change.get("from", "")),
                to_value=str(change.get("to", "")),
            )
        ),
    )


def checked(
    report: Report,
    *,
    figures: Mapping[str, str],
    tables: Sequence[str] = (),
) -> Report:
    """Keep the findings that cite real figures and propose changes to keys that exist."""
    kept: list[Finding] = []
    discarded: list[str] = []
    for finding in report.findings:
        if not finding.cites:
            discarded.append(f"{finding.what!r} cites no figure")
            continue
        wrong = [cites(f, v, figures) for f, v in finding.cites]
        bad = [w for w in wrong if w]
        if bad:
            discarded.append(f"{finding.what!r}: {'; '.join(bad)}")
            continue
        if finding.change is not None:
            problem = _key_problem(finding.change.key, tables)
            if problem:
                discarded.append(f"{finding.what!r}: {problem}")
                continue
        kept.append(finding)
    return Report(
        role=report.role,
        summary=report.summary,
        nothing_to_report=report.nothing_to_report or not kept,
        findings=tuple(kept),
        discarded=tuple(discarded),
    )


def _key_problem(key: str, tables: Sequence[str]) -> str:
    match = KEY.match(key.strip())
    if match is None:
        return f"{key!r} is not a configuration key; write it as [table].field"
    table = match.group("table").split(".", 1)[0]
    if tables and table not in tables:
        return f"[{table}] is not a table in this project's configuration"
    return ""
