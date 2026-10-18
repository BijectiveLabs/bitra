"""Market structure: swings, their labels, the trend they make, and what the closes broke."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from trading_research_agents.agents.technical_analyst.tools import features
from trading_research_agents.execution.regime import misfit
from trading_research_agents.execution.structure import against, read, swings
from trading_research_agents.strategy import Bar

MINUTE = 60_000_000_000
STEP_UP = [100, 103, 106, 103, 100, 104, 108, 105, 102, 106, 110, 107, 104, 108, 112, 109, 106]


def bars(closes: Sequence[float]) -> list[Bar]:
    out = []
    for i, c in enumerate(closes):
        price = Decimal(str(c))
        out.append(
            Bar(i * MINUTE, (i + 1) * MINUTE, price, price + 1, price - 1, price, Decimal(1), 3)
        )
    return out


def test_swings_alternate_and_carry_their_labels() -> None:
    found = swings(bars(STEP_UP))
    assert [s.kind for s in found] == ["high", "low", "high", "low", "high", "low", "high"]
    assert [s.label for s in found] == ["", "", "HH", "HL", "HH", "HL", "HH"]
    assert [s.price for s in found if s.kind == "high"] == [
        Decimal(p) for p in (107, 109, 111, 113)
    ]
    # Two highs with no low between them are one swing, the higher.
    double = swings(bars([100, 102, 105, 102, 104, 106, 103, 101, 99, 97, 99, 101]))
    assert [(s.kind, s.price) for s in double][:2] == [("high", Decimal(107)), ("low", Decimal(96))]


def test_higher_highs_and_lows_are_an_uptrend_and_the_mirror_a_downtrend() -> None:
    up = read(bars(STEP_UP))
    assert up.trend == "uptrend" and up.invalidation == Decimal(103)
    down = read(bars([200 - c for c in STEP_UP]))
    assert down.trend == "downtrend" and down.invalidation == Decimal(97)
    rec = up.record()
    assert rec["swings"][-1] == {"kind": "high", "label": "HH", "price": "113", "bars_ago": 2}
    assert rec["support"] == "103" and rec["resistance"] == "113"
    assert read(bars([100, 101, 102])).trend == "unclear"


def test_a_close_past_the_last_swing_is_a_break_or_a_change_of_character() -> None:
    went_on = read(bars([*STEP_UP, 110, 114]))
    assert went_on.event == "bullish break of structure" and went_on.event_level == Decimal(113)
    assert went_on.event_bars_ago == 0
    turned = read(bars([*STEP_UP, 104, 101]))
    assert turned.event == "bearish change of character" and turned.event_level == Decimal(103)
    # A wick through the level is not a break; only a close is.
    assert read(bars([*STEP_UP, 108])).event is None


def test_a_range_breaks_out_of_its_edges() -> None:
    contracting = [100, 110, 120, 110, 100, 108, 116, 108, 102, 108, 114, 108, 104]
    shape = read(bars(contracting))
    assert shape.trend == "range"
    assert read(bars([*contracting, 120])).event == "break above the range"


def test_a_trend_setup_against_the_structure_is_refused_until_it_changes_character() -> None:
    up = read(bars(STEP_UP)).record()
    assert against("short", up) and not against("long", up)
    why = misfit("trend_continuation", "short", "trending down", structure=up)
    assert "structure is an uptrend" in why
    turned = read(bars([*STEP_UP, 104, 101])).record()
    assert not against("short", turned)
    assert not against("long", None)


def test_a_fade_into_a_fresh_break_is_refused() -> None:
    broke = read(bars([*STEP_UP, 110, 114])).record()
    why = misfit("range_reversion", "short", "ranging", zscore=1.5, structure=broke)
    assert "into a bullish break of structure 0 bar(s) ago" in why
    assert not misfit("range_reversion", "long", "ranging", zscore=-1.5, structure=broke)
    stale = dict(broke, event_bars_ago=4)
    assert not misfit("range_reversion", "short", "ranging", zscore=1.5, structure=stale)


def test_the_brief_and_every_timeframe_carry_the_structure() -> None:
    f = features(bars(STEP_UP))
    assert f is not None
    shown = f.record()["market_structure"]
    assert shown["trend"] == "uptrend" and shown["invalidation"] == "103"
    assert shown["support_bps"] == "283.02" and shown["resistance_bps"] == "660.38"
