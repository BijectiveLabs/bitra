"""What the cycle reads beside the market: the readings by code, the higher timeframes, and the
analyst's own last reading."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from itertools import islice
from pathlib import Path
from typing import Any

from trading_research_agents.agents import (
    technical_analyst,
)
from trading_research_agents.agents.briefs import (
    BriefError,
    interval_name,
    reading_brief,
)
from trading_research_agents.agents.cycle.config import CycleConfig
from trading_research_agents.agents.cycle.result import CycleResult
from trading_research_agents.agents.progress import (
    SKIPPED,
    announce,
)
from trading_research_agents.data.series import (
    Event,
    Series,
    SeriesError,
    load_news,
    load_readable,
    read_events,
)
from trading_research_agents.data.sources.base import Fetched
from trading_research_agents.data.sources.chain import ChainExhaustedError
from trading_research_agents.execution.outcome import BPS
from trading_research_agents.readings import ONCHAIN, READINGS
from trading_research_agents.readings.state import declared_state
from trading_research_agents.records import (
    has_index,
    recent,
)
from trading_research_agents.store.snapshot import (
    Snapshot,
)

# Snapshots looked through for the last open interest; a desk that slept longer starts afresh.
OPEN_INTEREST_LOOK_BACK = 12


# Cycles looked through for the analyst's last reading; most of them were held by the trigger.
PREVIOUS_LOOK_BACK = 96


# The shelf files that feed each reading, for saying where to declare one.
CLASSES_OF: dict[str, tuple[str, ...]] = {
    "macro": ("macro",),
    "onchain": ("onchain", "derivatives"),
    "sentiment": ("sentiment",),
}


def _context_frames(cfg: CycleConfig, result: CycleResult) -> dict[int, Fetched]:
    """The higher timeframes' closed candles. One that cannot be had is named and left out."""
    chain = cfg.chains.get("ohlcv")
    if chain is None or not cfg.context_intervals:
        return {}
    out: dict[int, Fetched] = {}
    for seconds in cfg.context_intervals:
        # A closed candle on this timeframe is up to one interval old by construction, so the
        # decision chain's freshness bar would refuse every one of them.
        wider = replace(chain, ttl_s=chain.ttl_s + seconds)
        options = dict(cfg.data_options.get("ohlcv", {}))
        options.update({"interval_s": seconds, "limit": max(cfg.lookback_bars, 2)})
        try:
            got = wider.fetch(cfg.instrument, **options)
        except ChainExhaustedError as exc:
            result.refusals.append(f"context {interval_name(seconds)}: {exc}")
            continue
        if got.fetched is not None:
            out[seconds] = got.fetched
    if out:
        result.briefs["context"] = {
            interval_name(seconds): fetched.record() for seconds, fetched in out.items()
        }
    return out


def _previous_open_interest(root: Path, instrument: str, cycle: str) -> dict[str, Any] | None:
    """The open interest the latest earlier snapshot of this instrument recorded."""
    if not root.is_dir():
        return None
    if has_index(root):
        earlier = (r for r in recent(root, instrument=instrument) if str(r["cycle_id"]) < cycle)
        for row in islice(earlier, OPEN_INTEREST_LOOK_BACK):
            if isinstance(row.get("open_interest"), dict):
                return dict(row["open_interest"])
        return None
    safe = "".join(c for c in instrument if c.isalnum() or c in "-_")
    mine = sorted(
        (p for p in root.glob(f"*-{safe}") if p.is_dir() and p.name < cycle),
        reverse=True,
    )
    for path in mine[:OPEN_INTEREST_LOOK_BACK]:
        try:
            values = json.loads((path / "snapshot.json").read_text("utf-8")).get("values", {})
        except (OSError, ValueError, AttributeError):
            continue
        found = values.get("open_interest")
        if isinstance(found, dict):
            return found
    return None


