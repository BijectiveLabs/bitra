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


def answers(kind_confidence: float = 0.8, instructions: float = 0.01) -> dict[str, Answer]:
    probabilities = dict.fromkeys(KINDS, 0.0) | {"hack": 0.9, "none": 0.1}
    return {
        "kind": Picked("hack", kind_confidence, probabilities),
        "about_BTC": Checked(0.7),
        "about_market": Checked(0.4),
        "instructions": Checked(instructions),
    }


def test_the_questions_ask_the_kind_each_asset_the_market_and_the_guardrail() -> None:
    asked = questions_for(["BTC", "ETH"])
    assert set(asked) == {"kind", "about_BTC", "about_ETH", "about_market", "instructions"}


def test_a_headline_becomes_a_typed_event_from_what_was_sent_and_nothing_else() -> None:
    judge = FakeJudge(answers(), model="jev-1.14.0")
    seen = perceive(judge, [HACK], assets=["BTC"])
    (made,) = seen.events
    assert made.first_seen_ns == HACK.first_seen_ns and made.model == "jev-1.14.0"
    assert made.kinds["hack"] == 0.9 and made.concerns == {"BTC": 0.7, "market": 0.4}
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
