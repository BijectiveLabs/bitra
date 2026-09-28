"""A validator's words count only when they rest on a figure it was given.

The check is the whole point of the role: three stances can argue as well as they like, and code
keeps only the claims that cite a figure from the brief with that figure's value. A verdict with no
surviving claim becomes abstain, because a verdict resting on nothing is nothing.
"""

from __future__ import annotations

import json

import pytest

from trading_research_agents.agents.validator import (
    ROLE,
    SCHEMA,
    STANCES,
    checked,
    figures_of,
    parse,
    validator_brief,
)
from trading_research_agents.llm.form import FormError

BRIEF = {
    "instrument": "BTCUSDT",
    "mechanism": "short-horizon reversion after a burst of prints",
    "zscore": -1.631,
    "volume_ratio": 7.971,
    "vwap_gap_bps": -20.13,
    "spread_bps": None,
    "bars": 60,
    "candles": [{"close": "100"}],
}


def answer(**fields: object) -> str:
    payload: dict[str, object] = {
        "stance": "pro",
        "claims": [{"figure": "zscore", "value": "-1.631", "says": "far below its window"}],
        "verdict": "support",
        "reason": "the figures support the proposal",
    }
    payload.update(fields)
    return json.dumps(payload)


def test_only_scalar_figures_are_citable_and_nothing_is_renamed() -> None:
    figures = figures_of(BRIEF)

    assert figures == {
        "instrument": "BTCUSDT",
        "zscore": "-1.631",
        "volume_ratio": "7.971",
        "vwap_gap_bps": "-20.13",
        "bars": "60",
    }
    # A list is not a figure, a missing value is not a figure, and the mechanism is prose.
    assert "candles" not in figures and "spread_bps" not in figures and "mechanism" not in figures


def test_a_claim_that_cites_a_figure_with_its_value_survives() -> None:
    verdict = checked(parse(answer()), figures_of(BRIEF))

    assert verdict.verdict == "support" and verdict.discarded == ()
    assert [c.figure for c in verdict.kept] == ["zscore"]


def test_a_claim_may_round_the_figure_it_cites() -> None:
    said = answer(
        claims=[{"figure": "zscore", "value": "-1.63", "says": "two standard deviations below"}]
    )
    verdict = checked(parse(said), figures_of(BRIEF))

    assert verdict.kept and verdict.discarded == ()


def test_a_figure_that_is_not_in_the_brief_is_discarded_by_name() -> None:
    said = answer(
        claims=[{"figure": "open_interest_change", "value": "5", "says": "positioning is building"}]
    )
    verdict = checked(parse(said), figures_of(BRIEF))

    assert verdict.kept == ()
    assert verdict.discarded == ("'open_interest_change' is not a figure in the brief",)
    # With nothing left to stand on, the verdict is not the one the model wrote.
    assert verdict.verdict == "abstain" and "abstain" in verdict.reason


def test_a_value_that_does_not_match_the_brief_is_discarded() -> None:
    said = answer(claims=[{"figure": "volume_ratio", "value": "2.0", "says": "volume is ordinary"}])
    verdict = checked(parse(said), figures_of(BRIEF))

    assert verdict.kept == () and verdict.discarded == ("volume_ratio was 7.971, not 2.0",)


def test_one_good_claim_carries_the_verdict_and_the_bad_one_is_still_recorded() -> None:
    said = answer(
        verdict="oppose",
        stance="contra",
        claims=[
            {"figure": "volume_ratio", "value": "7.971", "says": "the burst is not finished"},
            {"figure": "funding_rate", "value": "0.0001", "says": "carry is against it"},
        ],
    )
    verdict = checked(parse(said), figures_of(BRIEF))

    assert verdict.verdict == "oppose" and verdict.stance == "contra"
    assert [c.figure for c in verdict.kept] == ["volume_ratio"]
    assert verdict.discarded == ("'funding_rate' is not a figure in the brief",)
    assert verdict.record()["claims"][0]["says"] == "the burst is not finished"


def test_a_made_up_number_is_not_a_citation() -> None:
    said = answer(claims=[{"figure": "zscore", "value": "nan", "says": "unreadable"}])
    verdict = checked(parse(said), figures_of(BRIEF))

    assert verdict.kept == () and verdict.verdict == "abstain"


def test_the_schema_is_closed_and_the_stance_and_verdict_are_named() -> None:
    assert SCHEMA["additionalProperties"] is False
    assert ROLE == "validator" and STANCES == ("pro", "contra", "neutral")

    with pytest.raises(FormError, match="stance must be one of"):
        parse(answer(stance="bullish"))
    with pytest.raises(FormError, match="verdict must be one of"):
        parse(answer(verdict="yes"))
    with pytest.raises(FormError, match="outside the schema"):
        parse(json.dumps({**json.loads(answer()), "size": "0.01"}))
    with pytest.raises(FormError, match="claim 1 figure"):
        parse(answer(claims=[{"figure": "", "value": "1", "says": "nothing"}]))


def test_the_brief_carries_the_proposal_and_the_declarations_and_nothing_else() -> None:
    brief = validator_brief(
        instrument="BTCUSDT",
        stance="neutral",
        proposal={"target": "long", "confidence": 0.71},
        figures=figures_of(BRIEF),
        declared_states={"macro_analyst": {"declared": "bullish"}},
        cost_bps="13.01",
    )

    assert set(brief) == {
        "instrument",
        "stance",
        "proposal",
        "figures",
        "declared_states",
        "cost_bps",
    }
    assert brief["stance"] == "neutral" and brief["cost_bps"] == "13.01"
