"""More than one model for a role, and what happens when the first one cannot be reached.

A provider is a dependency: it rate-limits, it has an outage, it retires a model. The data layer
answers that with a chain of sources and these tests hold the model layer to the same standard -
with one difference that matters more than the rest: a budget the operator declared stops the chain
where it stands, because no fallback is allowed past the operator's own limit.
"""

from __future__ import annotations

from typing import Any

import pytest

from trading_research_agents.llm import BudgetLimits, MeteredCompleter, UsageLedger
from trading_research_agents.llm.chain import (
    ChainError,
    CompleterChain,
    Link,
    parse_link,
)
from trading_research_agents.llm.completer import Completion, ProviderError
from trading_research_agents.llm.usage import BudgetSpentError


class Model:
    """A link that answers, or fails the way a provider fails."""

    def __init__(self, name: str, *, fails: Exception | None = None) -> None:
        self.name = name
        self.fails = fails
        self.calls = 0

    def complete(self, *, system: str, user: str, schema: Any) -> Completion:
        self.calls += 1
        if self.fails is not None:
            raise self.fails
        return Completion(text="{}", model=self.name, input_tokens=10, output_tokens=5)


def chain_of(*models: Model, **kwargs: Any) -> CompleterChain:
    return CompleterChain(
        role="technical_analyst",
        links=[Link(m.name, (lambda m=m: m)) for m in models],  # type: ignore[misc]
        **kwargs,
    )


def ask(chain: CompleterChain) -> Completion:
    return chain.complete(system="doctrine", user="{}", schema={"type": "object"})


def test_the_first_model_that_answers_wins_and_the_rest_are_not_asked() -> None:
    first, second = Model("groq:a"), Model("google:b")
    chain = chain_of(first, second)

    answer = ask(chain)
    assert answer.model == "groq:a"
    assert first.calls == 1 and second.calls == 0
    assert [a.outcome for a in chain.attempts] == ["answered"]


def test_a_provider_that_cannot_be_reached_hands_over_to_the_next() -> None:
    seen: list[tuple[str, Any]] = []
    rate_limited = Model("groq:a", fails=ProviderError("RateLimitError: 429"))
    spare = Model("google:b")
    chain = chain_of(rate_limited, spare, on_attempt=lambda role, a: seen.append((role, a)))

    answer = ask(chain)
    assert answer.model == "google:b"
    assert [a.outcome for a in chain.attempts] == ["failed", "answered"]
    assert "429" in chain.attempts[0].detail
    # The attempt is reported as it happens, so a cycle can write it down beside the answer.
    assert [role for role, _ in seen] == ["technical_analyst", "technical_analyst"]
    assert chain.record()["links"] == ["groq:a", "google:b"]


def test_every_link_failing_is_one_error_that_names_them_all() -> None:
    chain = chain_of(
        Model("groq:a", fails=ProviderError("RateLimitError: 429")),
        Model("google:b", fails=ProviderError("APIConnectionError: down")),
    )
    with pytest.raises(ChainError, match="no model answered"):
        ask(chain)
    assert [a.outcome for a in chain.attempts] == ["failed", "failed"]


def test_a_link_that_cannot_be_built_is_recorded_and_stepped_over() -> None:
    def missing() -> Any:
        raise ProviderError("the anthropic SDK is not installed")

    chain = CompleterChain(
        role="validator",
        links=[Link("anthropic:opus", missing), Link("groq:a", lambda: Model("groq:a"))],
    )
    assert ask(chain).model == "groq:a"
    assert chain.attempts[0].outcome == "unbuilt"


def test_a_link_is_built_once_and_only_when_it_is_needed() -> None:
    built: list[str] = []

    def build(name: str) -> Any:
        def make() -> Model:
            built.append(name)
            return Model(name)

        return make

    chain = CompleterChain(
        role="technical_analyst",
        links=[Link("groq:a", build("groq:a")), Link("google:b", build("google:b"))],
    )
    ask(chain)
    ask(chain)
    # The spare was never needed, so its client was never constructed.
    assert built == ["groq:a"]


def test_the_budget_stops_the_chain_rather_than_falling_through(tmp_path: Any) -> None:
    ledger = UsageLedger(tmp_path / "usage.jsonl")
    spent = MeteredCompleter(
        Model("groq:a"),
        role="technical_analyst",
        provider="groq",
        limits=BudgetLimits(token_budget=1, max_calls_per_hour=100, min_interval_s=0),
        ledger=ledger,
        clock=lambda: 0.0,
    )
    spare = Model("google:b")
    chain = CompleterChain(
        role="technical_analyst",
        links=[Link("groq:a", lambda: spent), Link("google:b", lambda: spare)],
    )

    ask(chain)  # the first call goes through and spends the budget
    with pytest.raises(BudgetSpentError):
        ask(chain)
    # The operator's own limit is not something another provider is allowed past.
    assert spare.calls == 0


def test_a_chain_refuses_to_be_declared_wrong() -> None:
    with pytest.raises(ChainError, match="at least one model"):
        CompleterChain(role="technical_analyst", links=[])
    with pytest.raises(ChainError, match="named twice"):
        chain_of(Model("groq:a"), Model("groq:a"))


def test_a_fallback_is_written_as_provider_and_model() -> None:
    assert parse_link("groq:openai/gpt-oss-20b") == ("groq", "openai/gpt-oss-20b")
    for bad in ("groq", ":model", "groq:", ""):
        with pytest.raises(ChainError, match="provider:model"):
            parse_link(bad)


def test_what_a_provider_cached_is_recorded_rather_than_assumed(tmp_path: Any) -> None:
    import json

    class Cached:
        def complete(self, *, system: str, user: str, schema: Any) -> Completion:
            return Completion(
                text="{}",
                model="claude",
                input_tokens=1200,
                output_tokens=40,
                cached_tokens=1024,
                cache_write_tokens=0,
            )

    ledger = UsageLedger(tmp_path / "usage.jsonl")
    metered = MeteredCompleter(
        Cached(),
        role="technical_analyst",
        provider="anthropic",
        limits=BudgetLimits(token_budget=100_000, max_calls_per_hour=10, min_interval_s=0),
        ledger=ledger,
        clock=lambda: 0.0,
    )
    metered.complete(system="doctrine", user="{}", schema={"type": "object"})

    row = json.loads((tmp_path / "usage.jsonl").read_text("utf-8").splitlines()[0])
    assert row["cached_tokens"] == 1024 and row["cache_write_tokens"] == 0
    # A cache that never engages shows up as a zero here, which is the point of recording it.
