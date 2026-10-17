"""Ten read tools over the gates. Plain functions; every caller wraps them the same way.

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

from trading_research_agents.chain import (
    cost_floor,
    load_observation,
    observe,
    read_trades,
    validate,
)
from trading_research_agents.chain.preregistration import from_record as prereg_from_record
from trading_research_agents.chain.validation import NullMode
from trading_research_agents.chain.validation import to_record as validation_record
from trading_research_agents.data import (
    archive_venue,
    audit,
    load_mapping,
    mapped_trades,
    open_archive_lines,
)
from trading_research_agents.envelope import load_envelope
from trading_research_agents.ledger import Ledger
from trading_research_agents.ledger.store import to_record as ledger_to_record
from trading_research_agents.trading import Ladder


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
    """Check whether a claimed edge can pay for trading it, before any data is touched.

    Reads the fee channels declared in envelope.toml, takes the cheapest round trip (on `venue`
    when given, else across every channel) and compares the claimed magnitude with it. Use it
    first: an idea whose edge is below the round trip stops here, and no backtest can rescue it.
    Read-only. Returns `outcome` ("cleared" or "stop"), the channel used, its round trip and the
    margin (magnitude minus round trip) in bps as decimal strings, and the measured round trip
    when the channel records one.
    """
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
    """Read a sealed pre-registration: its hypothesis, universe, power and kill criteria.

    Use it before `validate_trades`, to see what a validation will be judged against, or to
    confirm that a registration has not been edited since it was sealed: the seal is recomputed
    from the file. Read-only. Returns the name, the seal, the horizon, the instruments, the sample
    the power analysis requires, each kill criterion (metric, comparison, threshold, verdict) and
    the registration it amends, if any. A file that does not parse is refused with the reason.
    """
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


def observe_events(dataset_path: str, observation_path: str) -> dict[str, Any]:
    """Layer 1: count whether the event declared in observation.toml recurs in a dataset.

    Marks every occurrence of the declared event in a Parquet trade dataset, measures the
    response over the declared horizon and counts it per subperiod, so an event that happened in
    one week only is seen as such. Use it after `cost_floor_check` and before writing a
    pre-registration. Read-only. Returns the declaration and the report: events found, their
    spread across subperiods and whether the declared recurrence holds.
    """
    from trading_research_agents.data.parquet import read_trades as read_prints

    dataset = _path(dataset_path, kind="dataset")
    observation = load_observation(_path(observation_path, kind="observation"))
    return observe(observation, read_prints(dataset), dataset=str(dataset)).record()


def audit_dataset(
    archive_path: str,
    instrument: str,
    venue: str = "",
    mapping_path: str = "",
    max_gap_minutes: int = 5,
) -> dict[str, Any]:
    """Audit a raw trade archive before anything is tested on it.

    Normalises the file through a venue plugin (`venue`) or a column map (`mapping_path`), exactly
    one of the two, and checks it: rows that do not become trades, gaps longer than
    `max_gap_minutes`, price and size scales that change inside the file, and days that carry most
    of the rows. Use it on any new dataset; `observe_events` and a backtest assume a file that
    passed. Read-only. Returns `passed`, accepted and rejected row counts, the first and last
    timestamps, and each finding with its severity ("fail" or "warn").
    """
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
    """Layer 5: judge a set of closed trades against the registration sealed before them.

    Rejects trades outside the registered universe or era, subtracts `cost_bps` from every trade,
    then runs the eight requirements of layer 5, among them a block bootstrap interval, a
    permutation test against a null (`null_mode` "sign" or "cross_sectional") and the power
    achieved against the power required, and applies the registration's kill criteria.
    Deterministic for a given `seed`. Use it once per registration; running it again with other
    settings is the forking the seal exists to prevent. Read-only: the verdict is returned, never
    written to the ledger.
    """
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
    """List the kill ledger: every verdict that stands, and the count per verdict.

    The ledger is the research memory: what was falsified, closed, not proven or validated, on
    which axis, and at which fee it would reverse. A retracted row is left out. Use it for the
    whole picture; `ledger_search` finds the rows about one idea. Read-only.
    """
    ledger = Ledger(_path(ledger_path, kind="ledger"))
    rows = ledger.effective()
    return {
        "counts": {v.value: n for v, n in ledger.counts().items()},
        "rows": [ledger_to_record(e) for e in rows],
    }


def ladder_show(ladder_path: str = "ladder.json") -> dict[str, Any]:
    """Show the promotion ladder: the tier a strategy is on and how it got there.

    Tiers run research, paper, shadow, canary, production; canary and production send real
    orders. Use it before acting on a run, to know what the strategy is allowed to do. Read-only:
    promotion is never available through this surface. Returns the tier and every promotion and
    demotion with its reason.
    """
    ladder = Ladder(_path(ladder_path, kind="ladder"))
    return {"tier": ladder.tier.name, "history": list(ladder.history)}


def run_report(run_dir: str) -> dict[str, Any]:
    """Read one run's report: what a replay, shadow or canary run did, and how it ended.

    Returns run.json as written (label, instrument, strategy, prints, bars, intents, refusals,
    fills, net bps, what stopped it) and the count of closed round trips in its trades.jsonl. Use
    `runs_index` to find the run directory first. Read-only.
    """
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


def ledger_search(ledger_path: str, query: str, limit: int = 10) -> dict[str, Any]:
    """Search the kill ledger for verdicts that mention the words in `query`.

    Matches the subject, note, evidence and axis of every row that stands, and ranks rows by how
    many words they contain. Ask here before proposing an idea: if it already died, the row says
    of what and at which fee it would reverse. Read-only. Returns the query, the number of rows
    matched and up to `limit` of them, best first.
    """
    words = [w.lower() for w in query.split() if w.strip()]
    if not words:
        raise ValueError("query needs at least one word")
    ledger = Ledger(_path(ledger_path, kind="ledger"))
    hits = []
    for entry in ledger.effective():
        record = ledger_to_record(entry)
        haystack = json.dumps(record, default=str).lower()
        score = sum(1 for w in words if w in haystack)
        if score:
            hits.append((score, record))
    hits.sort(key=lambda h: -h[0])
    return {"query": query, "matched": len(hits), "rows": [r for _, r in hits[: max(limit, 1)]]}


def runs_index(runs_dir: str, instrument: str = "") -> dict[str, Any]:
    """List past runs under a directory, newest first, with the summary of each.

    Finds every run.json below `runs_dir` and returns its directory, label, instrument, strategy,
    round trips, net bps, what stopped it and when it finished; at most 50, with the total.
    Filter by `instrument` to compare runs on one market. Use `run_report` for one run in full.
    Read-only.
    """
    root = Path(runs_dir).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"runs directory {runs_dir!r} does not exist")
    rows = []
    for summary in sorted(root.rglob("run.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            report = json.loads(summary.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if instrument and report.get("instrument") != instrument:
            continue
        rows.append(
            {
                "run": str(summary.parent),
                "label": report.get("label"),
                "instrument": report.get("instrument"),
                "strategy": report.get("strategy"),
                "round_trips": report.get("round_trips"),
                "net_bps_total": report.get("net_bps_total"),
                "stopped_by": report.get("stopped_by"),
                "finished_at": report.get("finished_at"),
            }
        )
    return {"runs": rows[:50], "total": len(rows)}


# What each argument means, for the schema a host shows the model.
PARAMETERS: dict[str, dict[str, str]] = {
    "cost_floor_check": {
        "envelope_path": "path to envelope.toml, the declared fee channels",
        "magnitude_bps": "the claimed edge per round trip, in basis points, as a decimal string",
        "venue": "only this venue's channels; empty for the cheapest across all",
    },
    "observe_events": {
        "dataset_path": "path to a Parquet trade dataset written by `tra data get`",
        "observation_path": "path to observation.toml, the declared event and recurrence",
    },
    "check_registration": {
        "registration_path": "path to a preregistration.json",
    },
    "audit_dataset": {
        "archive_path": "path to the raw trade archive (csv, csv.gz or zip)",
        "instrument": "the instrument the archive should contain, e.g. BTCUSDT",
        "venue": "an archive venue plugin name; leave empty when giving mapping_path",
        "mapping_path": "path to a column map (mapping.toml); leave empty when giving venue",
        "max_gap_minutes": "a gap between trades longer than this is reported",
    },
    "validate_trades": {
        "registration_path": "path to the sealed preregistration.json",
        "trades_path": "path to trades.jsonl, the closed trades to judge",
        "cost_bps": "round-trip cost subtracted from every trade, in bps, as a decimal string",
        "block_length": "block length of the bootstrap, in trades",
        "n_resamples": "bootstrap resamples",
        "n_perm": "permutations of the null",
        "seed": "random seed; the same seed gives the same verdict",
        "null_mode": '"sign" (flip each trade\'s sign) or "cross_sectional"',
    },
    "ledger_show": {"ledger_path": "path to the kill ledger, ledger.jsonl"},
    "ladder_show": {"ladder_path": "path to the promotion ladder, ladder.json"},
    "run_report": {"run_dir": "a run's directory, the one holding run.json"},
    "ledger_search": {
        "ledger_path": "path to the kill ledger, ledger.jsonl",
        "query": "words to look for; a row matches when it contains any of them",
        "limit": "most rows to return",
    },
    "runs_index": {
        "runs_dir": "directory to search for run.json files, e.g. runs",
        "instrument": "only runs on this instrument; empty for all",
    },
}

# Name, title, function. The order is the order a host lists them in.
READ_TOOLS: tuple[tuple[str, str, Callable[..., dict[str, Any]]], ...] = (
    ("cost_floor_check", "Cost floor (layer 0)", cost_floor_check),
    ("observe_events", "Market observation (layer 1)", observe_events),
    ("check_registration", "Check a pre-registration (layer 3)", check_registration),
    ("audit_dataset", "Audit a dataset (data gate)", audit_dataset),
    ("validate_trades", "Validate trades (layer 5)", validate_trades),
    ("ledger_show", "Show the kill ledger", ledger_show),
    ("ladder_show", "Show the promotion ladder", ladder_show),
    ("run_report", "Read a run report", run_report),
    ("ledger_search", "Search the kill ledger", ledger_search),
    ("runs_index", "List past runs", runs_index),
)
