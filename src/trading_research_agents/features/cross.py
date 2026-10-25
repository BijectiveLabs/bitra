"""How one market moves with the reference market, BTCUSDT, over the same closed bars.

- `beta_btc_96`: cov(r, r_ref) / var(r_ref) over the last 96 one-bar log returns.
- `corr_btc_96`: their correlation.
- `rel_strength_btc_24`: the market's 24-bar return minus the reference's, in basis points.

Both windows must cover the same bars; a reference that is missing, stale or out of step makes the
figure absent rather than computed against the wrong minutes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import pairwise

from trading_research_agents.features.core import Absent, FeatureValue, Inputs, closes, window
from trading_research_agents.market_data.bars import FlowBar

REFERENCE = "BTCUSDT"
CROSS_BARS = 96
RELATIVE_BARS = 24
BPS = 1e4


def _pair(inputs: Inputs, count: int) -> tuple[list[FlowBar], list[FlowBar]] | Absent:
    own = window(inputs.bars, inputs.at_ns, count)
    if isinstance(own, Absent):
        return own
    reference = inputs.context.get(REFERENCE)
    if reference is None:
        return Absent(f"no {REFERENCE} bars in the context")
    ref = window(reference, inputs.at_ns, count)
    if isinstance(ref, Absent):
        return Absent(f"{REFERENCE}: {ref.reason}")
    if [b.start_ns for b in own] != [b.start_ns for b in ref]:
        return Absent(f"the {REFERENCE} bars do not line up with this market's")
    return own, ref


def _returns(bars: Sequence[FlowBar]) -> list[float]:
    return [math.log(b / a) for a, b in pairwise(closes(bars))]


def _cov(xs: Sequence[float], ys: Sequence[float]) -> float:
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (len(xs) - 1)


def beta_btc(inputs: Inputs) -> FeatureValue:
    pair = _pair(inputs, CROSS_BARS + 1)
    if isinstance(pair, Absent):
        return pair
    own, ref = _returns(pair[0]), _returns(pair[1])
    spread = _cov(ref, ref)
    if spread <= 0:
        return Absent(f"{REFERENCE} did not move over the window")
    return _cov(own, ref) / spread


def corr_btc(inputs: Inputs) -> FeatureValue:
    pair = _pair(inputs, CROSS_BARS + 1)
    if isinstance(pair, Absent):
        return pair
    own, ref = _returns(pair[0]), _returns(pair[1])
    spread = math.sqrt(_cov(own, own) * _cov(ref, ref))
    if spread <= 0:
        return Absent("one of the two markets did not move over the window")
    return _cov(own, ref) / spread


def rel_strength_btc(inputs: Inputs) -> FeatureValue:
    pair = _pair(inputs, RELATIVE_BARS + 1)
    if isinstance(pair, Absent):
        return pair
    own, ref = closes(pair[0]), closes(pair[1])
    return ((own[-1] / own[0] - 1) - (ref[-1] / ref[0] - 1)) * BPS
