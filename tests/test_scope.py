"""Every role inside its own job: what it is given, what it may answer, what is somebody else's.

The point of these tests is not the filter, it is the guarantee: a reviewing role never receives
equity, size, the controls or the exit distances, so it cannot reason about them however it is
prompted. The prose check is the weakest of the four edges and it is held to the weakest promise:
it records, it never silences.
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
from trading_research_agents.readings import READINGS

LATER = ("equity", "size", "max_position", "stop_frac", "take_profit")
ASKED = ("technical_analyst", "validator", "market_screener")


@pytest.mark.parametrize("role", ("validator", "market_screener"))
def test_a_reviewing_role_is_never_handed_a_figure_from_a_later_step(role: str) -> None:
    allowed = scope_of(role)
    for key in LATER:
        assert not allowed.keeps(key), f"{role} would be handed {key}"


@pytest.mark.parametrize("role", ASKED)
def test_every_asked_role_declares_what_it_decides(role: str) -> None:
    allowed = scope_of(role)
    assert allowed.reads and allowed.answers and allowed.decides.strip()


@pytest.mark.parametrize("name", READINGS)
def test_a_reading_has_no_scope_because_no_model_is_handed_it(name: str) -> None:
    with pytest.raises(ScopeError, match="no declared scope"):
        scope_of(name)


def test_the_filter_removes_what_is_not_this_roles_to_read() -> None:
    brief = {
        "instrument": "BTCUSDT",
        "stance": "contra",
        "proposal": {"target": "long"},
        "equity": "10000",
        "size": "0.01",
    }
    kept = brief_for("validator", brief)

    assert set(kept) == {"instrument", "stance", "proposal"}
    assert dropped("validator", brief) == ("equity", "size")


def test_the_technical_analyst_keeps_a_family_of_figures_but_not_the_size() -> None:
    brief = {"instrument": "BTCUSDT", "ret_1_bps": -12.0, "ret_20_bps": 3.0, "size": "0.01"}
    kept = brief_for("technical_analyst", brief)

    assert set(kept) == {"instrument", "ret_1_bps", "ret_20_bps"}


def test_a_role_with_no_declared_scope_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ScopeError, match="no declared scope"):
        scope_of("portfolio_manager")


def test_prose_that_reaches_into_a_later_step_is_recorded() -> None:
    said = {"verdict": "oppose", "reason": "I would go long here and set a stop loss below"}
    found = crossings("validator", said)

    assert any("an order" in f for f in found) and any("an exit" in f for f in found)
    # Nothing about the finding changes the verdict: the record is what the check is for.
    assert said["verdict"] == "oppose"


def test_the_vocabulary_leaves_ordinary_market_language_alone() -> None:
    said = {
        "verdict": "support",
        "reason": "the central bank said it would buy bonds; risk-off names the regime, and "
        "positioning is long into the release",
    }
    assert crossings("validator", said) == ()


def test_the_deciding_analyst_is_not_checked_for_the_words_of_its_own_job() -> None:
    said = {"target": "long", "reason": "the mechanism resolves long from here"}
    assert crossings("technical_analyst", said) == ()


def test_a_validator_may_name_the_role_whose_proposal_it_reviews() -> None:
    said = {"reason": "the technical analyst's figure holds, and the burst is not finished"}
    assert crossings("validator", said) == ()

    reaching = {"reason": "the position size should be halved"}
    assert crossings("validator", reaching)


def test_a_rule_no_role_was_told_is_recorded_for_every_role() -> None:
    invented = {"reason": "the desk requires a reward to risk of 3 before any entry"}
    for role in ("technical_analyst", "validator"):
        found = crossings(role, invented)
        assert any("a rule of the desk" in f for f in found), role
    # Describing the market's own numbers is not stating a rule.
    plain = {"reason": "the move clears its recent range and the spread stays tight"}
    assert crossings("technical_analyst", plain) == ()


def test_the_vocabulary_is_small_and_says_which_step_it_belongs_to() -> None:
    # A wide list would catch ordinary sentences; the promise here is that it stays deliberate.
    assert set(NOT_YOURS) == {"an order", "a size", "an exit", "another role"}
    assert all(phrase.islower() for phrases in NOT_YOURS.values() for phrase in phrases)
    assert "buy" not in {p.strip() for phrases in NOT_YOURS.values() for p in phrases}
    assert set(SCOPES) >= set(ASKED)
    assert not set(SCOPES) & set(READINGS)
