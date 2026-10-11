"""Sentiment analyst: dated scores and dated text the operator mapped in, and nothing else.

Writes: nothing. Scrapes: nothing. With no declared source it says so rather than guessing.
One call, one schema, one typed object out; the cycle records it. See `role.py`.
"""

from trading_research_agents.agents.sentiment_analyst.role import (
    ROLE,
    SCHEMA,
    STATES,
    SentimentAnalyst,
    parse,
)

__all__ = ["ROLE", "SCHEMA", "STATES", "SentimentAnalyst", "parse"]
