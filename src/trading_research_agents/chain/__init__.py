"""The falsification chain: the ways an idea can die, ordered by what each test costs to run.

Layers 0 (cost floor), 3 (pre-registration), 4 (data requirements) and 5 (validation) are here,
with the era seal that layer 5 runs behind. See docs/chain.md.
"""

from trading_research_agents.chain.floor import FloorCheck, FloorOutcome, cost_floor
from trading_research_agents.chain.observation import (
    Observation,
    ObservationError,
    ObservationReport,
    Recurrence,
    load_observation,
    observe,
)
from trading_research_agents.chain.preregistration import (
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
from trading_research_agents.chain.requirements import Requirements, check_requirements
from trading_research_agents.chain.seal import EraLeakageError, Seal, SealError, research_only
from trading_research_agents.chain.validation import (
    Decision,
    Interval,
    NullMode,
    NullReport,
    TradeRecord,
    Validation,
    ValidationError,
    read_trades,
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
    "Observation",
    "ObservationError",
    "ObservationReport",
    "Power",
    "Preregistration",
    "PreregistrationError",
    "Recurrence",
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
    "load_observation",
    "observe",
    "read_trades",
    "research_only",
    "validate",
    "validate_mechanism",
]
