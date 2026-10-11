"""Old cycle directories out of the way, on the operator's word, with the index kept whole."""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trading_research_agents.records import build_index
from trading_research_agents.store.cycles import SNAPSHOT, SUMMARY

# Positions open, intents counted today and the trigger's memory all read the last day or two.
MIN_KEEP = timedelta(days=2)
ARCHIVE = "archive"


@dataclass
class Plan:
    cutoff: str
    folders: list[Path] = field(default_factory=list)
    screens: list[Path] = field(default_factory=list)
    size: int = 0

    def record(self) -> dict[str, Any]:
        return {
            "older_than": self.cutoff,
            "cycles": len(self.folders),
            "screens": len(self.screens),
            "bytes": self.size,
        }


def plan(root: Path, keep: timedelta, now: datetime | None = None) -> Plan:
    if keep < MIN_KEEP:
        raise ValueError(f"keep at least {MIN_KEEP.days} days; the desk reads that far back")
    cutoff = f"{(now or datetime.now(UTC)) - keep:%Y-%m-%d}"
    out = Plan(cutoff=cutoff)
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if (
                path.is_dir()
                and path.name[:10] < cutoff
                and ((path / SUMMARY).is_file() or (path / SNAPSHOT).is_file())
            ):
                out.folders.append(path)
                out.size += sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    screens = root.parent / "screens"
    if screens.is_dir():
        for path in sorted(screens.glob("*.json")):
            if path.name[:10] < cutoff:
                out.screens.append(path)
                out.size += path.stat().st_size
    return out


def apply(root: Path, chosen: Plan, *, archive: bool) -> Path | None:
    """Remove what the plan names. With `archive`, each month goes into a zip under runs/archive
    first, and nothing is removed unless its copy was written."""
    build_index(root)
    kept: Path | None = None
    if archive and (chosen.folders or chosen.screens):
        kept = root.parent / ARCHIVE
        kept.mkdir(parents=True, exist_ok=True)
        for path in [*chosen.folders, *chosen.screens]:
            month = path.name[:7]
            with zipfile.ZipFile(
                kept / f"{month}.zip", "a", compression=zipfile.ZIP_DEFLATED
            ) as box:
                files = [f for f in path.rglob("*") if f.is_file()] if path.is_dir() else [path]
                for f in files:
                    box.write(f, f.relative_to(root.parent).as_posix())
    for path in chosen.folders:
        shutil.rmtree(path)
    for path in chosen.screens:
        path.unlink()
    return kept