def _previous_reading(
    root: Path, instrument: str, cycle: str, close: Any, *, now: datetime, within_s: float
) -> dict[str, Any] | None:
    """The technical analyst's own last reading of this instrument, inside the window it reads.

    The reading and the market since, nothing more. Whether it was traded, refused or how it ended
    would tell a proposing role the desk's rules, which it is not given.
    """
    if not root.is_dir():
        return None
    if has_index(root):
        rows = (r for r in recent(root, instrument=instrument) if str(r["cycle_id"]) < cycle)
        names = [str(r["cycle_id"]) for r in islice(rows, PREVIOUS_LOOK_BACK)]
    else:
        safe = "".join(c for c in instrument if c.isalnum() or c in "-_")
        found = (p.name for p in root.glob(f"*-{safe}") if p.is_dir() and p.name < cycle)
        names = sorted(found, reverse=True)[:PREVIOUS_LOOK_BACK]
    for name in names:
        try:
            at = datetime.strptime(name[:20], "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=UTC)
        except ValueError:
            continue
        age_s = (now - at).total_seconds()
        if age_s > within_s:
            return None
        folder = root / name
        answer = folder / "answers" / "technical.json"
        if not answer.is_file():
            continue
        try:
            raw = json.loads(answer.read_text(encoding="utf-8"))
            said = technical_analyst.parse(raw if isinstance(raw, str) else json.dumps(raw))
            then = Decimal(
                str(json.loads((folder / "briefs" / "technical.json").read_text("utf-8"))["close"])
            )
            moved = (Decimal(str(close)) / then - 1) * BPS if close is not None and then else None
        except (OSError, ValueError, KeyError, ArithmeticError):
            continue
        return {
            "minutes_ago": round(age_s / 60, 1),
            "target": said.target,
            "setup": said.setup,
            "confidence": said.confidence,
            "stop_price": None if said.stop_price is None else str(said.stop_price),
            "target_price": None if said.target_price is None else str(said.target_price),
            "close_then": str(then),
            "move_since_bps": None if moved is None else str(moved.quantize(Decimal("0.01"))),
        }
    return None


def _readings(
    cfg: CycleConfig, snapshot: Snapshot, result: CycleResult
) -> dict[str, dict[str, Any]]:
    """The figures of every reading on the roster; the others are named with why they were not."""
    calendar = _calendar(cfg, result)
    out: dict[str, dict[str, Any]] = {}
    for name in READINGS:
        if name == ONCHAIN and cfg.asset_class == "equity" and name in cfg.readings:
            why = "on-chain figures do not bear on an equity underlying, so not read"
        elif name not in cfg.readings:
            # Shown, not hidden: a reading missing from the table reads as one that broke.
            why = _not_on_roster(cfg, name)
        else:
            built = _reading_brief(cfg, name, snapshot, calendar, result)
            if built is not None:
                out[name] = built
                continue
            why = next((r for r in reversed(result.refusals) if r.startswith(name)), "not read")
            why = why.removeprefix(f"{name}: ")
        result.briefs.setdefault("not_read", {})[name] = why
        announce(cfg.watch, name, SKIPPED, why)
    return out


def _reading_state(name: str, brief: Mapping[str, Any]) -> dict[str, Any]:
    declared = declared_state(name, brief)
    return {
        "declared": declared.get("in_its_words") or declared.get("declared"),
        "score": declared.get("score", "0"),
    }


def _regime_line(cfg: CycleConfig, brief: Any, states: Mapping[str, Any]) -> str:
    said = [f"{brief.regime} on {interval_name(cfg.interval_s)}"]
    if brief.features.structure is not None:
        shape = brief.features.structure
        said.append(f"structure {shape.trend}" + (f", {shape.event}" if shape.event else ""))
    said.extend(f"{name} {frame['regime']}" for name, frame in (brief.timeframes or {}).items())
    # Shown to the operator; the analyst's brief does not carry them.
    said.extend(f"{name} {state['declared']}" for name, state in states.items())
    return ", ".join(said)


def _not_on_roster(cfg: CycleConfig, name: str) -> str:
    if cfg.series.get(name):
        return "not in [agents].readings, so not read"
    shelves = " or ".join(f"sources/{shelf}.toml" for shelf in CLASSES_OF.get(name, ()))
    return f"nothing declared in {shelves or 'sources/'}, so not read"


def _calendar(cfg: CycleConfig, result: CycleResult) -> tuple[Event, ...]:
    """Every dated text file the project declares, as one list. A bad file is named, not guessed."""
    out: list[Event] = []
    for path in cfg.news:
        try:
            out.extend(load_news(path) if path.suffix == ".toml" else read_events(path))
        except SeriesError as exc:
            result.refusals.append(f"news {path.name}: {exc}")
    return tuple(sorted(out, key=lambda e: e.when_ns))


def _reading_brief(
    cfg: CycleConfig,
    name: str,
    snapshot: Snapshot,
    calendar: tuple[Event, ...],
    result: CycleResult,
) -> dict[str, Any] | None:
    """One reading's figures, or nothing when its series cannot be read at all."""
    series: tuple[Series, ...] = ()
    files = cfg.series.get(name, ())
    for declared in files:
        try:
            readable, problems = load_readable(declared)
        except SeriesError as exc:
            # One unreadable file does not silence a reading that has other shelves to read from.
            result.refusals.append(f"{name} {declared.name}: {exc}")
            continue
        series = (*series, *readable)
        result.refusals.extend(f"{name} {declared.name}: {problem}" for problem in problems)
    if files and not series:
        # Everything this reading was given is unreadable; reading nothing would hide a broken
        # file behind an undeclared state.
        result.refusals.append(f"{name}: nothing readable in {[p.name for p in files]}; not read")
        return None
    try:
        brief = reading_brief(name, snapshot, series=series, events=calendar)
    except BriefError as exc:
        result.refusals.append(f"{name}: {exc}")
        return None
    rows = list(brief.get("series") or [])
    result.briefs.setdefault("series", []).extend(
        {
            "reading": name,
            "series": row.get("series"),
            "source": row.get("source"),
            "origin": row.get("origin"),
            "value": row.get("value"),
            "observed_at": row.get("observed_at"),
            "age_h": row.get("age_h"),
            "state": row.get("state"),
        }
        for row in rows
    )
    old = [row for row in rows if row.get("stale")]
    for row in old:
        result.refusals.append(
            f"{name} {row['series']}: last value {str(row['observed_at'])[:16]}, "
            f"{row['age_h']}h old, past max_age {row['max_age']}; shown, not counted"
        )
    if rows and len(old) == len(rows) and not (brief.get("events") or brief.get("notes")):
        # Every figure this reading has is out of date; a state from them is one nothing can count.
        result.refusals.append(f"{name}: every series is past its max_age; not read")
        return None
    return brief


def _conviction_states(result: CycleResult) -> dict[str, Mapping[str, Any]]:
    """The declared state of each reading, computed by code from the operator's thresholds."""
    return {
        role: said["declared_state"]
        for role, said in result.context.items()
        if isinstance(said.get("declared_state"), Mapping)
    }


def _context_of(briefs: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """What each reading's series say against the operator's own thresholds."""
    return {
        name: {"declared_state": declared_state(name, briefs[name]), "read_by": "code"}
        for name in READINGS
        if name in briefs
    }
