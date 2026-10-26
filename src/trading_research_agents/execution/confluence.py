"""The confluence gate: whether the context agrees with the direction, decided in code.

The operator's own thresholds decide. This gate reads the declared state of each reading - the one
code computes from the thresholds in the operator's series files - and never a sentence a model
wrote. That is what keeps a sentence from being an input to a trade.

It fails closed. A reading the operator marked as required whose declaration is `undeclared` or
`mixed` has produced no state, and a state nobody produced is not a neutral state: the cycle refuses
and says which reading was silent. An operator who means to run on one leg says so in the file,
and the record shows they did.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# What a declaration can be. The first two are states; the last two are the absence of one.
STATES = ("bullish", "bearish", "mixed", "undeclared")
POLICIES = ("refuse", "ignore")
VOTES = ("support", "oppose", "abstain")
# The three stances a validator can be asked to take. Declared here, beside the gate that counts
# their votes, so the role package and the config reader share one definition.
STANCES = ("pro", "contra", "neutral")


class ConfluenceError(ValueError):
    """The matrix as written cannot be applied."""


@dataclass(frozen=True)
class Matrix:
    """[confluence]: which readings must agree, what agreement means, and how votes count."""

    required: tuple[str, ...] = ()
    long_states: tuple[str, ...] = ("bullish",)
    short_states: tuple[str, ...] = ("bearish",)
    on_missing: str = "refuse"
    min_support: int = 0
    # None means the operator did not ask about opposition at all. Zero means no opposition is
    # allowed, which is the strictest setting there is: the two must not read the same.
    max_opposition: int | None = None

    def __post_init__(self) -> None:
        if self.on_missing not in POLICIES:
            raise ConfluenceError(f"[confluence].on_missing is one of {list(POLICIES)}")
        for name, allowed in (
            ("long_states", self.long_states),
            ("short_states", self.short_states),
        ):
            if not allowed:
                raise ConfluenceError(f"[confluence].{name} names at least one state")
            unknown = sorted(set(allowed) - set(STATES))
            if unknown:
                raise ConfluenceError(
                    f"[confluence].{name}: {unknown} is not a state {list(STATES)}"
                )
        if self.min_support < 0 or (self.max_opposition is not None and self.max_opposition < 0):
            raise ConfluenceError("[confluence] counts are not negative")

    @property
    def idle(self) -> bool:
        """Whether this matrix asks for anything at all."""
        return not self.required and self.min_support == 0 and self.max_opposition is None

    def allowed_for(self, target: str) -> tuple[str, ...]:
        if target == "long":
            return self.long_states
        if target == "short":
            return self.short_states
        raise ConfluenceError(f"target is long or short, not {target!r}")

    def record(self) -> dict[str, Any]:
        return {
            "required": list(self.required),
            "long_states": list(self.long_states),
            "short_states": list(self.short_states),
            "on_missing": self.on_missing,
            "min_support": self.min_support,
            "max_opposition": self.max_opposition,
        }


@dataclass(frozen=True)
class Confluence:
    """What the gate found: the states it read, the votes it counted, and why it refused."""

    admitted: bool
    target: str
    states: dict[str, str] = field(default_factory=dict)
    support: int = 0
    opposition: int = 0
    abstained: int = 0
    reasons: tuple[str, ...] = ()

    def record(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "target": self.target,
            "states": dict(sorted(self.states.items())),
            "support": self.support,
            "opposition": self.opposition,
            "abstained": self.abstained,
            "reasons": list(self.reasons),
        }


def declared_of(state: Mapping[str, Any] | None) -> str:
    """The one word in a reading's declared-state record, or `undeclared` when there is none."""
    if not state:
        return "undeclared"
    word = str(state.get("declared", "undeclared"))
    return word if word in STATES else "undeclared"


def confluent(
    matrix: Matrix,
    *,
    target: str,
    states: Mapping[str, Mapping[str, Any]],
    votes: Sequence[str] = (),
) -> Confluence:
    """Apply the matrix to the declared states and the validators' votes.

    `states` is what code derived from the operator's thresholds, one entry per reading read.
    `votes` are the validators' verdicts after their claims were checked against the brief.
    """
    allowed = matrix.allowed_for(target)
    read: dict[str, str] = {}
    reasons: list[str] = []
    for name in matrix.required:
        if name not in states:
            read[name] = "not read"
            if matrix.on_missing == "refuse":
                reasons.append(
                    f"{name} is required and was not read; a missing state is not neutral"
                )
            continue
        word = declared_of(states[name])
        read[name] = word
        if word in allowed:
            continue
        if word in ("undeclared", "mixed") and matrix.on_missing == "ignore":
            # The operator said in the file that a silent leg is acceptable. It is still recorded.
            reasons.append(f"{name} is {word}, allowed by on_missing = ignore")
            continue
        reasons.append(f"{name} declares {word}; a {target} needs {list(allowed)}")

    support = sum(1 for vote in votes if vote == "support")
    opposition = sum(1 for vote in votes if vote == "oppose")
    abstained = sum(1 for vote in votes if vote == "abstain")
    if support < matrix.min_support:
        reasons.append(f"{support} validator(s) support it; {matrix.min_support} required")
    if matrix.max_opposition is not None and opposition > matrix.max_opposition:
        reasons.append(
            f"{opposition} validator(s) oppose it; at most {matrix.max_opposition} allowed"
        )

    refusing = [r for r in reasons if "allowed by on_missing" not in r]
    return Confluence(
        admitted=not refusing,
        target=target,
        states=read,
        support=support,
        opposition=opposition,
        abstained=abstained,
        reasons=tuple(reasons),
    )
