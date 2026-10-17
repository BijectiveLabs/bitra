"""What each role is given, what it may answer, and what is somebody else's job.

A desk works because a job has edges. The analyst that labels a regime does not size the order, the
role that prices the round trip does not decide whether the idea is any good, and none of them
places anything. Here those edges are written down once, in code, so they are the same in a cycle,
in a session and in a test.

Four things keep a role inside its job, and only the last is words:

  the brief    a role receives the sections its job needs and nothing else, filtered here
  the schema   a closed schema with only that role's own fields (see each role package)
  the wiring   what decides reads figures and the operator's declarations, never a sentence
  the prompt   one line naming what is not this role's job

The fourth needs the least trust and gets the least authority. `crossings` looks for a small
vocabulary no reading analyst has reason to write - an order, a size, an exit - and records a hit
beside the answer. It never silences the answer, because the answer already decides nothing; the
record is there so an operator can see a model drifting out of its lane.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from trading_research_agents.agents import (
    macro_analyst,
    onchain_analyst,
    sentiment_analyst,
    technical_analyst,
)

VALIDATOR = "validator"
SCREENER = "market_screener"
TECH_AUDITOR = "tech_auditor"
QUANT_AUDITOR = "quant_auditor"

# Phrases that belong to a later step. Deliberately small and unambiguous: "buy" and "sell" are not
# here, because a headline about a central bank buying bonds is a proper thing for an analyst to
# report. What is here is the language of an order, a size and an exit, which is language a reading
# analyst has no reason to use at all.
NOT_YOURS: dict[str, tuple[str, ...]] = {
    "an order": (
        "go long",
        "go short",
        "open a position",
        "close the position",
        "place an order",
        "enter the trade",
        "exit the trade",
    ),
    "a size": ("position size", "size up", "size down", "leverage", "risk per trade"),
    "an exit": ("stop loss", "stop-loss", "take profit", "take-profit", "price target"),
    "another role": ("risk manager", "the executor", "the validator", "the technical analyst"),
}


class ScopeError(ValueError):
    """A role was asked for something outside its job, or given something that is not its own."""


@dataclass(frozen=True)
class Scope:
    """One role's edges: what it reads, what it answers, and what its answer is used for."""

    role: str
    reads: tuple[str, ...]
    answers: tuple[str, ...]
    decides: str
    prose: tuple[str, ...] = field(default=())
    # Some briefs name a figure per horizon (ret_1_bps, ret_5_bps), so a family of figures is
    # declared by its prefix rather than by listing every horizon an operator might configure.
    families: tuple[str, ...] = field(default=())

    def keeps(self, key: str) -> bool:
        return key in self.reads or any(key.startswith(f) for f in self.families)


# Every reading analyst sees its own series and the sentence that says how to read them. None of
# them sees equity, size, the controls, the cost of the round trip or the exit distances: those
# belong to steps that come later, and a figure a role never receives is a figure it cannot misuse.
_CONTEXT = ("instrument", "as_of", "basis", "series", "declared_series")

