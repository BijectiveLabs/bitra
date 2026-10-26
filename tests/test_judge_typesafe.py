"""The TypeSafe adapter against recorded exchanges of the vendor's documented API; no network."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from trading_research_agents.llm.judge import (
    Check,
    Checked,
    Grade,
    Graded,
    JudgeRefusedError,
    JudgeUnauthorizedError,
    JudgeUnavailableError,
    Pick,
    Picked,
    Question,
)
from trading_research_agents.llm.providers.typesafe import TypeSafeJudge

KEY = "ts-test-key-0123456789"
QUESTIONS: dict[str, Question] = {
    "kind": Pick("Which kind of event?", {"hack": "funds stolen", "none": "nothing of the kind"}),
    "severity": Grade("How severe?", ["minor", "serious", "critical"]),
    "btc": Check("Does it concern bitcoin?", yes="it names bitcoin"),
}
ANSWERED: dict[str, Any] = {
    "model": "jev-1.14.0",
    "answers": {
        "kind": {
            "type": "choice",
            "choice": "hack",
            "confidence": 0.9,
            "probabilities": {"hack": 0.95, "none": 0.05},
        },
        "severity": {
            "type": "score",
            "score": 1.4,
            "confidence": 0.6,
            "legend": {"0": "minor", "1": "serious", "2": "critical"},
            "probabilities": {"0": 0.1, "1": 0.4, "2": 0.5},
        },
        "btc": {"type": "noul", "noul": 0.2},
    },
    "usage": {"input_tokens": 120, "output_tokens": 0},
}


class Wire:
    """What the vendor would answer, in order; every request is kept."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.sent: list[tuple[str, dict[str, Any], Mapping[str, str], float]] = []

    def __call__(
        self, url: str, body: bytes, headers: Mapping[str, str], timeout_s: float
    ) -> tuple[int, Mapping[str, str], bytes]:
        self.sent.append((url, json.loads(body), headers, timeout_s))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        status, payload, *rest = reply
        return status, (rest[0] if rest else {}), json.dumps(payload).encode()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def judge(wire: Wire, clock: Clock | None = None, **kwargs: Any) -> TypeSafeJudge:
    clock = clock or Clock()
    return TypeSafeJudge(
        "jev-1.14.0",
        post=wire,
        env={"TYPESAFE_API_KEY": KEY},
        clock=clock,
        sleep=clock.sleep,
        **kwargs,
    )


def test_the_request_is_the_documented_body_and_the_answers_are_typed() -> None:
    wire = Wire((200, ANSWERED))
    said = judge(wire).evaluate({"headline": "hot wallet drained"}, QUESTIONS)
    url, body, headers, _ = wire.sent[0]
    assert url == "https://api.typesafe.ai/v1/systemone"
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert body == {
        "model": "jev-1.14.0",
        "state": {"headline": "hot wallet drained"},
        "questions": {
            "kind": {
                "type": "choice",
                "instructions": "Which kind of event?",
                "criteria": {"hack": "funds stolen", "none": "nothing of the kind"},
            },
            "severity": {
                "type": "score",
                "instructions": "How severe?",
                "criteria": ["minor", "serious", "critical"],
            },
            "btc": {
                "type": "noul",
                "instructions": "Does it concern bitcoin?",
                "criteria": {"true": "it names bitcoin"},
            },
        },
    }
    assert said["kind"] == Picked("hack", 0.9, {"hack": 0.95, "none": 0.05})
    assert said["severity"] == Graded(1.4, 0.6, {0: 0.1, 1: 0.4, 2: 0.5})
    assert said["btc"] == Checked(0.2)
    assert said.model == "jev-1.14.0" and said.input_tokens == 120


def test_a_moving_alias_is_refused_because_it_changes_answers_without_a_code_change() -> None:
    for alias in ("jev-latest", "jev-preview"):
        with pytest.raises(ValueError, match="pinned"):
            TypeSafeJudge(alias, post=Wire(), env={"TYPESAFE_API_KEY": KEY})


def test_slow_down_is_waited_for_inside_the_deadline_then_answered() -> None:
    clock = Clock()
    wire = Wire((429, {}, {"Retry-After": "1"}), (529, {}), (200, ANSWERED))
    said = judge(wire, clock).evaluate("text", QUESTIONS)
    assert len(wire.sent) == 3 and clock.slept[0] == 1.0 and said.model == "jev-1.14.0"


def test_slow_down_past_the_deadline_is_unavailable_not_a_no() -> None:
    clock = Clock()
    wire = Wire(*[(429, {}, {"Retry-After": "5"})] * 5)
    with pytest.raises(JudgeUnavailableError, match="429"):
        judge(wire, clock, deadline_s=6.0).evaluate("text", QUESTIONS)
    assert sum(clock.slept) <= 6.0


def test_a_timeout_is_retried_and_then_unavailable() -> None:
    wire = Wire(*[TimeoutError("read timed out")] * 3)
    clock = Clock()
    with pytest.raises(JudgeUnavailableError, match="timed out"):
        judge(wire, clock, max_retries=2).evaluate("text", QUESTIONS)
    # Two waits between three attempts, and none after the last.
    assert len(wire.sent) == 3 and clock.slept == [0.5, 1.0]


def test_a_request_the_vendor_refuses_is_never_retried() -> None:
    detail = {
        "detail": [{"loc": ["body", "questions"], "msg": "Field required", "type": "missing"}]
    }
    wire = Wire((422, detail))
    with pytest.raises(JudgeRefusedError, match="Field required"):
        judge(wire).evaluate("text", QUESTIONS)
    assert len(wire.sent) == 1


def test_a_missing_or_refused_key_stops_the_judge() -> None:
    with pytest.raises(JudgeUnauthorizedError, match="TYPESAFE_API_KEY"):
        TypeSafeJudge("jev-1.14.0", post=Wire(), env={}).evaluate("text", QUESTIONS)
    with pytest.raises(JudgeUnauthorizedError, match="401"):
        judge(Wire((401, {"detail": "bad key"}))).evaluate("text", QUESTIONS)


def test_an_answer_that_does_not_fit_the_questions_is_refused() -> None:
    short = {**ANSWERED, "answers": {"kind": ANSWERED["answers"]["kind"]}}
    with pytest.raises(JudgeRefusedError, match="severity"):
        judge(Wire((200, short))).evaluate("text", QUESTIONS)
    wrong = {
        **ANSWERED,
        "answers": {**ANSWERED["answers"], "kind": {"type": "choice", "choice": "rumour"}},
    }
    with pytest.raises(JudgeRefusedError, match="kind"):
        judge(Wire((200, wrong))).evaluate("text", QUESTIONS)


def test_a_credential_in_the_state_never_leaves() -> None:
    wire = Wire((200, ANSWERED))
    judge(wire).evaluate({"note": f"key is {KEY}"}, QUESTIONS)
    assert KEY not in json.dumps(wire.sent[0][1])


def test_another_route_to_the_same_api_is_a_base_url() -> None:
    wire = Wire((200, ANSWERED))
    judge(wire, base_url="https://relay.example/api/").evaluate("text", QUESTIONS)
    assert wire.sent[0][0] == "https://relay.example/api/v1/systemone"
    with pytest.raises(ValueError, match="https"):
        TypeSafeJudge("jev-1.14.0", base_url="http://relay.example", post=wire, env={})
