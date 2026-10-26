from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest

from trading_research_agents.features.catalog import (
    DEFAULT_FEATURES,
    MAX_WINDOW_BARS,
    compute_features,
)
from trading_research_agents.features.core import Absent, Inputs
from trading_research_agents.features.cross import beta_btc, corr_btc, rel_strength_btc
from trading_research_agents.market_data.bars import FlowBar, GapState
from trading_research_agents.market_data.days import midnight_ns

HOUR = 3_600_000_000_000
T0 = midnight_ns(date(2025, 7, 1))


def bars_of(closes: list[float], instrument: str, offset: int = 0) -> list[FlowBar]:
    out = []
    for i, value in enumerate(closes):
        price = Decimal(repr(value))
        out.append(
            FlowBar(
                venue="v",
                instrument=instrument,
                start_ns=T0 + (i + offset) * HOUR,
                end_ns=T0 + (i + offset + 1) * HOUR,
                open=price,
                high=price,
                low=price,
                close=price,
                volume_base=Decimal(1),
                volume_quote=price,
                taker_buy_base=Decimal(1),
                taker_sell_base=Decimal(0),
                trade_count=1,
                large_trade_base=None,
                late_trades=0,
                gap=GapState.NONE,
                source="s",
            )
        )
    return out


def walk(scale: float) -> list[float]:
    price, out = 100.0, [100.0]
    for i in range(1, 97):
        price *= math.exp(scale * (0.003 if i % 3 else -0.004))
        out.append(price)
    return out


def test_beta_and_correlation_against_the_reference() -> None:
    own = bars_of(walk(2.0), "ETHUSDT")
    ref = bars_of(walk(1.0), "BTCUSDT")
    inputs = Inputs(at_ns=own[-1].end_ns, interval_ns=HOUR, bars=own, context={"BTCUSDT": ref})
    assert beta_btc(inputs) == pytest.approx(2.0, rel=1e-6)
    assert corr_btc(inputs) == pytest.approx(1.0, rel=1e-6)
    a, b = walk(2.0), walk(1.0)
    expected = ((a[-1] / a[-25] - 1) - (b[-1] / b[-25] - 1)) * 1e4
    assert rel_strength_btc(inputs) == pytest.approx(expected)


def test_without_the_reference_the_figures_say_so() -> None:
    own = bars_of(walk(2.0), "ETHUSDT")
    alone = Inputs(at_ns=own[-1].end_ns, interval_ns=HOUR, bars=own)
    missing = beta_btc(alone)
    assert isinstance(missing, Absent) and "no BTCUSDT bars" in missing.reason
    behind = bars_of(walk(1.0), "BTCUSDT")[:-1]
    stale = Inputs(at_ns=own[-1].end_ns, interval_ns=HOUR, bars=own, context={"BTCUSDT": behind})
    found = corr_btc(stale)
    assert isinstance(found, Absent) and found.reason.startswith("BTCUSDT:")


def test_the_catalog_names_each_feature_once_and_computes_all() -> None:
    names = [f.spec.name for f in DEFAULT_FEATURES]
    assert len(names) == len(set(names)) == 34
    assert MAX_WINDOW_BARS == 97
    own = bars_of(walk(2.0), "ETHUSDT")
    vector = compute_features(
        Inputs(at_ns=own[-1].end_ns, interval_ns=HOUR, bars=own, context={"BTCUSDT": own})
    )
    assert list(vector) == names
    assert isinstance(vector["funding_rate"], Absent)
    assert isinstance(vector["mom_z_24"], float)
