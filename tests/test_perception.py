from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from trading_research_agents.features.text import KINDS
from trading_research_agents.llm.cache import CacheMissError
from trading_research_agents.llm.judge import (
    Answer,
    Checked,
    FakeJudge,
    JudgeUnauthorizedError,
    JudgeUnavailableError,
    Picked,
    Question,
    Verdicts,
)
from trading_research_agents.market_data.text import TextItem
from trading_research_agents.perception.text_events import perceive, questions_for

NS = 1_000_000_000
HACK = TextItem("coindesk", 100 * NS, 90 * NS, "exchange hot wallet drained of 40m", "CoinDesk")


def answers(
    kind_confidence: float = 0.8, instructions: float = 0.01, reversed_says: str = "hack"
) -> dict[str, Answer]:
    probabilities = dict.fromkeys(KINDS, 0.0) | {"hack": 0.9, "none": 0.1}
    second = dict.fromkeys(KINDS, 0.0) | {reversed_says: 0.7, "none": 0.3}
    if reversed_says == "none":
        second = dict.fromkeys(KINDS, 0.0) | {"none": 0.8, "hack": 0.2}
    return {
        "kind": Picked("hack", kind_confidence, probabilities),
        "kind_reversed": Picked(reversed_says, kind_confidence, second),
        "about_BTC": Checked(0.7),
        "about_market": Checked(0.4),
        "instructions": Checked(instructions),
    }


def test_the_questions_ask_the_kind_each_asset_the_market_and_the_guardrail() -> None:
    asked = questions_for(["BTC", "ETH"])
    assert set(asked) == {
        "kind",
        "kind_reversed",
        "about_BTC",
        "about_ETH",
        "about_market",
        "instructions",
    }
    # The same kinds in the opposite order: a first-option bias shows as two answers that differ.
    forward, backward = asked["kind"], asked["kind_reversed"]
    assert list(backward.options) == list(reversed(list(forward.options)))  # type: ignore[union-attr]


def test_a_headline_becomes_a_typed_event_from_what_was_sent_and_nothing_else() -> None:
    judge = FakeJudge(answers(), model="jev-1.14.0")
    seen = perceive(judge, [HACK], assets=["BTC"])
    (made,) = seen.events
    assert made.first_seen_ns == HACK.first_seen_ns and made.model == "jev-1.14.0"
    assert made.kinds["hack"] == pytest.approx(0.8) and made.concerns == {"BTC": 0.7, "market": 0.4}
    state, _ = judge.asked[0]
    # Only the headline and its publisher leave the desk: no time, no position, no account.
    assert state == {"headline": HACK.text, "source": HACK.source}


def test_an_unsure_kind_is_absent_rather_than_a_guess() -> None:
    seen = perceive(FakeJudge(answers(kind_confidence=0.2)), [HACK], assets=["BTC"])
    assert seen.events[0].kinds == {} and seen.events[0].concerns["BTC"] == 0.7


def test_text_that_speaks_to_a_machine_is_dropped_and_counted() -> None:
    seen = perceive(FakeJudge(answers(instructions=0.95)), [HACK], assets=["BTC"])
    assert seen.events == () and seen.guarded == 1


class Flaky:
    model = "jev-1.14.0"

    def __init__(self, error: Exception) -> None:
        self.error = error

    def evaluate(self, state: Any, questions: Mapping[str, Question]) -> Verdicts:
        raise self.error


def test_a_judge_that_did_not_answer_leaves_the_headline_absent_with_why() -> None:
    seen = perceive(Flaky(JudgeUnavailableError("timed out")), [HACK], assets=["BTC"])
    assert seen.events == () and "timed out" in seen.absent[0]


def test_a_refused_key_or_a_replay_miss_stops_rather_than_skipping() -> None:
    with pytest.raises(JudgeUnauthorizedError):
        perceive(Flaky(JudgeUnauthorizedError("401")), [HACK], assets=["BTC"])
    with pytest.raises(CacheMissError):
        perceive(Flaky(CacheMissError("no verdict")), [HACK], assets=["BTC"])


def test_a_kind_the_two_orders_disagree_on_is_absent_and_counted() -> None:
    seen = perceive(FakeJudge(answers(reversed_says="none")), [HACK], assets=["BTC"])
    assert seen.events[0].kinds == {} and seen.disagreed == 1
