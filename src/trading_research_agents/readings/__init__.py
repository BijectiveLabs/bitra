"""Readings: the operator's own series, read by code against the operator's own thresholds.

Three readings, each over the classes of data a desk declares under `sources/`:

    macro      dated levels and rates, and the scheduled events in the horizon
    onchain    chain and derivatives figures: flows, supply held, funding, open interest
    sentiment  dated scores and dated notes

A reading asks no model. Until 0.11 each was an analyst role whose model labelled the same figures,
and no gate ever read the label; the declared state code computes here is what confluence and
conviction read. The names the roles had are kept in `RETIRED` so a project that still writes them
is told the new name rather than ignored.
"""

MACRO = "macro"
ONCHAIN = "onchain"
SENTIMENT = "sentiment"
READINGS = (MACRO, ONCHAIN, SENTIMENT)
RETIRED = {
    "macro_analyst": MACRO,
    "onchain_analyst": ONCHAIN,
    "sentiment_analyst": SENTIMENT,
}

__all__ = ["MACRO", "ONCHAIN", "READINGS", "RETIRED", "SENTIMENT"]
