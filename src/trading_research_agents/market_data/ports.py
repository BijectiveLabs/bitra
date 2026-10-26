"""Where shared state could live, and the file implementation the package ships.

TRA keeps its state in files that can be hashed, copied and replayed; there is no shared store to
install. These protocols are the seams where a platform running many desks would put one: a request
budget several processes draw from, and a feed of closed bars several readers poll. Code depends on
the protocol, so swapping the implementation changes no caller.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Protocol, runtime_checkable

from trading_research_agents.market_data.archive import read_flow_range
from trading_research_agents.market_data.archive_book import read_book_range
from trading_research_agents.market_data.archive_deriv import read_deriv_range
from trading_research_agents.market_data.bars import FlowBar
from trading_research_agents.market_data.book import BookBar
from trading_research_agents.market_data.deriv import DerivPoint


@runtime_checkable
class BudgetStore(Protocol):
    """Requests a venue allows, drawn by whoever asks; `RequestBudget` is the in-process one."""

    def acquire(self) -> float: ...

    def pause(self, seconds: float) -> None: ...


@runtime_checkable
class BarFeed(Protocol):
    """Closed bars and published points of one market, by UTC day."""

    def flow(self, venue: str, instrument: str, start: date, end: date) -> list[FlowBar]: ...

    def book(self, venue: str, instrument: str, start: date, end: date) -> list[BookBar]: ...

    def deriv(self, venue: str, instrument: str, start: date, end: date) -> list[DerivPoint]: ...


class ArchiveFeed:
    """The archive under `root` as a bar feed: day files and the hour parts already written."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def flow(self, venue: str, instrument: str, start: date, end: date) -> list[FlowBar]:
        return read_flow_range(self.root, venue, instrument, start, end)

    def book(self, venue: str, instrument: str, start: date, end: date) -> list[BookBar]:
        return read_book_range(self.root, venue, instrument, start, end)

    def deriv(self, venue: str, instrument: str, start: date, end: date) -> list[DerivPoint]:
        return read_deriv_range(self.root, venue, instrument, start, end)
