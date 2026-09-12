"""trading-research-agent: a research agent that refuses to claim what its gates have not measured.

The package is organised as a chain of gates. A claim about a strategy has to pass the data
gate, the falsification chain, and the execution gate before the agent is allowed to say it.
Every outcome, including the cheap ones, is written to a kill ledger.

Recommended import style::

    import trading_research_agent as tra
"""

__version__ = "0.1.0.dev0"
