"""TypeSafe's Jev as a `Judge`, over its documented HTTP API with the standard library.

`POST <base>/v1/systemone` with `{model, state, questions}` and a bearer key. A `Pick` is sent as
`choice` (criteria: option name to description), a `Grade` as `score` (criteria: the ordered
levels), a `Check` as `noul` (criteria: what true and false mean, when given). Reference read
2026-10-26: docs.typesafe.ai/api.

The SDK is not used: one endpoint and a small schema do not justify its dependencies, and the
timeout and retry policy here must be ours. A moving alias (`jev-latest`, `jev-preview`) is refused,
because it would change answers without a code change and make every threshold fitted on it
meaningless. 429 and 529 are waited for, honouring `Retry-After`, inside one total deadline; any
other 4xx is our mistake and is never retried; a timeout says nothing was answered.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from trading_research_agents.llm.completer import redact
from trading_research_agents.llm.judge import (
    Answer,
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
    Verdicts,
)

BASE_URL = "https://api.typesafe.ai"
KEY_ENV = "TYPESAFE_API_KEY"
ALIASES = ("-latest", "-preview")
RETRYABLE = (429, 529)
BACKOFF_START_S = 0.5

# What a caller sends: the url, the body, the headers and a timeout; what it gets: the status,
# the headers and the body. A network failure raises OSError or TimeoutError.
Post = Callable[[str, bytes, Mapping[str, str], float], tuple[int, Mapping[str, str], bytes]]


def _post(
    url: str, body: bytes, headers: Mapping[str, str], timeout_s: float
) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            return int(response.status), dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), dict(exc.headers or {}), exc.read()


class TypeSafeJudge:
    def __init__(
        self,
        model: str,
        *,
        base_url: str = BASE_URL,
        key_env: str = KEY_ENV,
        timeout_s: float = 3.0,
        deadline_s: float = 6.0,
        max_retries: int = 2,
        post: Post = _post,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not model.strip() or model.endswith(ALIASES):
            raise ValueError(f"{model!r}: a judge model is pinned to a version, e.g. jev-1.14.0")
        if urlsplit(base_url).scheme != "https":
            raise ValueError(f"{base_url!r}: the judge is reached over https only")
        self.model = model
        self.url = base_url.rstrip("/") + "/v1/systemone"
        self.key_env = key_env
        self.timeout_s = timeout_s
        self.deadline_s = deadline_s
        self.max_retries = max_retries
        self.post = post
        self.env = os.environ if env is None else env
        self.clock = clock
        self.sleep = sleep

    def evaluate(
        self, state: str | Mapping[str, Any], questions: Mapping[str, Question]
    ) -> Verdicts:
        key = self.env.get(self.key_env, "")
        if not key:
            raise JudgeUnauthorizedError(f"{self.key_env} is not set; the judge is off")
        body = {
            "model": self.model,
            "state": json.loads(redact(json.dumps(state), self.env)),
            "questions": {name: _question(q) for name, q in questions.items()},
        }
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        status, payload = self._send(json.dumps(body).encode("utf-8"), headers)
        return _verdicts(status, payload, questions)

    def _send(self, body: bytes, headers: Mapping[str, str]) -> tuple[int, Any]:
        started = self.clock()
        wait = BACKOFF_START_S
        why = ""
        for attempt in range(self.max_retries + 1):
            try:
                status, replied, raw = self.post(self.url, body, headers, self.timeout_s)
            except (TimeoutError, OSError) as exc:
                why = f"{type(exc).__name__}: {exc}"
            else:
                if status not in RETRYABLE:
                    return status, _json(raw)
                why = f"HTTP {status}"
                wait = max(wait, _retry_after(replied))
            last = attempt == self.max_retries
            if last or self.clock() - started + wait > self.deadline_s:
                break
            self.sleep(wait)
            wait *= 2
        raise JudgeUnavailableError(f"the judge did not answer within {self.deadline_s}s: {why}")


def _question(question: Question) -> dict[str, Any]:
    if isinstance(question, Pick):
        criteria: Any = dict(question.options)
        kind = "choice"
    elif isinstance(question, Grade):
        criteria, kind = list(question.levels), "score"
    else:
        criteria = {k: v for k, v in (("true", question.yes), ("false", question.no)) if v}
        kind = "noul"
    out: dict[str, Any] = {"type": kind, "instructions": question.instructions}
    if criteria:
        out["criteria"] = criteria
    return out


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        return {"detail": raw[:200].decode("utf-8", "replace")}


def _retry_after(headers: Mapping[str, str]) -> float:
    for name, value in headers.items():
        if name.lower() == "retry-after":
            try:
                return max(0.0, float(value))
            except ValueError:
                return 0.0
    return 0.0


def _verdicts(status: int, payload: Any, questions: Mapping[str, Question]) -> Verdicts:
    if status in (401, 403):
        raise JudgeUnauthorizedError(f"HTTP {status}: {_detail(payload)}")
    if status != 200:
        raise JudgeRefusedError(f"HTTP {status}: {_detail(payload)}")
    typed: dict[str, Answer] = {}
    for name, question in questions.items():
        try:
            typed[name] = _answer(question, payload["answers"][name])
        except (KeyError, TypeError, ValueError) as exc:
            raise JudgeRefusedError(
                f"{name}: an answer that does not fit the question: {exc!r}"
            ) from exc
    try:
        usage = payload.get("usage") or {}
        return Verdicts(
            typed,
            model=str(payload["model"]),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise JudgeRefusedError(f"a reply without its model or usage: {exc!r}") from exc


def _answer(question: Question, raw: Mapping[str, Any]) -> Answer:
    if isinstance(question, Pick):
        label = str(raw["choice"])
        if label not in question.options:
            raise ValueError(f"choice {label!r} was not offered")
        probabilities = {str(k): float(v) for k, v in raw["probabilities"].items()}
        return Picked(label, float(raw["confidence"]), probabilities)
    if isinstance(question, Grade):
        levels = {int(k): float(v) for k, v in raw["probabilities"].items()}
        return Graded(float(raw["score"]), float(raw["confidence"]), levels)
    if not isinstance(question, Check):  # pragma: no cover - the union has three members
        raise TypeError(question)
    return Checked(float(raw["noul"]))


def _detail(payload: Any) -> str:
    if isinstance(payload, Mapping) and "detail" in payload:
        detail = payload["detail"]
        if isinstance(detail, list):
            return "; ".join(
                str(d.get("msg", d)) if isinstance(d, Mapping) else str(d) for d in detail
            )
        return str(detail)
    return str(payload)[:200]


__all__ = ["BASE_URL", "KEY_ENV", "TypeSafeJudge"]
