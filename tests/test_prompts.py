"""The prompts are doctrine. They name and they formulate; they never promise an outcome."""

from __future__ import annotations

from importlib import resources

import pytest

ROLES = ("bound", "memo", "critique", "registration")
# The roles a cycle or a session asks. Every one of them ships a prompt, and the prompt is held to
# the same rule as the doctrine: it names what to read and never promises an outcome.
AGENTS = (
    "market_screener",
    "technical_analyst",
    "macro_analyst",
    "onchain_analyst",
    "sentiment_analyst",
    "risk_manager",
    "executor",
    "auditor",
)
FORBIDDEN = ("win rate", "profit", "guaranteed", "alpha")


@pytest.mark.parametrize("role", ROLES)
def test_prompt_is_ascii_and_names_its_question(role: str) -> None:
    raw = (
        resources.files("trading_research_agents.agents.prompts")
        .joinpath(f"{role}.md")
        .read_bytes()
    )
    assert raw.isascii(), f"{role}.md contains non-ASCII bytes"
    text = raw.decode("ascii")
    assert len(text.split()) > 60, f"{role}.md is too thin to carry the doctrine"


@pytest.mark.parametrize("role", ROLES)
def test_prompt_only_mentions_outcome_words_to_forbid_them(role: str) -> None:
    text = (
        resources.files("trading_research_agents.agents.prompts").joinpath(f"{role}.md").read_text()
    )
    for line in text.splitlines():
        lowered = line.lower()
        for word in FORBIDDEN:
            if word in lowered:
                assert "do not use" in lowered, (
                    f"{role}.md: {word!r} outside the prohibition: {line!r}"
                )


def test_memo_prompt_carries_the_question_verbatim() -> None:
    text = resources.files("trading_research_agents.agents.prompts").joinpath("memo.md").read_text()
    assert "Who is forced to lose, and why does the structure force them?" in text


def test_registration_prompt_lists_all_four_cost_terms() -> None:
    text = (
        resources.files("trading_research_agents.agents.prompts")
        .joinpath("registration.md")
        .read_text()
    )
    for term in ("fee", "spread_cost", "slippage", "funding_accrual"):
        assert term in text


@pytest.mark.parametrize("role", AGENTS)
def test_every_agent_ships_an_ascii_prompt_that_forbids_outcome_talk(role: str) -> None:
    raw = (
        resources.files("trading_research_agents.agents.prompts")
        .joinpath(f"{role}.md")
        .read_bytes()
    )
    assert raw.isascii(), f"{role}.md contains non-ASCII bytes"
    text = raw.decode("ascii")
    assert len(text.split()) > 60, f"{role}.md is too thin to carry the role"
    assert "one JSON object" in text, f"{role}.md does not say what shape to answer in"
    for line in text.splitlines():
        lowered = line.lower()
        for word in FORBIDDEN:
            if word in lowered:
                assert "do not" in lowered or "never" in lowered, (
                    f"{role}.md: {word!r} outside a prohibition: {line!r}"
                )
