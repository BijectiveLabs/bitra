"""Which instrument a cycle is spent on, when a desk watches more than one.

A cycle costs a data pull and a roster of model calls, so a desk with five instruments cannot run
five cycles a minute and should not want to. The question this answers is the cheap one: of the
instruments the operator declared, which ones have had something happen, and which of those is worth
the pass.

Nothing new is declared to answer it. The same `[trigger]` an operator already set - the move since
the last look, the width of the book, the cooldown after an intent - is applied per instrument, and
what survives is ordered by a written rule: the move already seen against what it costs to cross the
spread. That is the same reasoning the trigger does, asked across a row instead of down one.

The ordering is code because it decides where capital gets to be considered at all. A screener may
narrow what code admitted and say why; it may never add an instrument, reorder one into the run, or
say anything about direction or size.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any


class UniverseError(ValueError):
    """The universe as declared cannot be applied."""


@dataclass(frozen=True)
class Universe:
    """[universe]: what this desk watches, and how much of it one pass may take on."""

    instruments: tuple[str, ...] = ()
    max_per_cycle: int = 1
    max_intents_per_day: int | None = None
    screener: bool = False
    # Discovery: every linear perpetual the venue lists, filtered to what can be traded, the most
    # traded `top` of them added to `instruments` each pass. Code finds and measures; the screener
    # chooses among what code found.
    discover: bool = False
    top: int = 20
    min_quote_volume: Decimal = Decimal(10_000_000)
    max_spread_bps: Decimal = Decimal(5)
    min_listed_days: int = 30
    crypto_only: bool = True
    exclude: tuple[str, ...] = ()
    # The settlement currency of the perpetuals discovery considers.
    quote: str = "USDT"
    # The venue's own market field that says what a market is, and which values to leave out; for a
    # venue the package has no default for, or to change the default.
    type_field: str = ""
    exclude_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(set(self.instruments)) != len(self.instruments):
            raise UniverseError("[universe].instruments names each instrument once")
        if self.max_per_cycle < 1:
            raise UniverseError("[universe].max_per_cycle is at least 1")
        if self.max_intents_per_day is not None and self.max_intents_per_day < 0:
            raise UniverseError("[universe].max_intents_per_day is not negative")
        if self.screener and not self.instruments and not self.discover:
            raise UniverseError(
                "[universe].screener has nothing to screen; declare instruments, set discover, "
                "or drop it"
            )
        if self.top < 1 or self.min_listed_days < 0:
            raise UniverseError("[universe].top is at least 1 and min_listed_days not negative")
        if self.min_quote_volume < 0 or self.max_spread_bps <= 0:
            raise UniverseError("[universe].min_quote_volume and max_spread_bps are positive")

    @property
    def idle(self) -> bool:
        return not self.instruments and not self.discover

    def record(self) -> dict[str, Any]:
        return {
            "instruments": list(self.instruments),
            "max_per_cycle": self.max_per_cycle,
            "max_intents_per_day": self.max_intents_per_day,
            "screener": self.screener,
            "discover": self.discover,
            "top": self.top,
            "min_quote_volume": str(self.min_quote_volume),
            "max_spread_bps": str(self.max_spread_bps),
            "min_listed_days": self.min_listed_days,
            "crypto_only": self.crypto_only,
            "exclude": list(self.exclude),
            "quote": self.quote,
            "type_field": self.type_field,
            "exclude_types": list(self.exclude_types),
        }

    def with_instruments(self, names: tuple[str, ...]) -> Universe:
        """This universe for one pass, with what discovery found added to the declared names."""
        return replace(self, instruments=names, discover=False)


@dataclass(frozen=True)
class Candidate:
    """One instrument as the screen sees it: free figures, and what the trigger made of them."""

    instrument: str
    admitted: bool
    mid: Decimal | None = None
    spread_bps: Decimal | None = None
    moved_bps: Decimal | None = None
    range_bps: Decimal | None = None
    age_s: float | None = None
    reasons: tuple[str, ...] = ()
    kept_by_screener: bool = True
    screener_said: str = ""
    # How closely this instrument has been moving with something the desk already holds, and which
    # one. Two names moving together are one trade in two places.
    correlation: Decimal | None = None
    correlated_with: str = ""
    # What code measured on this instrument for the screener to choose from, beyond the trigger's
    # figures: regime, stretch, volume, the venue's 24 hours, correlation with BTC.
    features: Mapping[str, Any] = field(default_factory=dict)
    # Where the screener placed it (1 is first) and the setup it expects; None when it was not
    # picked or no screener was asked.
    pick_rank: int | None = None
    setup: str = ""

    @property
    def taken(self) -> bool:
        return self.admitted and self.kept_by_screener

    @property
    def pressure(self) -> Decimal | None:
        """The move already seen against the cost of crossing the book, or nothing to compare."""
        if self.moved_bps is None or self.spread_bps is None or self.spread_bps <= 0:
            return None
        return (self.moved_bps / self.spread_bps).quantize(Decimal("0.01"))

    def record(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "admitted": self.admitted,
            "mid": None if self.mid is None else str(self.mid),
            "spread_bps": None if self.spread_bps is None else str(self.spread_bps),
            "moved_bps": None if self.moved_bps is None else str(self.moved_bps),
            "range_bps": None if self.range_bps is None else str(self.range_bps),
            "age_s": self.age_s,
            "pressure": None if self.pressure is None else str(self.pressure),
            "correlation": None if self.correlation is None else str(self.correlation),
            "correlated_with": self.correlated_with,
            "reasons": list(self.reasons),
            "kept_by_screener": self.kept_by_screener,
            "screener_said": self.screener_said,
            "features": dict(self.features),
            "pick_rank": self.pick_rank,
            "setup": self.setup,
        }


@dataclass
class Screened:
    """One pass over the universe: every candidate, and the ones a cycle is spent on."""

    at: str
    candidates: list[Candidate] = field(default_factory=list)
    chosen: tuple[str, ...] = ()
    refusals: list[str] = field(default_factory=list)
    asked_screener: bool = False
    # Code's own order of what it admitted, kept beside the screener's so the two can be compared
    # once trades have closed.
    code_order: tuple[str, ...] = ()
    discovered: Mapping[str, Any] | None = None

    def record(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "candidates": [c.record() for c in self.candidates],
            "chosen": list(self.chosen),
            "refusals": self.refusals,
            "asked_screener": self.asked_screener,
            "code_order": list(self.code_order),
            "discovered": None if self.discovered is None else dict(self.discovered),
        }


def order(candidates: list[Candidate], declared: tuple[str, ...]) -> list[Candidate]:
    """The order a pass takes them in: most pressure first, then the operator's own order.

    An instrument with no previous cycle has no move to measure, so it cannot be compared on
    pressure and keeps the place the operator gave it. Nothing here is a forecast: it is the same
    question the trigger asks, which is where something has happened relative to what it costs.
    """
    where = {name: i for i, name in enumerate(declared)}

    def key(c: Candidate) -> tuple[int, int, Decimal, int]:
        pressure = c.pressure
        return (
            # The screener's order, when it gave one, comes before code's.
            c.pick_rank if c.pick_rank is not None else 1_000_000,
            0 if pressure is not None else 1,
            -(pressure or Decimal(0)),
            where.get(c.instrument, len(where)),
        )

    return sorted(candidates, key=key)


def take(universe: Universe, candidates: list[Candidate]) -> tuple[str, ...]:
    """The instruments this pass runs a full cycle on, in order, inside the declared limit."""
    ready = [c for c in order(candidates, universe.instruments) if c.taken]
    return tuple(c.instrument for c in ready[: universe.max_per_cycle])


def why_no_cycle(rows: Sequence[Mapping[str, Any]]) -> str:
    """Why a pass ran no cycle, by cause, so a quiet market and a full book read differently.

    Reads the candidates as the screen records them, so the console and a log say the same thing.
    """

    def held_for(row: Mapping[str, Any], words: str) -> bool:
        taken = row.get("admitted") and row.get("kept_by_screener", True)
        return not taken and any(words in str(r) for r in row.get("reasons") or [])

    # Each instrument is named once, under the first cause that holds it, in the order that
    # matters: the book before the market.
    causes = (
        ("open", "a position is open"),
        ("capped", "max_open_positions"),
        ("paired", "moving with"),
        ("quiet", "the mid has moved"),
    )
    found: dict[str, list[Mapping[str, Any]]] = {name: [] for name, _ in causes}
    rest: list[str] = []
    for row in rows:
        if row.get("admitted") and row.get("kept_by_screener", True):
            continue
        cause = next((name for name, words in causes if held_for(row, words)), None)
        if cause is None:
            rest.append(str(row["instrument"]))
        else:
            found[cause].append(row)
    open_now = [str(r["instrument"]) for r in found["open"]]
    capped = [str(r["instrument"]) for r in found["capped"]]
    paired = found["paired"]
    quiet = [str(r["instrument"]) for r in found["quiet"]]
    said = []
    if open_now:
        said.append(f"{', '.join(open_now)} open at the venue")
    if paired:
        others = sorted({str(r.get("correlated_with") or "an open position") for r in paired})
        said.append(
            f"{', '.join(str(r['instrument']) for r in paired)} moving with the open "
            f"{', '.join(others)} above [desk].correlation_above"
        )
    if capped:
        said.append(f"{', '.join(capped)} waiting: the desk is at [desk].max_open_positions")
    if quiet:
        said.append(f"{', '.join(quiet)} not moved enough for [trigger]")
    if rest:
        said.append(f"{', '.join(rest)} held for other reasons (above)")
    if open_now and paired and not quiet and not rest:
        said.append("this universe moves together, so the desk carries one position at a time")
    return "; ".join(said) or "nothing was admitted"
