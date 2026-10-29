"""The desk's models: one metered client per role, and a journal of fallback links that failed."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_research_agents.cli.common import (
    _completer_for_role,
)
from trading_research_agents.records import append_line


def _metered_completers(a: Any, usage: Any, out: Path | None = None) -> Callable[[str], Any]:
    """One budget and one rate window for the whole desk, one client per role: a role may run on a
    cheaper model without the others paying for it."""
    from trading_research_agents.llm.usage import (
        BudgetLimits,
        MeteredCompleter,
        Pricing,
        RateWindow,
    )

    limits = BudgetLimits(
        token_budget=a.token_budget,
        max_calls_per_hour=a.max_calls_per_hour,
        min_interval_s=a.min_interval_s,
        call_timeout_s=a.call_timeout_s,
    )
    pricing = {model: Pricing(i, o) for model, (i, o) in a.pricing.items()}
    metered: dict[str, MeteredCompleter] = {}
    window = RateWindow()

    def completer_for(role: str) -> MeteredCompleter:
        if role not in metered:
            settings = a.for_role(role)
            metered[role] = MeteredCompleter(
                _completer_for_role(a, role, on_attempt=_ladder_journal(out)),
                role=role,
                provider=settings.provider,
                limits=limits,
                ledger=usage,
                pricing=pricing,
                window=window,
            )
        return metered[role]

    return completer_for


LADDER = "ladder.jsonl"


def _ladder_journal(out: Path | None) -> Callable[[str, Any], None] | None:
    """Every link of a fallback ladder that did not answer, one structured line each: the link
    that answered is in usage.jsonl, and the ones before it would otherwise leave no trace."""
    if out is None:
        return None

    def note(role: str, attempt: Any) -> None:
        if attempt.outcome == "answered":
            return
        line = {"at": datetime.now(UTC).isoformat(), "role": role, **attempt.record()}
        append_line(out / LADDER, line)

    return note
