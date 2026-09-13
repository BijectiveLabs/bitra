"""Seven read tools over the gates. Plain functions; every caller wraps them the same way.

Paths are resolved on the machine running the tool; every number in basis points is a decimal
string; every time is UTC. A refusal from a gate is a `ValueError`, `KeyError`, `OSError` or
`RuntimeError`, which the caller turns into a message the model or the host can read.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trading_research_agent.chain import cost_floor, read_trades, validate
from trading_research_agent.chain.preregistration import from_record as prereg_from_record
from trading_research_agent.chain.validation import NullMode
from trading_research_agent.chain.validation import to_record as validation_record
from trading_research_agent.data import (
    archive_venue,
    audit,
    load_mapping,
    mapped_trades,
    open_archive_lines,
)
from trading_research_agent.envelope import load_envelope
from trading_research_agent.ledger import Ledger
from trading_research_agent.ledger.store import to_record as ledger_to_record
from trading_research_agent.trading import Ladder


def _path(text: str, *, kind: str) -> Path:
    path = Path(text).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"{kind} {text!r} does not exist")
    return path


def _decimal(text: str, label: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{label} must be a number, got {text!r}") from None
    if not value.is_finite():
        raise ValueError(f"{label} must be finite")
    return value


def cost_floor_check(envelope_path: str, magnitude_bps: str, venue: str = "") -> dict[str, Any]:
    """Is a claimed edge, in basis points, plausible against the cheapest round trip?"""
    envelope = load_envelope(_path(envelope_path, kind="envelope"))
    check = cost_floor(envelope, _decimal(magnitude_bps, "magnitude_bps"), venue=venue or None)
    c = check.channel
    return {
        "outcome": check.outcome.value,
        "plausible_magnitude_bps": str(check.plausible_magnitude_bps),
        "channel": c.key,
        "round_trip_bps": str(c.round_trip_bps),
        "margin_bps": str(check.margin_bps),
        "measured_round_trip_bps": (
            None if c.measured_round_trip_bps is None else str(c.measured_round_trip_bps)
        ),
        "stopped": check.stopped,
    }


def check_registration(registration_path: str) -> dict[str, Any]:
    """Parse a preregistration.json, verify its seal, and list its kill criteria."""
    record = json.loads(_path(registration_path, kind="registration").read_text("utf-8"))
    prereg = prereg_from_record(record)
    return {
        "name": prereg.name,
        "seal": prereg.seal(),
        "horizon": str(prereg.horizon),
        "instruments": list(prereg.universe.instruments),
        "power_required_sample": prereg.power.required_sample,
        "kill_criteria": [
            {
                "metric": k.metric,
                "comparison": k.comparison,
                "threshold": str(k.threshold),
                "verdict": k.verdict.value,
            }
            for k in prereg.kill_criteria
        ],
        "amends": prereg.amends,
    }


def audit_dataset(
    archive_path: str,
    instrument: str,
    venue: str = "",
    mapping_path: str = "",
    max_gap_minutes: int = 5,
) -> dict[str, Any]:
    """Is this trade file safe to backtest on? Give a venue plugin name or a column map."""
    archive = _path(archive_path, kind="archive")
    if bool(venue) == bool(mapping_path):
        raise ValueError("name exactly one of venue or mapping_path")
    if venue:
        source = venue
        rows = archive_venue(venue).normalise(open_archive_lines(archive), instrument=instrument)
    else:
        mapping = load_mapping(_path(mapping_path, kind="mapping"))
        source = mapping.source
        rows = mapped_trades(open_archive_lines(archive), mapping, instrument=instrument)
    report = audit(
        rows,
        max_gap=timedelta(minutes=max_gap_minutes),
        expected_venue=source,
        expected_instrument=instrument,
    )
    return {
        "passed": report.passed,
        "venue": report.venue,
        "instrument": report.instrument,
        "rows_accepted": report.rows_accepted,
        "rows_rejected": report.rows_rejected,
        "first_ts": None if report.first_ts is None else report.first_ts.isoformat(),
        "last_ts": None if report.last_ts is None else report.last_ts.isoformat(),
        "findings": [
            {"check": f.check, "severity": f.severity, "detail": f.detail} for f in report.findings
        ],
    }


def validate_trades(
    registration_path: str,
    trades_path: str,
    cost_bps: str,
    block_length: int = 7,
    n_resamples: int = 2000,
    n_perm: int = 500,
    seed: int = 0,
    null_mode: str = "sign",
) -> dict[str, Any]:
    """Run the eight validation requirements on trades.jsonl against a sealed registration."""
    registration = prereg_from_record(
        json.loads(_path(registration_path, kind="registration").read_text("utf-8"))
    )
    trades = read_trades(_path(trades_path, kind="trades"))
    cost = _decimal(cost_bps, "cost_bps")
    result = validate(
        registration,
        trades,
        lambda _trade: cost,
        block_length=block_length,
        n_resamples=n_resamples,
        n_perm=n_perm,
        seed=seed,
        null_mode=NullMode(null_mode),
    )
    return dict(validation_record(result))


def ledger_show(ledger_path: str = "ledger.jsonl") -> dict[str, Any]:
    """Every row that stands, and the count per verdict."""
    ledger = Ledger(_path(ledger_path, kind="ledger"))
    rows = ledger.effective()
    return {
        "counts": {v.value: n for v, n in ledger.counts().items()},
        "rows": [ledger_to_record(e) for e in rows],
    }


def ladder_show(ladder_path: str = "ladder.json") -> dict[str, Any]:
    """The current tier and the history of promotions and demotions."""
    ladder = Ladder(_path(ladder_path, kind="ladder"))
    return {"tier": ladder.tier.name, "history": list(ladder.history)}


def run_report(run_dir: str) -> dict[str, Any]:
    """run.json of a replay, shadow or canary run, with its closed round trips counted."""
    directory = Path(run_dir).expanduser()
    summary = _path(str(directory / "run.json"), kind="run report")
    report = json.loads(summary.read_text("utf-8"))
    trades = directory / "trades.jsonl"
    report["closed_round_trips"] = (
        sum(1 for line in trades.read_text("utf-8").splitlines() if line.strip())
        if trades.is_file()
        else 0
    )
    return dict(report)


# Name, title, function. The order is the order a host lists them in.
READ_TOOLS: tuple[tuple[str, str, Callable[..., dict[str, Any]]], ...] = (
    ("cost_floor_check", "Cost floor (layer 0)", cost_floor_check),
    ("check_registration", "Check a pre-registration (layer 3)", check_registration),
    ("audit_dataset", "Audit a dataset (data gate)", audit_dataset),
    ("validate_trades", "Validate trades (layer 5)", validate_trades),
    ("ledger_show", "Show the kill ledger", ledger_show),
    ("ladder_show", "Show the promotion ladder", ladder_show),
    ("run_report", "Read a run report", run_report),
)
