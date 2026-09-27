"""A bounded tool loop: the model picks a tool from a catalog or answers, code runs the tool.

This is the reason-act loop every agent framework ships, written as forty lines of code the
package owns. What keeps it safe is not the loop but its edges: the catalog is closed (a tool
not in it is a refusal by name), the arguments are checked against the function before it
runs, every observation is cut to a size, the number of steps is capped, the budget applies to
every call, and each step is written down. The model never sees a tool it could not have
called, and never gets to keep going once the cap is reached.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from trading_research_agents.agents.roles import RoleError
from trading_research_agents.llm.completer import Completer
from trading_research_agents.llm.form import parse_object
from trading_research_agents.llm.usage import BudgetSpentError

OBSERVATION_LIMIT = 6000
STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["call", "answer"]},
        "tool": {"type": "string"},
        "arguments": {"type": "object"},
        "answer": {"type": "object"},
        "thought": {"type": "string"},
    },
    "required": ["action", "tool", "arguments", "answer", "thought"],
    "additionalProperties": False,
}


class ToolLoopError(RuntimeError):
    """The loop ended without an answer: the cap was reached, or the budget was spent."""


@dataclass(frozen=True)
class Step:
    index: int
    thought: str
    tool: str | None
    arguments: dict[str, Any]
    observation: str | None
    error: str | None

    def record(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "thought": self.thought,
            "tool": self.tool,
            "arguments": self.arguments,
            "observation": self.observation,
            "error": self.error,
        }


@dataclass
class LoopResult:
    answer: dict[str, Any] | None
    steps: list[Step] = field(default_factory=list)
    calls: int = 0


def catalog_text(tools: Mapping[str, Callable[..., Any]]) -> str:
    """The tools as the model sees them: name, parameters, first line of the docstring."""
    lines = []
    for name, fn in tools.items():
        sig = inspect.signature(fn)
        params = ", ".join(
            f"{p.name}: {getattr(p.annotation, '__name__', str(p.annotation))}"
            + ("" if p.default is inspect.Parameter.empty else f" = {p.default!r}")
            for p in sig.parameters.values()
        )
        doc = (inspect.getdoc(fn) or "").splitlines()[0] if inspect.getdoc(fn) else ""
        lines.append(f"- {name}({params}): {doc}")
    return "\n".join(lines)


def _check_arguments(fn: Callable[..., Any], arguments: Mapping[str, Any]) -> None:
    sig = inspect.signature(fn)
    unknown = sorted(set(arguments) - set(sig.parameters))
    if unknown:
        raise RoleError(f"unknown arguments {unknown}; the tool takes {list(sig.parameters)}")
    missing = [
        p.name
        for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty and p.name not in arguments
    ]
    if missing:
        raise RoleError(f"missing arguments {missing}")


def run_tool_loop(
    *,
    completer: Completer,
    system: str,
    question: Mapping[str, Any],
    tools: Mapping[str, Callable[..., Any]],
    answer_schema: Mapping[str, Any],
    max_steps: int,
    observation_limit: int = OBSERVATION_LIMIT,
) -> LoopResult:
    """Ask, let the model call tools up to `max_steps` times, and return its final answer.

    The final answer is validated against `answer_schema` by the caller's parser; here it is
    only required to be an object. A malformed step is reported back to the model once as an
    error observation, which counts as a step.
    """
    if max_steps < 1:
        raise ValueError("max_steps is at least 1")
    result = LoopResult(answer=None)
    transcript: list[dict[str, Any]] = []
    preamble = (
        "You may call a tool or answer. A call is {action: 'call', tool, arguments}; an "
        "answer is {action: 'answer', answer: <object matching the answer schema>}. Fill every "
        "field of the step schema; leave tool empty and arguments {} when answering, and "
        "answer {} when calling. Tools available:\n"
        + catalog_text(tools)
        + "\nAnswer schema:\n"
        + json.dumps(dict(answer_schema), sort_keys=True)
    )
    for index in range(1, max_steps + 1):
        user = json.dumps(
            {"question": question, "steps_so_far": transcript, "steps_left": max_steps - index + 1},
            sort_keys=True,
            default=str,
        )
        try:
            done = completer.complete(
                system=f"{system}\n\n{preamble}", user=user, schema=STEP_SCHEMA
            )
        except BudgetSpentError as exc:
            raise ToolLoopError(f"budget spent at step {index}: {exc}") from exc
        result.calls += 1
        try:
            step = parse_object(done.text, STEP_SCHEMA)
        except RoleError as exc:
            observation = f"malformed step: {exc}"
            result.steps.append(Step(index, "", None, {}, None, observation))
            transcript.append({"index": index, "error": observation})
            continue
        thought = str(step.get("thought", ""))
        if step.get("action") == "answer":
            answer = step.get("answer")
            if not isinstance(answer, dict):
                observation = "answer must be an object"
                result.steps.append(Step(index, thought, None, {}, None, observation))
                transcript.append({"index": index, "error": observation})
                continue
            result.answer = answer
            result.steps.append(Step(index, thought, None, {}, None, None))
            return result
        name = str(step.get("tool", ""))
        raw_arguments = step.get("arguments")
        arguments: dict[str, Any] = dict(raw_arguments) if isinstance(raw_arguments, dict) else {}
        fn = tools.get(name)
        if fn is None:
            observation = f"no tool named {name!r}; the catalog is {sorted(tools)}"
            result.steps.append(Step(index, thought, name, arguments, None, observation))
            transcript.append({"index": index, "tool": name, "error": observation})
            continue
        try:
            _check_arguments(fn, arguments)
            out = fn(**arguments)
            text = json.dumps(out, sort_keys=True, default=str)
            if len(text) > observation_limit:
                text = text[:observation_limit] + f"... [cut at {observation_limit} characters]"
            result.steps.append(Step(index, thought, name, arguments, text, None))
            transcript.append(
                {"index": index, "tool": name, "arguments": arguments, "observation": text}
            )
        except (RoleError, ValueError, KeyError, OSError, RuntimeError, TypeError) as exc:
            observation = f"{exc.__class__.__name__}: {exc}"
            result.steps.append(Step(index, thought, name, arguments, None, observation))
            transcript.append(
                {"index": index, "tool": name, "arguments": arguments, "error": observation}
            )
    raise ToolLoopError(f"no answer after {max_steps} steps")
