from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_research_agents.llm.cache import CacheError, CacheMissError, CacheMode
from trading_research_agents.llm.judge import (
    Answer,
    CachedJudge,
    Check,
    Checked,
    FakeJudge,
    Grade,
    Graded,
    JudgeRefusedError,
    Pick,
    Picked,
    Question,
    judge_key,
)

KIND = Pick("Which kind of event does the headline report?", {"hack": "funds stolen", "none": "no"})
SEVERITY = Grade("How severe is it?", ["minor", "serious", "critical"])
ABOUT_BTC = Check("Does it concern bitcoin?")
QUESTIONS: dict[str, Question] = {"kind": KIND, "severity": SEVERITY, "btc": ABOUT_BTC}
ANSWERS: dict[str, Answer] = {
    "kind": Picked("hack", 0.9, {"hack": 0.95, "none": 0.05}),
    "severity": Graded(1.4, 0.6, {0: 0.1, 1: 0.4, 2: 0.5}),
    "btc": Checked(0.2),
}
STATE = {"headline": "exchange drained of 40m in hot wallet exploit"}


def test_a_question_is_refused_when_it_cannot_be_asked() -> None:
    with pytest.raises(ValueError, match="says what it asks"):
        Check("  ")
    with pytest.raises(ValueError, match="1 to 255 options"):
        Pick("which?", {})
    with pytest.raises(ValueError, match="1 to 10 levels"):
        Grade("how much?", [str(i) for i in range(11)])


def test_the_fake_judge_answers_what_was_scripted_and_refuses_the_rest() -> None:
    judge = FakeJudge(ANSWERS)
    said = judge.evaluate(STATE, QUESTIONS)
    assert said["kind"] == ANSWERS["kind"] and said.model == "fake-judge-1.0.0"
    assert isinstance(said["kind"], Picked) and said["kind"].probability("none") == 0.05
    with pytest.raises(JudgeRefusedError, match="no scripted answer"):
        judge.evaluate(STATE, {"other": ABOUT_BTC})


def test_a_recorded_verdict_is_answered_from_disk_and_a_replay_never_asks(tmp_path: Path) -> None:
    inner = FakeJudge(ANSWERS)
    recording = CachedJudge(inner, tmp_path, mode=CacheMode.RECORD, clock=lambda: 7)
    first = recording.evaluate(STATE, QUESTIONS)
    again = recording.evaluate(STATE, QUESTIONS)
    assert len(inner.asked) == 1 and again.from_cache and again.answers == first.answers

    nobody = FakeJudge({})
    replay = CachedJudge(nobody, tmp_path, mode=CacheMode.REPLAY)
    assert replay.evaluate(STATE, QUESTIONS).answers == first.answers and not nobody.asked
    with pytest.raises(CacheMissError, match="replay never asks a judge"):
        replay.evaluate({"headline": "another"}, QUESTIONS)


def test_a_verdict_edited_on_disk_is_refused(tmp_path: Path) -> None:
    CachedJudge(FakeJudge(ANSWERS), tmp_path, mode=CacheMode.RECORD).evaluate(STATE, QUESTIONS)
    key = judge_key("fake-judge-1.0.0", STATE, QUESTIONS)
    path = tmp_path / key[:2] / f"{key}.json"
    entry = json.loads(path.read_text("utf-8"))
    entry["state"] = {"headline": "something else"}
    path.write_text(json.dumps(entry), "utf-8")
    with pytest.raises(CacheError, match="does not match its key"):
        CachedJudge(FakeJudge({}), tmp_path, mode=CacheMode.REPLAY).evaluate(STATE, QUESTIONS)


def test_the_key_moves_with_the_model_the_state_and_the_questions() -> None:
    base = judge_key("jev-1.14.0", STATE, QUESTIONS)
    assert base != judge_key("jev-1.13.0", STATE, QUESTIONS)
    assert base != judge_key("jev-1.14.0", {"headline": "x"}, QUESTIONS)
    assert base != judge_key("jev-1.14.0", STATE, {"kind": KIND})


def test_an_answer_from_another_model_is_not_recorded(tmp_path: Path) -> None:
    class Drifted(FakeJudge):
        def evaluate(self, state, questions):  # type: ignore[no-untyped-def]
            made = super().evaluate(state, questions)
            return type(made)(made.answers, model="jev-1.15.0")

    cached = CachedJudge(Drifted(ANSWERS, model="jev-1.14.0"), tmp_path, mode=CacheMode.RECORD)
    with pytest.raises(JudgeRefusedError, match="answered; not recorded"):
        cached.evaluate(STATE, QUESTIONS)
    assert not list(tmp_path.rglob("*.json"))


def test_off_passes_every_call_through(tmp_path: Path) -> None:
    inner = FakeJudge(ANSWERS)
    off = CachedJudge(inner, tmp_path, mode=CacheMode.OFF)
    off.evaluate(STATE, QUESTIONS)
    off.evaluate(STATE, QUESTIONS)
    assert len(inner.asked) == 2 and not list(tmp_path.rglob("*.json"))
