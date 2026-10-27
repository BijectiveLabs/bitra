"""Asking a role: the prompt, the brief as compact JSON, one more try on a misfit."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from trading_research_agents.agents import (
    technical_analyst,
    technical_peer,
)
from trading_research_agents.agents.cycle.config import CycleConfig
from trading_research_agents.agents.cycle.result import CycleResult
from trading_research_agents.agents.roles import RoleError, prompt
from trading_research_agents.agents.scope import brief_for, dropped
from trading_research_agents.records import (
    COMPACT,
)

# A desk picks its setup from the market in front of it; the research core trades one registered
# mechanism. Same role and schema, so only the instructions differ.
LIVE_PROMPTS = {
    technical_analyst.ROLE: "technical_analyst_live",
    # The peer is a second read of the same brief, under the same instructions.
    technical_peer.ROLE: "technical_analyst_live",
}


# Neutral fills the validator's schema, but as the judge of the two cases it has its own job.
JUDGE_PROMPT = "validator_judge"


def _ask(
    cfg: CycleConfig,
    role: str,
    question: Mapping[str, Any],
    schema: Mapping[str, Any],
    refused: str = "",
    system: str = "",
) -> str:
    completer = cfg.completer_for(role)
    user = json.dumps(question, sort_keys=True, default=str, separators=COMPACT)
    if refused:
        # After the brief, so the prefix a provider caches is the same on both calls.
        user += (
            f"\n\nYour last answer to this brief was refused before anyone read it: {refused}. "
            "Answer again with one JSON object that fills the schema."
        )
    done = completer.complete(
        system=prompt(system or LIVE_PROMPTS.get(role, role)), user=user, schema=dict(schema)
    )
    return done.text


def _ask_readable(
    cfg: CycleConfig,
    role: str,
    question: Mapping[str, Any],
    schema: Mapping[str, Any],
    parse: Callable[[str], Any],
    system: str = "",
) -> str:
    """Ask, and ask once more, with the reason, when the answer does not fit its schema.

    A malformed answer is a failed call, not a verdict, and with on_missing_validator at refuse one
    of them stops a trade (seen 2026-10-06: a validator's JSON came back with its fields out of
    place). One more call is cheaper than a lost cycle, and a model told what was wrong with its
    answer does not have to guess; a second malformed answer is left to fail.
    """
    text = _ask(cfg, role, question, schema, system=system)
    try:
        parse(text)
    except (RoleError, ValueError) as exc:
        return _ask(cfg, role, question, schema, refused=str(exc)[:300], system=system)
    return text


def _within_scope(role: str, brief: Mapping[str, Any], result: CycleResult) -> dict[str, Any]:
    """The brief a role is handed: its own sections, with anything else named and removed."""
    extra = dropped(role, brief)
    if extra:
        result.refusals.append(f"{role}: not this role's to read, removed: {list(extra)}")
    return brief_for(role, brief)
