"""What a dataset is, where it came from, and what the audit said. Written beside the data."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_research_agent.data.audit import AuditReport, Finding

SCHEMA = 1


class ManifestError(ValueError):
    """The manifest cannot be read as one this version wrote."""


@dataclass(frozen=True)
class Source:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Manifest:
    venue: str
    instrument: str
    first_ts: datetime
    last_ts: datetime
    rows_accepted: int
    rows_rejected: int
    sources: tuple[Source, ...]
    normaliser_version: str
    audit_passed: bool
    findings: tuple[Finding, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        for label, value in (
            ("first_ts", self.first_ts),
            ("last_ts", self.last_ts),
            ("created_at", self.created_at),
        ):
            offset = value.utcoffset()
            if offset is None or offset != UTC.utcoffset(None):
                raise ManifestError(f"{label} must be timezone-aware, in UTC")
        if self.last_ts < self.first_ts:
            raise ManifestError("last_ts is before first_ts")
        if not self.sources:
            raise ManifestError("a manifest names at least one source file")


def manifest_from_audit(
    report: AuditReport,
    *,
    sources: tuple[Source, ...],
    normaliser_version: str,
    created_at: datetime,
) -> Manifest:
    if report.venue is None or report.instrument is None:
        raise ManifestError("audit report names no venue or instrument; nothing to describe")
    first, last = report.first_ts, report.last_ts
    if first is None or last is None:
        raise ManifestError(
            "audit report covers no trades; a manifest describes data, not its absence"
        )
    return Manifest(
        venue=report.venue,
        instrument=report.instrument,
        first_ts=first,
        last_ts=last,
        rows_accepted=report.rows_accepted,
        rows_rejected=report.rows_rejected,
        sources=sources,
        normaliser_version=normaliser_version,
        audit_passed=report.passed,
        findings=report.findings,
        created_at=created_at,
    )


def to_record(m: Manifest) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "venue": m.venue,
        "instrument": m.instrument,
        "first_ts": m.first_ts.isoformat(),
        "last_ts": m.last_ts.isoformat(),
        "rows_accepted": m.rows_accepted,
        "rows_rejected": m.rows_rejected,
        "sources": [{"path": s.path, "sha256": s.sha256, "size": s.size} for s in m.sources],
        "normaliser_version": m.normaliser_version,
        "audit": {
            "passed": m.audit_passed,
            "findings": [
                {"check": f.check, "severity": f.severity, "detail": f.detail} for f in m.findings
            ],
        },
        "created_at": m.created_at.isoformat(),
    }


def from_record(record: dict[str, Any]) -> Manifest:
    if record.get("schema") != SCHEMA:
        raise ManifestError(f"unsupported manifest schema {record.get('schema')!r}")
    try:
        return Manifest(
            venue=record["venue"],
            instrument=record["instrument"],
            first_ts=datetime.fromisoformat(record["first_ts"]),
            last_ts=datetime.fromisoformat(record["last_ts"]),
            rows_accepted=int(record["rows_accepted"]),
            rows_rejected=int(record["rows_rejected"]),
            sources=tuple(
                Source(s["path"], s["sha256"], int(s["size"])) for s in record["sources"]
            ),
            normaliser_version=record["normaliser_version"],
            audit_passed=bool(record["audit"]["passed"]),
            findings=tuple(
                Finding(f["check"], f["severity"], f["detail"]) for f in record["audit"]["findings"]
            ),
            created_at=datetime.fromisoformat(record["created_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError(f"manifest is incomplete or malformed: {exc}") from exc


def write_manifest(path: Path, m: Manifest) -> None:
    path.write_text(json.dumps(to_record(m), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_manifest(path: Path) -> Manifest:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: not valid JSON: {exc}") from exc
    return from_record(record)
