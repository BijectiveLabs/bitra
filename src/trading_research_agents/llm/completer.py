"""The one thing a language model is asked to do here: fill a JSON schema from a prompt."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderError(RuntimeError):
    """The vendor could not be reached, or answered with something unusable.

    Every adapter translates its SDK's own exceptions into this one, so a failed call is a
    recorded event rather than a traceback that ends a session.
    """


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    # What the provider said it read from, and wrote to, its prompt cache. Recorded rather than
    # assumed: a cache that never engages is a cache that is not paying for itself, and the only
    # way to know is the figure the provider returns.
    cached_tokens: int = 0
    cache_write_tokens: int = 0


class Completer(Protocol):
    def complete(self, *, system: str, user: str, schema: Mapping[str, Any]) -> Completion: ...


@dataclass(frozen=True)
class Call:
    system: str
    user: str
    schema: Mapping[str, Any]


@dataclass
class FakeCompleter:
    """Scripted responses, in order. Runs out loudly rather than inventing a reply."""

    responses: Sequence[str | Mapping[str, Any]]
    model: str = "fake"
    calls: list[Call] = field(default_factory=list)

    def complete(self, *, system: str, user: str, schema: Mapping[str, Any]) -> Completion:
        self.calls.append(Call(system=system, user=user, schema=schema))
        index = len(self.calls) - 1
        if index >= len(self.responses):
            raise RuntimeError(f"FakeCompleter has no response for call {index + 1}")
        scripted = self.responses[index]
        text = scripted if isinstance(scripted, str) else json.dumps(scripted)
        return Completion(
            text=text, model=self.model, input_tokens=len(user), output_tokens=len(text)
        )
