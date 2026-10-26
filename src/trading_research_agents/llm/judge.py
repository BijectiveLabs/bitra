"""A fast typed judge: text in, a probability per answer out, and no generated text.

A reasoning model writes sentences; a judge of this kind answers closed questions about a piece of
text with a distribution over the answers it was offered. Three question types, named for what
they ask rather than for any vendor's spelling:

    Pick    one of a set of named options, with a probability for each
    Grade   a position on an ordered list of levels, with a probability for each level
    Check   the probability that a statement holds

Every answer carries the versioned model that gave it, so a threshold fitted on one model is never
silently applied to another. Nothing above this package imports a vendor type: an adapter in
`llm/providers` translates. A judge's failure is a `JudgeError`, and its caller treats it as an
absent answer, never as a "no".

`CachedJudge` keys an answer by the model, the state and the questions, so a replay asks nothing
and a published result can be reproduced without a key.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from trading_research_agents.llm.cache import CacheError, CacheMissError, CacheMode
from trading_research_agents.records import write_json

MAX_OPTIONS = 255
MAX_LEVELS = 10


class JudgeError(RuntimeError):
    """No usable answer. The caller records the reason and reads the answer as absent."""


class JudgeUnavailableError(JudgeError):
    """The judge did not answer in time, or asked to slow down past the deadline."""


class JudgeRefusedError(JudgeError):
    """The request was refused as malformed, or the answer did not fit the questions asked."""


class JudgeUnauthorizedError(JudgeError):
    """The key is missing, wrong or revoked; nothing more will be answered this run."""


@dataclass(frozen=True)
class Pick:
    instructions: str
    options: Mapping[str, str]

    def __post_init__(self) -> None:
        _instructed(self.instructions)
        if not 1 <= len(self.options) <= MAX_OPTIONS:
            raise ValueError(f"a pick offers 1 to {MAX_OPTIONS} options")


@dataclass(frozen=True)
class Grade:
    instructions: str
    levels: Sequence[str]

    def __post_init__(self) -> None:
        _instructed(self.instructions)
        if not 1 <= len(self.levels) <= MAX_LEVELS:
            raise ValueError(f"a grade has 1 to {MAX_LEVELS} levels")


@dataclass(frozen=True)
class Check:
    instructions: str
    yes: str | None = None
    no: str | None = None

    def __post_init__(self) -> None:
        _instructed(self.instructions)


Question = Pick | Grade | Check


@dataclass(frozen=True)
class Picked:
    label: str
    confidence: float
    probabilities: Mapping[str, float]

    def probability(self, label: str) -> float:
        return float(self.probabilities.get(label, 0.0))


@dataclass(frozen=True)
class Graded:
    # The probability-weighted level, from 0 (the first level) to the number of levels less one.
    level: float
    confidence: float
    probabilities: Mapping[int, float]


@dataclass(frozen=True)
class Checked:
    probability: float


Answer = Picked | Graded | Checked


@dataclass(frozen=True)
class Verdicts:
    answers: Mapping[str, Answer]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    from_cache: bool = field(default=False, compare=False)

    def __getitem__(self, name: str) -> Answer:
        return self.answers[name]


class Judge(Protocol):
    model: str

    def evaluate(
        self, state: str | Mapping[str, Any], questions: Mapping[str, Question]
    ) -> Verdicts: ...


def _instructed(text: str) -> None:
    if not text.strip():
        raise ValueError("a question says what it asks")


class FakeJudge:
    """Answers scripted per question name, for tests; every request is kept for inspection."""

    def __init__(self, answers: Mapping[str, Answer], *, model: str = "fake-judge-1.0.0") -> None:
        self.answers = dict(answers)
        self.model = model
        self.asked: list[tuple[str | Mapping[str, Any], Mapping[str, Question]]] = []

    def evaluate(
        self, state: str | Mapping[str, Any], questions: Mapping[str, Question]
    ) -> Verdicts:
        self.asked.append((state, questions))
        missing = sorted(set(questions) - set(self.answers))
        if missing:
            raise JudgeRefusedError(f"no scripted answer for {missing}")
        return Verdicts({name: self.answers[name] for name in questions}, model=self.model)


def question_record(question: Question) -> dict[str, Any]:
    """A question as plain data: what the cache key and the record are made of."""
    if isinstance(question, Pick):
        return {"pick": question.instructions, "options": dict(question.options)}
    if isinstance(question, Grade):
        return {"grade": question.instructions, "levels": list(question.levels)}
    return {"check": question.instructions, "yes": question.yes, "no": question.no}


def answer_record(answer: Answer) -> dict[str, Any]:
    if isinstance(answer, Picked):
        return {
            "label": answer.label,
            "confidence": answer.confidence,
            "probabilities": dict(answer.probabilities),
        }
    if isinstance(answer, Graded):
        return {
            "level": answer.level,
            "confidence": answer.confidence,
            "probabilities": {str(k): v for k, v in answer.probabilities.items()},
        }
    return {"probability": answer.probability}


def _answer_of(question: Question, raw: Mapping[str, Any]) -> Answer:
    if isinstance(question, Pick):
        return Picked(
            label=str(raw["label"]),
            confidence=float(raw["confidence"]),
            probabilities={str(k): float(v) for k, v in raw["probabilities"].items()},
        )
    if isinstance(question, Grade):
        return Graded(
            level=float(raw["level"]),
            confidence=float(raw["confidence"]),
            probabilities={int(k): float(v) for k, v in raw["probabilities"].items()},
        )
    return Checked(probability=float(raw["probability"]))


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def judge_key(model: str, state: str | Mapping[str, Any], questions: Mapping[str, Question]) -> str:
    asked = {name: question_record(q) for name, q in questions.items()}
    body = {"model": model, "state": state, "questions": asked}
    return hashlib.sha256(_canonical(body)).hexdigest()


class CachedJudge:
    """A `Judge` that answers from files, records into them, or neither, by mode.

    Entries live under `root/<key[:2]>/<key>.json`. The model in the key is the pinned model the
    inner judge was built with; an answer from another model is refused when it is recorded.
    """

    def __init__(
        self,
        inner: Judge,
        root: Path,
        *,
        mode: CacheMode,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self.inner = inner
        self.root = root
        self.mode = mode
        self.clock = clock
        self.model = inner.model

    def evaluate(
        self, state: str | Mapping[str, Any], questions: Mapping[str, Question]
    ) -> Verdicts:
        if self.mode is CacheMode.OFF:
            return self.inner.evaluate(state, questions)
        key = judge_key(self.model, state, questions)
        found = self._read(key, questions)
        if found is not None:
            return found
        if self.mode is CacheMode.REPLAY:
            raise CacheMissError(f"no cached verdict (key {key[:12]}); replay never asks a judge")
        made = self.inner.evaluate(state, questions)
        if made.model != self.model:
            raise JudgeRefusedError(f"asked {self.model} and {made.model} answered; not recorded")
        entry = {
            "key": key,
            "model": made.model,
            "state": state,
            "questions": {name: question_record(q) for name, q in questions.items()},
            "answers": {name: answer_record(a) for name, a in made.answers.items()},
            "usage": {"input_tokens": made.input_tokens, "output_tokens": made.output_tokens},
            "made_ns": self.clock(),
        }
        write_json(self._path(key), entry)
        return made

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def _read(self, key: str, questions: Mapping[str, Question]) -> Verdicts | None:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            again = judge_key(str(entry["model"]), entry["state"], questions)
            answers = {name: _answer_of(q, entry["answers"][name]) for name, q in questions.items()}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CacheError(f"{path.name}: not a verdict this version reads ({exc})") from exc
        if again != key or entry.get("key") != key:
            raise CacheError(f"{path.name} does not match its key; refused")
        usage = entry.get("usage") or {}
        return Verdicts(
            answers,
            model=str(entry["model"]),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            from_cache=True,
        )


__all__ = [
    "Answer",
    "CachedJudge",
    "Check",
    "Checked",
    "FakeJudge",
    "Grade",
    "Graded",
    "Judge",
    "JudgeError",
    "JudgeRefusedError",
    "JudgeUnauthorizedError",
    "JudgeUnavailableError",
    "Pick",
    "Picked",
    "Question",
    "Verdicts",
    "answer_record",
    "judge_key",
    "question_record",
]
