"""The kill ledger: a closed vocabulary and an append-only record of every test and its end.

See docs/ledger.md for the vocabulary and the consistency rules between verdicts and axes.
"""

from trading_research_agents.ledger.entry import LedgerEntry, LedgerError, ReversalPoint
from trading_research_agents.ledger.store import Ledger
from trading_research_agents.ledger.vocabulary import (
    ENVELOPE_BOUND_AXES,
    UNIVERSAL_AXES,
    Axis,
    Transferability,
    Verdict,
    implied_transferability,
)

__all__ = [
    "ENVELOPE_BOUND_AXES",
    "UNIVERSAL_AXES",
    "Axis",
    "Ledger",
    "LedgerEntry",
    "LedgerError",
    "ReversalPoint",
    "Transferability",
    "Verdict",
    "implied_transferability",
]
