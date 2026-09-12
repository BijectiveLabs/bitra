"""The falsification chain: the ways an idea can die, ordered by what each test costs to run.

Layer 0 (cost floor) and layer 3 (pre-registration) are here. The rest arrive release by
release; see docs/architecture.md.
"""

from trading_research_agent.chain.floor import FloorCheck, FloorOutcome, cost_floor
from trading_research_agent.chain.preregistration import (
    COST_TERMS,
    CostModel,
    CostTerm,
    Era,
    KillCriterion,
    Power,
    Preregistration,
    PreregistrationError,
    SealedEra,
    Universe,
    amend,
    validate_mechanism,
)

__all__ = [
    "COST_TERMS",
    "CostModel",
    "CostTerm",
    "Era",
    "FloorCheck",
    "FloorOutcome",
    "KillCriterion",
    "Power",
    "Preregistration",
    "PreregistrationError",
    "SealedEra",
    "Universe",
    "amend",
    "cost_floor",
    "validate_mechanism",
]
