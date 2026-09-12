"""The falsification chain: the ways an idea can die, ordered by what each test costs to run.

Layers 0 (cost floor), 3 (pre-registration), 4 (data requirements) and 5 (validation) are here,
with the era seal that layer 5 runs behind. See docs/chain.md.
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
from trading_research_agent.chain.requirements import Requirements, check_requirements
from trading_research_agent.chain.seal import EraLeakageError, Seal, SealError, research_only
from trading_research_agent.chain.validation import (
    Decision,
    Interval,
    NullMode,
    NullReport,
    TradeRecord,
    Validation,
    ValidationError,
    validate,
)

__all__ = [
    "COST_TERMS",
    "CostModel",
    "CostTerm",
    "Decision",
    "Era",
    "EraLeakageError",
    "FloorCheck",
    "FloorOutcome",
    "Interval",
    "KillCriterion",
    "NullMode",
    "NullReport",
    "Power",
    "Preregistration",
    "PreregistrationError",
    "Requirements",
    "Seal",
    "SealError",
    "SealedEra",
    "TradeRecord",
    "Universe",
    "Validation",
    "ValidationError",
    "amend",
    "check_requirements",
    "cost_floor",
    "research_only",
    "validate",
    "validate_mechanism",
]
