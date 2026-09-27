"""What a run is made of. Everything here is frozen; a run is a record, not a workspace."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trading_research_agents.chain.floor import FloorCheck
    from trading_research_agents.chain.preregistration import Preregistration

IDEA_EXCERPT = 200


class Outcome(StrEnum):
    STOPPED_AT_FLOOR = "stopped_at_floor"
    REFUSED_LAYER_0 = "refused_layer_0"
    REFUSED_LAYER_2 = "refused_layer_2"
    REFUSED_LAYER_3 = "refused_layer_3"
    REGISTERED = "registered"

    @property
    def is_refusal(self) -> bool:
        return self.value.startswith("refused_")


@dataclass(frozen=True)
class Exchange:
    """One call to a language model, kept whole so a ledger row can be traced back to it."""

    sequence: int
    role: str
    system: str
    user: str
    schema: dict[str, Any]
    response: str
    model: str
    input_tokens: int
    output_tokens: int
    at: datetime


@dataclass(frozen=True)
class Refusal:
    """An idea the gates would not let through. Not a verdict: the idea was never a hypothesis."""

    run_id: str
    at: datetime
    layer: int
    revisions: int
    reason: str
    idea: str

    def __post_init__(self) -> None:
        if self.layer not in (0, 2, 3):
            raise ValueError(f"v0.2 refuses at layers 0, 2 or 3, not {self.layer}")
        if not self.reason.strip():
            raise ValueError("a refusal states why")
        object.__setattr__(self, "idea", self.idea[:IDEA_EXCERPT])


@dataclass(frozen=True)
class RunResult:
    run_id: str
    outcome: Outcome
    idea: str
    started_at: datetime
    finished_at: datetime
    exchanges: tuple[Exchange, ...]
    floor: FloorCheck | None = None
    ledger_entry_id: str | None = None
    preregistration: Preregistration | None = None
    refusal: Refusal | None = None

    @property
    def input_tokens(self) -> int:
        return sum(e.input_tokens for e in self.exchanges)

    @property
    def output_tokens(self) -> int:
        return sum(e.output_tokens for e in self.exchanges)