SCOPES: dict[str, Scope] = {
    macro_analyst.ROLE: Scope(
        role=macro_analyst.ROLE,
        reads=(*_CONTEXT, "events", "horizon"),
        answers=("regime", "events", "reason", "figures_used", "data_as_of"),
        decides="nothing on its own; recorded beside the operator's declared state",
        prose=("reason",),
    ),
    onchain_analyst.ROLE: Scope(
        role=onchain_analyst.ROLE,
        reads=(*_CONTEXT, "flow_window"),
        answers=("signal", "reason", "figures_used", "data_as_of"),
        decides="nothing on its own; recorded beside the operator's declared state",
        prose=("reason",),
    ),
    sentiment_analyst.ROLE: Scope(
        role=sentiment_analyst.ROLE,
        reads=(*_CONTEXT, "notes", "declared"),
        answers=("state", "figures_used", "reason", "data_as_of"),
        decides="nothing on its own; recorded beside the operator's declared state",
        prose=("reason",),
    ),
    technical_analyst.ROLE: Scope(
        role=technical_analyst.ROLE,
        reads=(
            "instrument",
            "interval_s",
            "bars",
            "mechanism",
            "age_s",
            "spread_bps",
            "funding_rate",
            "close",
            "zscore",
            "vwap_gap_bps",
            "volume_ratio",
            "range_ratio",
            "position_in_range",
            "window_move_bps",
            "trend_strength",
            "variance_ratio",
            "range_high",
            "range_low",
            "atr",
            "atr_bps",
            "regime",
            "timeframes",
            "book_imbalance",
            "open_interest",
            "readings",
        ),
        answers=(
            "target",
            "setup",
            "confidence",
            "horizon_bars",
            "stop_price",
            "target_price",
            "reason",
            "figures_used",
        ),
        decides=(
            "the direction, the horizon and where the idea is wrong or done; the size, the cost "
            "and the bounds on those levels are not its own"
        ),
        prose=("reason",),
        families=("ret_",),
    ),
    SCREENER: Scope(
        role=SCREENER,
        reads=("as_of", "basis", "mechanism", "candidates"),
        answers=("kept", "excluded", "picks"),
        decides=(
            "which of the instruments code found and admitted are worth a cycle, and in what "
            "order; it cannot add one, or say anything about a direction or a size"
        ),
    ),
    TECH_AUDITOR: Scope(
        role=TECH_AUDITOR,
        reads=("cycles", "unreadable_records", "agents", "data", "spend", "refusals", "installed"),
        answers=("summary", "nothing_to_report", "findings"),
        decides="nothing; it writes proposals an operator applies or does not",
        prose=("summary",),
    ),
    QUANT_AUDITOR: Scope(
        role=QUANT_AUDITOR,
        reads=("cycles", "decisions", "refusals", "round_trips"),
        answers=("summary", "nothing_to_report", "findings"),
        decides="nothing; it writes proposals an operator applies or does not",
        prose=("summary",),
    ),
    VALIDATOR: Scope(
        role=VALIDATOR,
        reads=("instrument", "stance", "proposal", "figures", "declared_states", "cost_bps"),
        answers=("stance", "claims", "verdict", "reason"),
        decides="one vote, and only through claims that cite a figure it was given",
        prose=("reason",),
    ),
}


def scope_of(role: str) -> Scope:
    try:
        return SCOPES[role]
    except KeyError as exc:
        raise ScopeError(f"{role} has no declared scope; add one deliberately") from exc


def brief_for(role: str, brief: Mapping[str, Any]) -> dict[str, Any]:
    """The brief with anything outside this role's job removed.

    The filter is the point, not a formality: a caller that hands a role more than its job needs is
    corrected here rather than in review, and the role never learns that the extra figure existed.
    """
    allowed = scope_of(role)
    return {key: value for key, value in brief.items() if allowed.keeps(key)}


def dropped(role: str, brief: Mapping[str, Any]) -> tuple[str, ...]:
    """What the filter removed, for the record. An empty tuple is the normal case."""
    allowed = scope_of(role)
    return tuple(sorted(key for key in brief if not allowed.keeps(key)))


def crossings(role: str, said: Mapping[str, Any]) -> tuple[str, ...]:
    """Where an answer's prose reached into a later step. Recorded, never used to silence it."""
    allowed = scope_of(role)
    if role == technical_analyst.ROLE:
        # The direction and the horizon are this role's job, and the vocabulary of an entry is the
        # vocabulary it is paid to use. Sizing and exits are still not its own, but it names them
        # by describing the mechanism, so a prose check here would be noise.
        return ()
    found: list[str] = []
    # A validator's whole job is to review a proposal another role made, so naming that role is not
    # a crossing for it. An order, a size and an exit still are.
    forbidden = {
        k: v for k, v in NOT_YOURS.items() if not (role == VALIDATOR and k == "another role")
    }
    for key in allowed.prose:
        text = str(said.get(key, "")).lower()
        if not text:
            continue
        for what, phrases in forbidden.items():
            for phrase in phrases:
                if phrase in text:
                    found.append(f"{key} names {what}: {phrase!r}")
                    break
    return tuple(found)
