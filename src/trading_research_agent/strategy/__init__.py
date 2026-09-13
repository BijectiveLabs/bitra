"""The mechanism a strategy runs under: what it may see, how it exits, how it is fitted.

No strategy lives here. The package ships the decision-core protocol, the causal exits of
layer 6, and a walk-forward harness that refuses precomputed parameters; what is fitted and
what signal comes out of it is the caller's, and `examples/strategies/` shows two.
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
from trading_research_agent.strategy.harness import (
    FittedCore,
    HarnessResult,
    HarnessTrade,
    PrecomputedParameterError,
    ols,
    walk_forward,
)

__all__ = [
    "Bar",
    "BracketOutcome",
    "BracketResult",
    "ExitError",
    "ExitRule",
    "FittedCore",
    "HarnessResult",
    "HarnessTrade",
    "PrecomputedParameterError",
    "Signal",
    "StopOutcome",
    "Strategy",
    "Target",
    "ols",
    "simulate_with_bracket",
    "simulate_with_stop",
    "stop_price",
    "walk_forward",
]
