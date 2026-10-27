"""How much capital goes behind an idea, from a rule the operator wrote.

The case that matters is the one a desk actually meets: macro and sentiment say one thing, on-chain
says the other, and the deciding analyst wants to trade anyway. Each voice counts as strong as it
actually is, conflicting context costs size in proportion, only opposition that outweighs the case
stops the trade, silence counts as neither agreement nor disagreement, and nothing here may ever
produce a size larger than the operator already allowed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from trading_research_agents.execution.conviction import (
    Conviction,
    ConvictionError,
    Score,
    Stake,
    Tier,
    score_of,
    stake_for,
)
from trading_research_agents.execution.sizing import SizingError, leverage_of, notional_cap

RULE = Conviction(
    weights={
        "macro": Decimal(1),
        "onchain": Decimal("1.5"),
        "sentiment": Decimal("0.5"),
        "structure_lead": Decimal(2),
    },
    validator_weight=Decimal(1),
    min_score=Decimal("0.4"),
    ladder=(
        Tier(Decimal("0.75"), Decimal(1)),
        Tier(Decimal("0.55"), Decimal("0.5")),
        Tier(Decimal("0.4"), Decimal("0.25")),
    ),
    max_leverage=Decimal(3),
)
PRICE = Decimal("100000")
BASE = Decimal("0.01")


def states(**declared: str) -> dict[str, dict[str, str]]:
    return {role: {"declared": word} for role, word in declared.items()}


def stake(score: Score, **overrides: Any) -> Stake:
    settings: dict[str, Any] = {
        "base_size": BASE,
        "price": PRICE,
        "max_position": Decimal(1),
        "equity": Decimal(10_000),
        "step": Decimal("0.001"),
    }
    settings.update(overrides)
    return stake_for(RULE, score, **settings)


def test_everything_agreeing_trades_the_whole_base_size() -> None:
    score = score_of(
        RULE,
        target="long",
        states=states(macro="bullish", onchain="bullish", sentiment="bullish"),
        confidence=0.9,
        votes=("support", "support", "abstain"),
    )
    assert score.score == Decimal("0.971") and score.against == ()

    put = stake(score)
    assert put.admitted and put.size == BASE and put.multiple == Decimal(1)
    assert put.leverage == Decimal("0.100")


def test_one_leg_pointing_the_other_way_costs_size() -> None:
    # The desk's own example: good news and a bullish press, whales sending coins to the venues.
    score = score_of(
        RULE,
        target="long",
        states=states(macro="bullish", onchain="bearish", sentiment="bullish"),
        confidence=0.7,
        votes=("support", "oppose", "abstain"),
    )

    assert score.agreed == ("macro", "sentiment", "structure_lead")
    assert score.against == ("onchain",)
    # A full band of opposition from the heaviest reading voice, and a validator against: 0.2.
    assert score.score == Decimal("0.200")

    put = stake(score)
    assert not put.admitted and put.size == Decimal(0)
    assert any("below the declared floor" in r for r in put.reasons)


def test_the_middle_of_the_ladder_trades_a_fraction() -> None:
    score = score_of(
        RULE,
        target="long",
        states=states(macro="bullish", onchain="bullish"),
        confidence=0.6,
        votes=(),
    )
    # Sentiment declared nothing, so it is silence rather than a vote either way.
    assert score.silent == ("sentiment",)
    # Two voices agreeing in full lift a 0.6 confidence to 3.7 of 4.5.
    assert score.score == Decimal("0.822")

    put = stake(score)
    assert put.admitted and put.multiple == Decimal(1) and put.size == BASE


def test_silence_is_never_counted_as_agreement() -> None:
    quiet = score_of(RULE, target="long", states={}, confidence=0.8, votes=())
    assert quiet.silent == ("macro", "onchain", "sentiment")
    # Only the deciding analyst spoke, so the score is its own confidence: silence neither raised it
    # nor diluted it. A reading barely past its line moves it barely, and silence is the limit of
    # that, so the two cannot be treated differently without a jump at zero.
    assert quiet.score == Decimal("0.800")
    assert quiet.agreed == ("structure_lead",)


def test_a_short_reads_the_declarations_the_other_way_round() -> None:
    score = score_of(
        RULE,
        target="short",
        states=states(macro="bearish", onchain="bearish", sentiment="bearish"),
        confidence=0.8,
        votes=(),
    )
    assert score.against == () and score.score == Decimal("0.920")
    assert stake(score).multiple == Decimal(1)


def test_the_leverage_ceiling_is_a_notional_cap_and_it_only_shrinks() -> None:
    score = score_of(
        RULE,
        target="long",
        states=states(macro="bullish", onchain="bullish", sentiment="bullish"),
        confidence=1.0,
        votes=(),
    )
    # 0.2 at 100000 is 20000 of notional against 5000 of equity: three times equity is the ceiling.
    put = stake(score, base_size=Decimal("0.2"), equity=Decimal(5_000))

    assert put.admitted and put.size == Decimal("0.15")
    assert put.capped_by == "max_notional" and put.leverage == Decimal("3.000")
    assert any("cut to" in r for r in put.reasons)


def test_the_position_cap_still_stands_above_the_ladder() -> None:
    score = score_of(RULE, target="long", states=states(macro="bullish"), confidence=1.0)
    put = stake(score, base_size=Decimal("0.5"), max_position=Decimal("0.2"), equity=None)

    assert put.size == Decimal("0.2") and put.capped_by == "max_position"
    assert put.leverage is None


def test_a_size_that_cannot_reach_one_step_is_refused_rather_than_rounded_up() -> None:
    score = score_of(RULE, target="long", states=states(macro="bullish"), confidence=1.0)
    put = stake(score, base_size=Decimal("0.0004"), step=Decimal("0.001"))

    assert not put.admitted and put.size == Decimal(0)
    assert any("nothing left to trade" in r for r in put.reasons)


def test_a_score_no_rung_covers_is_refused_by_name() -> None:
    rule = Conviction(
        weights={"structure_lead": Decimal(1)},
        ladder=(Tier(Decimal("0.95"), Decimal(1)),),
    )
    score = score_of(rule, target="long", states={}, confidence=0.5)
    put = stake_for(
        rule,
        score,
        base_size=BASE,
        price=PRICE,
        max_position=Decimal(1),
        step=Decimal("0.001"),
    )
    assert not put.admitted and any("no rung of the ladder" in r for r in put.reasons)


def test_the_ladder_is_read_from_the_top_however_it_was_written() -> None:
    rule = Conviction(
        weights={"structure_lead": Decimal(1)},
        ladder=(Tier(Decimal("0.3"), Decimal("0.25")), Tier(Decimal("0.8"), Decimal(1))),
    )
    assert [str(t.at_least) for t in rule.ladder] == ["0.8", "0.3"]
    assert rule.tier_for(Decimal("0.9")).size_multiple == Decimal(1)  # type: ignore[union-attr]
    assert rule.tier_for(Decimal("0.4")).size_multiple == Decimal("0.25")  # type: ignore[union-attr]
    assert rule.tier_for(Decimal("0.1")) is None


def test_a_rule_that_cannot_be_applied_is_refused_when_it_is_written() -> None:
    with pytest.raises(ConvictionError, match="is positive"):
        Conviction(weights={"macro": Decimal(0)})
    with pytest.raises(ConvictionError, match="min_score"):
        Conviction(min_score=Decimal("1.5"))
    with pytest.raises(ConvictionError, match="max_leverage"):
        Conviction(max_leverage=Decimal(0))
    with pytest.raises(ConvictionError, match="between 0 and 1"):
        Tier(Decimal("1.5"), Decimal(1))
    with pytest.raises(ConvictionError, match="size multiple is positive"):
        Tier(Decimal("0.5"), Decimal(0))
    with pytest.raises(ConvictionError, match="long or short"):
        score_of(RULE, target="flat", states={}, confidence=0.5)


def test_an_undeclared_rule_changes_nothing() -> None:
    assert Conviction().idle
    assert not RULE.idle


def test_the_caps_are_arithmetic_anyone_can_check() -> None:
    assert notional_cap(equity=Decimal(1000), max_leverage=Decimal(2)) == Decimal(2000)
    assert notional_cap(equity=None, max_leverage=Decimal(2)) is None
    assert notional_cap(equity=Decimal(1000), max_leverage=None) is None
    with pytest.raises(SizingError):
        notional_cap(equity=Decimal(0), max_leverage=Decimal(2))
    assert leverage_of(size=Decimal(1), price=Decimal(100), equity=Decimal(50)) == Decimal("2.000")
    assert leverage_of(size=Decimal(1), price=Decimal(100), equity=None) is None


def graded(**scores: str) -> dict[str, dict[str, str]]:
    """Declared states with their graded scores, as a brief now carries them."""
    return {
        role: {"declared": "bullish" if Decimal(value) > 0 else "bearish", "score": value}
        for role, value in scores.items()
    }


def test_weak_opposition_costs_a_little_size_and_the_trade_goes_ahead() -> None:
    # Seen live on 2026-10-04: a short at 0.70 against macro barely past its bullish line (+0.15)
    # and sentiment barely past its own (+0.057). As words that read as two full voices against;
    # as figures it is a small dent in a clear case.
    score = score_of(
        RULE,
        target="short",
        states=graded(macro="0.15", sentiment="0.057"),
        confidence=0.7,
    )
    assert score.against == ("macro", "sentiment")
    assert score.score == Decimal("0.561")
    put = stake(score)
    assert put.admitted and put.multiple == Decimal("0.5")


def test_mostly_agreeing_with_one_dissent_trades_a_middle_size() -> None:
    score = score_of(
        RULE,
        target="long",
        states=graded(macro="0.6", onchain="0.5", sentiment="-0.4"),
        confidence=0.7,
    )
    assert score.score == Decimal("0.718")
    assert stake(score).multiple == Decimal("0.5")


def test_opposition_that_outweighs_the_case_stops_the_trade() -> None:
    score = score_of(
        RULE,
        target="short",
        states=graded(macro="1", onchain="1", sentiment="1"),
        confidence=0.7,
    )
    assert score.score == Decimal("0.000")
    assert not stake(score).admitted


def test_a_stronger_reading_moves_the_score_further() -> None:
    weak = score_of(RULE, target="long", states=graded(macro="0.2"), confidence=0.6)
    strong = score_of(RULE, target="long", states=graded(macro="0.9"), confidence=0.6)
    against = score_of(RULE, target="long", states=graded(macro="-0.9"), confidence=0.6)
    assert against.score < Decimal("0.600") < weak.score < strong.score
