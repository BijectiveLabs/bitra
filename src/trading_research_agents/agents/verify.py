"""Check a desk's cycle records are the ones it wrote, and its briefs follow from its inputs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.agents.briefs import BriefError, structure_brief
from trading_research_agents.execution.regime import RegimeRule
from trading_research_agents.records import read_gzip_json, recent, sha256_of
from trading_research_agents.store.cycles import BRIEFS, SUMMARY, lead_file
from trading_research_agents.store.snapshot import INPUTS, read_inputs

# The figures a structure brief carries that follow from the inputs alone. Ages depend on when the
# brief was built, and the readings and open interest on files outside the cycle.
FIGURES = (
    "bars",
    "close",
    "ret_1_bps",
    "ret_5_bps",
    "ret_20_bps",
    "zscore",
    "vwap_gap_bps",
    "volume_ratio",
    "range_ratio",
    "position_in_range",
    "window_move_bps",
    "trend_strength",
    "variance_ratio",
    "market_structure",
    "regime",
    "spread_bps",
    "funding_rate",
    "book_imbalance",
)


@dataclass
class Checked:
    cycle: str
    problems: list[str] = field(default_factory=list)
    hashed: bool = False
    recomputed: bool = False

    @property
    def ok(self) -> bool:
        return not self.problems

    def record(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle,
            "ok": self.ok,
            "hashed": self.hashed,
            "recomputed": self.recomputed,
            "problems": list(self.problems),
        }


def verify_cycle(folder: Path, row: Mapping[str, Any] | None = None) -> Checked:
    """Hashes against the manifest, the summary against its index line, figures against inputs."""
    out = Checked(cycle=folder.name)
    summary_path = folder / SUMMARY
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        out.problems.append(f"{SUMMARY} cannot be read: {exc}")
        return out
    expected = (row or {}).get("summary_sha256")
    if expected and sha256_of(summary_path) != expected:
        out.problems.append(f"{SUMMARY} differs from the hash its index line recorded")
    files = summary.get("files")
    if isinstance(files, dict):
        out.hashed = True
        for name, digest in sorted(files.items()):
            path = folder / name
            if not path.is_file():
                out.problems.append(f"{name} is listed and missing")
            elif sha256_of(path) != digest:
                out.problems.append(f"{name} differs from the hash written when the cycle ended")
        for path in sorted(folder.rglob("*")):
            name = path.relative_to(folder).as_posix()
            listed = name in files or name == SUMMARY or path.name.startswith(".")
            if path.is_file() and not listed:
                out.problems.append(f"{name} was added after the cycle ended")
    if (folder / INPUTS).is_file() and lead_file(folder, BRIEFS).is_file():
        out.recomputed = True
        out.problems.extend(_recompute(folder))
    return out


def _recompute(folder: Path) -> list[str]:
    try:
        snapshot, context, settings = read_inputs(read_gzip_json(folder / INPUTS))
        stored = json.loads(lead_file(folder, BRIEFS).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"the inputs cannot be read back: {exc}"]
    regime = settings.get("regime") or {}
    try:
        brief = structure_brief(
            snapshot,
            mechanism=str(settings.get("mechanism", "")),
            interval_s=int(settings["interval_s"]),
            lookback_bars=int(settings["lookback_bars"]),
            rule=RegimeRule(
                trend_above=Decimal(str(regime.get("trend_above", "2.0"))),
                range_below=Decimal(str(regime.get("range_below", "1.0"))),
                setups_must_fit=bool(regime.get("setups_must_fit", True)),
                fade_below_variance_ratio=Decimal(
                    str(regime.get("fade_below_variance_ratio", "1.0"))
                ),
            ),
            context=context,
        ).record()
    except (BriefError, KeyError, ValueError) as exc:
        return [f"the structure brief cannot be rebuilt from the inputs: {exc}"]
    problems = [
        f"structure brief {name} is {stored.get(name)!r}; the inputs give {brief.get(name)!r}"
        for name in FIGURES
        if name in stored and stored.get(name) != brief.get(name)
    ]
    for frame, figures in (stored.get("timeframes") or {}).items():
        rebuilt = (brief.get("timeframes") or {}).get(frame) or {}
        for name, value in figures.items():
            # A figure this version no longer builds was retired by a later release; the hashes
            # still vouch for the file, and there is nothing to recompute it against.
            if name == "age_s" or name not in rebuilt:
                continue
            if rebuilt.get(name) != value:
                problems.append(
                    f"structure brief timeframes.{frame}.{name} is {value!r}; the inputs give "
                    f"{rebuilt.get(name)!r}"
                )
    return problems


def verify_runs(root: Path, *, days: int | None = None) -> dict[str, Any]:
    """Every cycle in the window: checked, pruned, or written before hashes existed."""
    since = None if days is None else f"{datetime.now(UTC) - timedelta(days=days):%Y-%m-%d}"
    rows = {str(r.get("cycle_id")): r for r in recent(root)}
    folders = {
        p.name: p
        for p in (root.iterdir() if root.is_dir() else [])
        if p.is_dir() and (p / SUMMARY).is_file()
    }
    names = sorted(set(rows) | set(folders), reverse=True)
    if since is not None:
        names = [n for n in names if n[:10] >= since]
    checked = [verify_cycle(folders[n], rows.get(n)) for n in names if n in folders]
    failed = [c.record() for c in checked if not c.ok]
    return {
        "cycles": len(names),
        "checked": len(checked),
        "pruned": sum(1 for n in names if n not in folders),
        "hashed": sum(1 for c in checked if c.hashed),
        "recomputed": sum(1 for c in checked if c.recomputed),
        "failed": failed,
    }
