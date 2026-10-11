"""Execution realism: what a trade would actually have cost, and whether it would have happened.

Every figure here is a floor that omits market impact. Nothing in the research this package is
built on ran at a size where its own transacting moved the book, so an operator at size should
treat every number as the cost of the first trade, not of the tenth.
"""

from trading_research_agents.execution.book import (
    BookError,
    Level,
    Snapshot,
    WalkResult,
    half_spread_bps,
    mid_price,
    spread_bps,
    walk_the_book,
)
from trading_research_agents.execution.cost import (
    CostBreakdown,
    CostError,
    Direction,
    FundingSchedule,
    funding_accrual_bps,
    pair_leg_fee_bps,
    realised_cost,
)
from trading_research_agents.execution.fill import (
    EffectiveCost,
    FillError,
    FillMode,
    IntendedOrder,
    Print,
    effective_cost,
    entry_after_latency,
    passive_fill,
)
from trading_research_agents.execution.markout import Fill, Markout, markout_bps, markout_table

__all__ = [
    "BookError",
    "CostBreakdown",
    "CostError",
    "Direction",
    "EffectiveCost",
    "Fill",
    "FillError",
    "FillMode",
    "FundingSchedule",
    "IntendedOrder",
    "Level",
    "Markout",
    "Print",
    "Snapshot",
    "WalkResult",
    "effective_cost",
    "entry_after_latency",
    "funding_accrual_bps",
    "half_spread_bps",
    "markout_bps",
    "markout_table",
    "mid_price",
    "pair_leg_fee_bps",
    "passive_fill",
    "realised_cost",
    "spread_bps",
    "walk_the_book",
]
