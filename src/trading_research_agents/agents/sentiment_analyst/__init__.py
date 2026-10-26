"""Sentiment reading: dated scores and dated text the operator mapped in, read by code against the
operator's declared thresholds. With no declared source it says so rather than guessing.

Writes: nothing. Scrapes: nothing. Asks no model: until 0.11 a model labelled the same brief, and
no gate ever read that label. The brief builder is in `tools.py`.
"""

ROLE = "sentiment_analyst"

__all__ = ["ROLE"]
