"""Every role inside its own job: what it is given, what it may answer, what is somebody else's.

The point of these tests is not the filter, it is the guarantee: a reading analyst never receives
equity, size, the controls, the cost of the round trip or the exit distances, so it cannot reason
about them however it is prompted. The prose check is the weakest of the four edges and it is held
to the weakest promise: it records, it never silences.
"""

from __future__ import annotations

import pytest

from trading_research_agents.agents.scope import (
    NOT_YOURS,
    SCOPES,
    ScopeError,
    brief_for,
    crossings,
    dropped,
    scope_of,
)

LATER = ("equity", "size", "max_position", "round_trip_bps", "stop_sigma_frac", "take_profit")
READING = ("macro_analyst", "onchain_analyst", "sentiment_analyst")


@pytest.mark.parametrize("role", READING)
def test_a_reading_analyst_is_never_handed_a_figure_from_a_later_step(role: str) -> None:
    allowed = scope_of(role)
    for key in LATER:
        assert not allowed.keeps(key), f"{role} would be handed {key}"


@pytest.mark.parametrize("role", (*READING, "technical_analyst", "validator"))
def test_every_asked_role_declares_what_it_decides(role: str) -> None:
    allowed = scope_of(role)
    assert allowed.reads and allowed.answers and allowed.decides.strip()


def test_the_filter_removes_what_is_not_this_roles_to_read() -> None:
    brief = {
        "instrument": "BTCUSDT",
        "as_of": "2026-09-28T00:00:00+00:00",
        "series": [{"name": "us10y"}],
        "equity": "10000",
        "round_trip_bps": "13.01",
    }
    kept = brief_for("macro_analyst", brief)

    assert set(kept) == {"instrument", "as_of", "series"}
    assert dropped("macro_analyst", brief) == ("equity", "round_trip_bps")


def test_the_technical_analyst_keeps_a_family_of_figures_but_not_the_size() -> None:
    brief = {"instrument": "BTCUSDT", "ret_1_bps": -12.0, "ret_20_bps": 3.0, "size": "0.01"}
    kept = brief_for("technical_analyst", brief)

    assert set(kept) == {"instrument", "ret_1_bps", "ret_20_bps"}


def test_a_role_with_no_declared_scope_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ScopeError, match="no declared scope"):
        scope_of("portfolio_manager")


def test_prose_that_reaches_into_a_later_step_is_recorded() -> None:
    said = {"regime": "risk-off", "reason": "I would go long here and set a stop loss below"}
    found = crossings("macro_analyst", said)

    assert any("an order" in f for f in found) and any("an exit" in f for f in found)
    # Nothing about the finding changes the label: the answer was never the thing that decides.
    assert said["regime"] == "risk-off"


def test_the_vocabulary_leaves_ordinary_market_language_alone() -> None:
    said = {
        "state": "negative",
        "reason": "the central bank said it would buy bonds; risk-off names the regime, and "
        "positioning is long into the release",
    }
    assert crossings("sentiment_analyst", said) == ()


def test_the_deciding_analyst_is_not_checked_for_the_words_of_its_own_job() -> None:
    said = {"target": "long", "reason": "the mechanism resolves long from here"}
    assert crossings("technical_analyst", said) == ()


def test_a_validator_may_name_the_role_whose_proposal_it_reviews() -> None:
    said = {"reason": "the technical analyst's figure holds, and the burst is not finished"}
    assert crossings("validator", said) == ()

    reaching = {"reason": "the position size should be halved"}
    assert crossings("validator", reaching)


def test_the_vocabulary_is_small_and_says_which_step_it_belongs_to() -> None:
    # A wide list would catch ordinary sentences; the promise here is that it stays deliberate.
    assert set(NOT_YOURS) == {"an order", "a size", "an exit", "another role"}
    assert all(phrase.islower() for phrases in NOT_YOURS.values() for phrase in phrases)
    assert "buy" not in {p.strip() for phrases in NOT_YOURS.values() for p in phrases}
    assert set(SCOPES) >= {*READING, "technical_analyst", "validator"}
