"""Which declaration file feeds which analyst, and how a project says what it has.

A desk does not have one data source, it has a shelf of them, and the shelf changes when a
subscription starts or a free endpoint dies. So the declarations are one file per class of data,
never one file per vendor: a vendor is a line inside a file, and swapping one for another is an
edit in one place rather than a new file and a new habit.

    my-desk/sources/
      macro.toml         dated levels and rates: yields, dollar index, whatever the desk follows
      news.toml          dated text: releases, statements, the desk's own notes
      sentiment.toml     dated scores: a fear index, a positioning score
      onchain.toml       dated chain figures: venue flows, supply held, active addresses
      derivatives.toml   dated derivative figures: funding history, open interest, basis

Nothing here knows a vendor's name and nothing here fetches. Each file names local files the
operator produced, through the same series engine and the same as-of rule as everything else, so a
free CSV export and a paid vendor's dump are read the same way and neither is privileged.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trading_research_agents.agents import macro_analyst, onchain_analyst, sentiment_analyst
from trading_research_agents.data.series import (
    SeriesError,
    load_news_map,
    load_series_map,
)

SOURCES_DIR = "sources"

# One class of data, one file, and the role that reads it. Two classes may feed one role: a desk's
# chain figures and its derivative figures are both read by the on-chain analyst, because they
# answer the same question about who is accumulating and who is leaving.
SERIES_CLASSES: dict[str, str] = {
    "macro": macro_analyst.ROLE,
    "sentiment": sentiment_analyst.ROLE,
    "onchain": onchain_analyst.ROLE,
    "derivatives": onchain_analyst.ROLE,
}
# Dated text, not dated numbers, so it is read by the calendar reader rather than the series
# engine. The macro analyst reads it as events; the sentiment analyst reads it as notes.
TEXT_CLASSES: dict[str, tuple[str, ...]] = {
    "news": (macro_analyst.ROLE, sentiment_analyst.ROLE),
}
# What a project written before `sources/` existed called these files. Still read, still supported.
LEGACY: dict[str, str] = {
    "series.toml": "macro",
    "onchain.toml": "onchain",
    "sentiment.toml": "sentiment",
}


class ContextError(ValueError):
    """A declaration file is named for a class of data this package does not read."""


@dataclass(frozen=True)
class Declared:
    """What a project has on its shelf, by class and by the role that will read it."""

    series: dict[str, list[Path]] = field(default_factory=dict)
    text: list[Path] = field(default_factory=list)
    classes: dict[str, Path] = field(default_factory=dict)
    # On the shelf but declaring nothing yet. `tra init` writes these, and a class stays off until
    # the operator fills one in, so an untouched project asks for no role it cannot feed.
    empty: tuple[str, ...] = ()

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(sorted(self.series))

    def for_role(self, role: str) -> tuple[Path, ...]:
        return tuple(self.series.get(role, ()))

    def record(self) -> dict[str, Any]:
        return {
            "classes": {name: str(path) for name, path in sorted(self.classes.items())},
            "roles": {role: [str(p) for p in paths] for role, paths in sorted(self.series.items())},
            "text": [str(p) for p in self.text],
            "empty": list(self.empty),
        }


def declared_in(project: Path, *, overrides: Mapping[str, Path] | None = None) -> Declared:
    """What this project declares: `sources/<class>.toml`, the older names, and any override.

    An override is a path an operator passed on the command line for one class. It wins over the
    file on the shelf, which is what a client wants when they are trying a second export without
    moving their own files around.
    """
    found: dict[str, Path] = {}
    empty: list[str] = []
    shelf = project / SOURCES_DIR
    for name in (*SERIES_CLASSES, *TEXT_CLASSES):
        path = shelf / f"{name}.toml"
        if not path.is_file():
            continue
        if _declares(path, "news" if name in TEXT_CLASSES else "series"):
            found[name] = path
        else:
            empty.append(name)
    for legacy, name in LEGACY.items():
        path = project / legacy
        if name not in found and name not in empty and path.is_file():
            found[name] = path
    for name, path in (overrides or {}).items():
        if name not in SERIES_CLASSES and name not in TEXT_CLASSES:
            raise ContextError(
                f"{name} is not a class of data; have {sorted((*SERIES_CLASSES, *TEXT_CLASSES))}"
            )
        found[name] = path
        if name in empty:
            empty.remove(name)

    series: dict[str, list[Path]] = {}
    text: list[Path] = []
    for name, path in sorted(found.items()):
        if name in SERIES_CLASSES:
            series.setdefault(SERIES_CLASSES[name], []).append(path)
        else:
            text.append(path)
    return Declared(series=series, text=text, classes=found, empty=tuple(sorted(empty)))


def _declares(path: Path, key: str) -> bool:
    """Whether a declaration has anything in it. A file of comments is a form, not a mistake."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        # Malformed is not empty: let the loader say so with its own words.
        return True
    entries = raw.get(key)
    return isinstance(entries, list) and bool(entries)


@dataclass(frozen=True)
class Reading:
    """What one declaration promises, and whether the files it names are on disk."""

    name: str
    path: Path
    entries: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    error: str = ""

    @property
    def usable(self) -> bool:
        return not self.error and not self.missing and bool(self.entries)


def inspect_shelf(declared: Declared) -> tuple[Reading, ...]:
    """Read every declaration without reading the data: structure and the named files exist.

    This is what `tra check` asks before a client spends a cycle's tokens. A missing export is
    the ordinary mistake, and it is cheaper to name it here than to have a role refuse later.
    """
    out: list[Reading] = []
    for name, path in sorted(declared.classes.items()):
        try:
            named = (
                [(s.name, s.file) for s in load_news_map(path)]
                if name in TEXT_CLASSES
                else [(s.name, s.file) for s in load_series_map(path)]
            )
        except SeriesError as exc:
            out.append(Reading(name=name, path=path, error=str(exc)))
            continue
        base = path.resolve().parent
        missing = []
        for _, declared_file in named:
            target = Path(declared_file)
            where = target if target.is_absolute() else base / target
            if not where.is_file():
                missing.append(declared_file)
        out.append(
            Reading(
                name=name,
                path=path,
                entries=tuple(entry for entry, _ in named),
                missing=tuple(missing),
            )
        )
    return tuple(out)


def roster_for(declared: Declared, deciding_role: str) -> tuple[str, ...]:
    """The roles worth asking: the one that decides, plus every role with something to read."""
    return (deciding_role, *declared.roles)
