"""The features a decision reads by default, each with its unit, its window and its inputs."""

from __future__ import annotations

from collections.abc import Sequence

from trading_research_agents.features import book, cross, derivatives, flow, price
from trading_research_agents.features.core import (
    Feature,
    FeatureSpec,
    FeatureVector,
    Inputs,
    Value,
    compute,
)

FLOW = frozenset({"flow_1m"})
DERIV = frozenset({"flow_1m", "deriv"})
CONTEXT = frozenset({"flow_1m", "context"})
BOOK = frozenset({"book_1m"})
EVENTS = frozenset({"deriv"})


def _feature(name: str, unit: str, bars: int, requires: frozenset[str], value: Value) -> Feature:
    return Feature(FeatureSpec(name, unit, bars, requires), value)


DEFAULT_FEATURES: tuple[Feature, ...] = (
    _feature("ret_bps_1", "bps", 2, FLOW, price.ret_bps(1)),
    _feature("ret_bps_4", "bps", 5, FLOW, price.ret_bps(4)),
    _feature("ret_bps_24", "bps", 25, FLOW, price.ret_bps(24)),
    _feature("mom_z_4", "sd", 97, FLOW, price.mom_z(4)),
    _feature("mom_z_24", "sd", 97, FLOW, price.mom_z(24)),
    _feature("vol_bps", "bps", 97, FLOW, price.vol_bps),
    _feature("range_vol_bps", "bps", 96, FLOW, price.range_vol_bps),
    _feature("atr_bps", "bps", 96, FLOW, price.atr_bps),
    _feature("zscore", "sd", 60, FLOW, price.zscore),
    _feature("vwap_gap_bps", "bps", 60, FLOW, price.vwap_gap_bps),
    _feature("position_in_range", "fraction", 60, FLOW, price.position_in_range),
    _feature("window_move_bps", "bps", 60, FLOW, price.window_move_bps),
    _feature("trend_strength", "ratio", 60, FLOW, price.trend_strength_of),
    _feature("variance_ratio_4", "ratio", 60, FLOW, price.variance_ratio_of),
    _feature("regime", "label", 60, FLOW, price.regime_of),
    _feature("taker_imbalance_4", "fraction", 4, FLOW, flow.taker_imbalance(4)),
    _feature("taker_imbalance_24", "fraction", 24, FLOW, flow.taker_imbalance(24)),
    _feature("delta_z_24", "sd", 24, FLOW, flow.delta_z_24),
    _feature("trade_intensity_z_24", "sd", 24, FLOW, flow.trade_intensity_z_24),
    _feature("large_trade_share_24", "fraction", 24, FLOW, flow.large_trade_share_24),
    _feature("funding_rate", "fraction", 0, DERIV, derivatives.funding_rate),
    _feature("funding_z_30d", "sd", 0, DERIV, derivatives.funding_z_30d),
    _feature("basis_bps", "bps", 0, DERIV, derivatives.basis_bps),
    _feature("oi_change_pct_4", "pct", 4, DERIV, derivatives.oi_change_pct(4)),
    _feature("oi_change_pct_24", "pct", 24, DERIV, derivatives.oi_change_pct(24)),
    _feature("oi_price_state", "label", 5, DERIV, derivatives.oi_price_state),
    _feature("long_short_ratio", "ratio", 0, DERIV, derivatives.long_short_ratio),
    _feature("liq_imbalance_24", "fraction", 24, EVENTS, derivatives.liq_imbalance_24),
    _feature("spread_bps", "bps", 1, BOOK, book.spread_bps),
    _feature("book_imbalance", "fraction", 1, BOOK, book.book_imbalance),
    _feature("depth_ratio_10bps", "ratio", 1, BOOK, book.depth_ratio_10bps),
    _feature("beta_btc_96", "ratio", 97, CONTEXT, cross.beta_btc),
    _feature("corr_btc_96", "ratio", 97, CONTEXT, cross.corr_btc),
    _feature("rel_strength_btc_24", "bps", 25, CONTEXT, cross.rel_strength_btc),
)
MAX_WINDOW_BARS = max(f.spec.window_bars for f in DEFAULT_FEATURES)


def compute_features(
    inputs: Inputs, features: Sequence[Feature] = DEFAULT_FEATURES
) -> FeatureVector:
    """Every feature in `features` at `inputs.at_ns`, by name, in catalog order."""
    return compute(features, inputs)
