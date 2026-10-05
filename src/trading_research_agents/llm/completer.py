"""The one thing a language model is asked to do here: fill a JSON schema from a prompt."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderError(RuntimeError):
    """The vendor could not be reached, or answered with something unusable.

    Every adapter translates its SDK's own exceptions into this one, so a failed call is a
    recorded event rather than a traceback that ends a session.
    """

    # Seconds the vendor asked us to wait before trying again, when it said so.
    retry_after: float | None = None

    @classmethod
    def from_vendor(cls, exc: BaseException) -> ProviderError:
        """The vendor's own words, with any credential this process holds taken out.

        The message goes into `cycle.json`, into a brief an auditor reads and so to another vendor;
        some SDKs echo the key they were given when they refuse it.
        """
        made = cls(redact(f"{exc.__class__.__name__}: {exc}"))
        made.retry_after = retry_after_of(exc)
        return made


RETRY_IN = re.compile(r"(?:try again|retry) in ([0-9]+(?:\.[0-9]+)?)\s*(ms|s)\b", re.IGNORECASE)


def retry_after_of(exc: BaseException) -> float | None:
    """How long the vendor asked for: its retry-after header, or the figure in its message."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            raw = headers.get("retry-after")
            if raw is not None:
                return max(0.0, float(raw))
        except (TypeError, ValueError, AttributeError):
            pass
    found = RETRY_IN.search(str(exc))
    if found is None:
        return None
    value = float(found.group(1))
    return value / 1000 if found.group(2).lower() == "ms" else value


CREDENTIAL_NAMES = ("_API_KEY", "_KEY", "_SECRET", "_TOKEN", "_PASSPHRASE", "_PASSWORD")
REDACTED = "[redacted]"


def redact(text: str, env: Mapping[str, str] | None = None) -> str:
    """Replace the value of every credential-looking environment variable found in `text`."""
    held = os.environ if env is None else env
    out = text
    for name, value in held.items():
        if name.upper().endswith(CREDENTIAL_NAMES) and len(value) >= 8 and value in out:
            out = out.replace(value, REDACTED)
    return out


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
