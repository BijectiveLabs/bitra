"""A reviewer's words count only when they rest on a figure it was given.

The check is the whole point of the role: three stances can argue as well as they like, and code
keeps only the claims that cite a figure from the brief with that figure's value. A verdict with no
surviving claim becomes abstain, because a verdict resting on nothing is nothing.
"""

from __future__ import annotations

import json

import pytest

from trading_research_agents.agents.reviewer import (
    ROLE,
    SCHEMA,
    STANCES,
    checked,
    figures_of,
    parse,
    reviewer_brief,
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
        "spread_bps": "null",
        "bars": "60",
    }
    # A list is not a figure and the mechanism is prose; a figure the snapshot did not have is a
    # fact about the snapshot, and a reader may point at it.
    assert "candles" not in figures and "mechanism" not in figures


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
    assert ROLE == "reviewer" and STANCES == ("pro", "contra", "neutral")

    with pytest.raises(FormError, match="stance must be one of"):
        parse(answer(stance="bullish"))
    with pytest.raises(FormError, match="verdict must be one of"):
        parse(answer(verdict="yes"))
    with pytest.raises(FormError, match="outside the schema"):
        parse(json.dumps({**json.loads(answer()), "size": "0.01"}))
    with pytest.raises(FormError, match="claim 1 figure"):
        parse(answer(claims=[{"figure": "", "value": "1", "says": "nothing"}]))


def test_the_brief_carries_the_proposal_and_its_figures_and_nothing_else() -> None:
    brief = reviewer_brief(
        instrument="BTCUSDT",
        stance="neutral",
        proposal={"target": "long", "confidence": 0.71},
        figures=figures_of(BRIEF),
    )

    assert set(brief) == {
        "instrument",
        "stance",
        "proposal",
        "figures",
    }
    # The cost and the desk's rules are code's question, so they are not in a reviewer's brief.
    assert brief["stance"] == "neutral" and "cost_bps" not in brief


SERIES_BRIEF = {
    "instrument": "BTCUSDT",
    "series": [
        {
            "series": "us10y",
            "state": "bullish",
            "value": "4.02",
            "correlation_measured": {"value": -0.0, "n": 60, "window": "60 steps"},
            "zscore_vs_recent": {"value": 0.7559, "n": 3, "window": "30 points"},
        },
        {"series": "dxy", "state": "bearish", "value": "97.4"},
    ],
    "bars": [{"close": "100"}, {"close": "101"}],
}


def test_a_row_that_names_the_thing_it_is_about_makes_its_figures_citable() -> None:
    from trading_research_agents.agents.cited import cites, resolve

    figures = figures_of(SERIES_BRIEF)
    assert figures["series.us10y.value"] == "4.02"
    assert figures["series.dxy.state"] == "bearish"
    assert figures["series.us10y.zscore_vs_recent.n"] == "3"
    # Sixty bars name nothing, so they stay out rather than burying every other figure.
    assert not any(name.startswith("bars.") for name in figures)

    # The tail of exactly one name, however the model spelled the separator.
    assert resolve("us10y value", figures) == ("series.us10y.value", "")
    assert resolve("dxy.state", figures) == ("series.dxy.state", "")
    # A block measured with its own n resolves to the number in it.
    assert resolve("us10y zscore_vs_recent", figures) == (
        "series.us10y.zscore_vs_recent.value",
        "",
    )
    # Several names share the tail "value"; the shallowest is the one a reader means.
    assert resolve("us10y.value", figures) == ("series.us10y.value", "")
    # Ambiguous across rows, and named as such rather than guessed at.
    name, why = resolve("state", figures)
    assert not name and "series.dxy.state or series.us10y.state" in why
    assert cites("us10y value", "4.02", figures) == ""
    assert "was 4.02, not 9.99" in cites("us10y value", "9.99", figures)
    assert "not a figure in the brief" in cites("gold price", "1", figures)


def test_a_name_written_as_one_word_resolves_and_a_different_name_still_does_not() -> None:
    from trading_research_agents.agents.cited import cites, resolve

    figures = figures_of(SERIES_BRIEF)
    # The brief writes a path; a reader writes one word. Same figure, and the value still decides.
    assert resolve("us10y_value", figures) == ("series.us10y.value", "")
    assert resolve("us10y_zscore_vs_recent", figures) == (
        "series.us10y.zscore_vs_recent.value",
        "",
    )
    assert cites("us10y_value", "4.02", figures) == ""
    assert "was 4.02, not 1" in cites("us10y_value", "1", figures)
    # A figure whose own name carries an underscore resolves to itself before any of that.
    assert resolve("volume_ratio", {"volume_ratio": "1.0", "bars.volume_ratio": "2.0"}) == (
        "volume_ratio",
        "",
    )
    # A name the brief does not carry is still refused, however it is spelled.
    assert "not a figure" in cites("us10y_correlation", "-0.0", figures)


def test_a_desk_that_still_says_validator_runs_unchanged() -> None:
    from decimal import Decimal

    from test_config import AGENTS, agent_raw
    from trading_research_agents.trading.config import parse_strategy_config
    from trading_research_agents.trading.config_agents import renamed_in
    from trading_research_agents.trading.config_values import ConfigError

    raw = agent_raw(
        agents={
            **AGENTS,
            "validators": ["pro", "contra"],
            "on_missing_validator": "continue",
            "roles": {"validator": {"model": "m-review"}},
        },
        conviction={"validator_weight": Decimal("0.2")},
    )
    cfg = parse_strategy_config(raw)
    assert cfg.agents is not None and cfg.agents.reviewers == ("pro", "contra")
    assert cfg.agents.on_missing_reviewer == "continue"
    assert cfg.agents.for_role("reviewer").model == "m-review"
    assert cfg.conviction.reviewer_weight == Decimal("0.2")
    notes = renamed_in(raw)
    assert "[agents].validators is [agents].reviewers since 0.12" in notes
    assert "[conviction].validator_weight is [conviction].reviewer_weight since 0.12" in notes
    twice = agent_raw(agents={**AGENTS, "validators": ["pro"], "reviewers": ["pro"]})
    with pytest.raises(ConfigError, match="names reviewers twice"):
        parse_strategy_config(twice)


def test_a_cycle_recorded_with_validators_still_shows_them() -> None:
    from trading_research_agents.cli.tui_panels import _reviewer_table
    from trading_research_agents.execution.hindsight import gate_of

    record = {"context": {"validator_pro": {"stance": "pro", "verdict": "support"}}}
    assert _reviewer_table(record)
    assert gate_of({"refusals": ["validator_contra: opposes"]}) == "reviewers"
