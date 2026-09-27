"""The operating envelope: what execution costs for this operator, on this infrastructure.

Fee tier, execution channel, latency and venue access belong to the operator, not to the market.
They are configuration, never constants in logic, so that a reader with a different schedule can
re-derive every cost-bound verdict without touching anything else.
"""

from trading_research_agent.envelope.load import load_envelope, parse_envelope
from trading_research_agent.envelope.model import Channel, Envelope, EnvelopeError

__all__ = ["Channel", "Envelope", "EnvelopeError", "load_envelope", "parse_envelope"]
