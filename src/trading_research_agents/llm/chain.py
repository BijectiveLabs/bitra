"""More than one model for a role, tried in the order the operator declared.

A provider is a dependency like any other: it rate-limits, it has an outage, it retires a model on
its own schedule. The data layer already answers that with a chain of sources; this is the same
answer for models. The first link that returns a usable answer wins, every attempt is recorded with
the reason it failed, and nothing is retried in a loop - the vendor SDK already retried inside the
link, and a second pass at the same provider is the same wait twice.

Two failures are not the same and are not treated the same. A provider that could not be reached is
a reason to try the next link. A budget that is spent is the operator's own limit, and no link in
the world is allowed past it, so it stops the chain where it stands.

What makes this safe for a role that has to be stateless: the brief is built by code, so a second
model asked after the first one failed is handed exactly what the first one was handed. There is no
conversation to lose, no context to rebuild, and no "the new model has to understand the last
answer" - the cycle's record already holds the last answer, and the next call starts from figures.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from trading_research_agents.llm.completer import Completer, Completion, ProviderError


class ChainError(ProviderError):
    """Every link was tried and none of them answered."""


@dataclass(frozen=True)
class Attempt:
    """One link, and what came of it."""

    link: str
    outcome: str
    detail: str = ""

    def record(self) -> dict[str, Any]:
        return {"link": self.link, "outcome": self.outcome, "detail": self.detail}


@dataclass(frozen=True)
class Link:
    """One way of asking: a name for the record, and the completer to ask with.

    The completer is built when it is first needed rather than when the chain is declared, so an
    operator who names a fallback they have no key for does not pay for that mistake until the day
    the first link actually fails.
    """

    name: str
    build: Callable[[], Completer]


@dataclass
class CompleterChain:
    """The links for one role, in order, with every attempt on the record."""

    role: str
    links: Sequence[Link]
    on_attempt: Callable[[str, Attempt], None] | None = None
    # A link that says "try again in N seconds" with N inside this is waited for once, instead of
    # being abandoned for the next link straight away. Zero never waits.
    max_wait_s: float = 0.0
    sleep: Callable[[float], None] = time.sleep
    attempts: list[Attempt] = field(default_factory=list)
    _built: dict[str, Completer] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.links:
            raise ChainError(f"{self.role}: a chain needs at least one model")
        names = [link.name for link in self.links]
        if len(set(names)) != len(names):
            raise ChainError(f"{self.role}: the same model is named twice: {names}")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(link.name for link in self.links)

    def complete(self, *, system: str, user: str, schema: Mapping[str, Any]) -> Completion:
        failures: list[str] = []
        for link in self.links:
            try:
                completer = self._completer(link)
            except Exception as exc:
                self._note(Attempt(link.name, "unbuilt", f"{exc.__class__.__name__}: {exc}"))
                failures.append(f"{link.name}: {exc}")
                continue
            try:
                answer = completer.complete(system=system, user=user, schema=schema)
            except ProviderError as exc:
                wait = exc.retry_after
                if wait is not None and 0 < wait <= self.max_wait_s:
                    self._note(Attempt(link.name, "waited", f"{wait:.1f}s, as the vendor asked"))
                    self.sleep(wait + 0.5)
                    try:
                        answer = completer.complete(system=system, user=user, schema=schema)
                    except ProviderError as again:
                        exc = again
                    else:
                        self._note(Attempt(link.name, "answered", answer.model))
                        return answer
                self._note(Attempt(link.name, "failed", f"{exc.__class__.__name__}: {exc}"))
                failures.append(f"{link.name}: {exc}")
                continue
            self._note(Attempt(link.name, "answered", answer.model))
            return answer
        raise ChainError(f"{self.role}: no model answered; {'; '.join(failures)}")

    def _completer(self, link: Link) -> Completer:
        if link.name not in self._built:
            self._built[link.name] = link.build()
        return self._built[link.name]

    def _note(self, attempt: Attempt) -> None:
        self.attempts.append(attempt)
        if self.on_attempt is not None:
            self.on_attempt(self.role, attempt)

    def record(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "links": list(self.names),
            "attempts": [a.record() for a in self.attempts],
        }


def parse_link(text: str) -> tuple[str, str]:
    """`provider:model` as the operator writes it in a fallback list."""
    provider, _, model = str(text).partition(":")
    provider, model = provider.strip(), model.strip()
    if not provider or not model:
        raise ChainError(f"{text!r} is not a fallback; write it as provider:model")
    return provider, model
