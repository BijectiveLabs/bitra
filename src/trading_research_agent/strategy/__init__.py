"""Decision cores and the exits bound to them (layer 6).

A strategy is a pure function of the bars it has seen. It holds no clock, opens no socket and
places no order; it says what it wants and the trading runtime decides whether it may have it.
"""

from trading_research_agent.strategy.core import Bar, Signal, Strategy, Target
from trading_research_agent.strategy.exits import (
    BracketOutcome,
    BracketResult,
    ExitError,
    ExitRule,
    StopOutcome,
    simulate_with_bracket,
    simulate_with_stop,
    stop_price,
)
from trading_research_agent.strategy.pair_reversion import (
    PairCost,
    PairTrade,
    PrecomputedParameterError,
    WalkForwardResult,
    ols,
    walk_forward,
)

__all__ = [
    "Bar",
    "BracketOutcome",
    "BracketResult",
    "ExitError",
    "ExitRule",
    "PairCost",
    "PairTrade",
    "PrecomputedParameterError",
    "Signal",
    "StopOutcome",
    "Strategy",
    "Target",
    "WalkForwardResult",
    "ols",
    "simulate_with_bracket",
    "simulate_with_stop",
    "stop_price",
    "walk_forward",
]
