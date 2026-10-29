"""What a research session decides by written rule, where a model used to be asked.

Each rule here was a role's prompt: the screener kept an instrument when its cost floor cleared and
its figures were not degenerate; the risk manager demoted the ladder when measured latency was a
multiple of the declared one, when the rolling net fell below the registered claim, or when the
same control kept refusing, and halted when a control tripped; the executor split the allocation.
Written as code they give the same answer every time, cost no token and can be read before they
act. Every ruling names the figure that decided it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# A measured latency this many times the declared one means the replay's fills were not the
# market's; a control refusing this many times in one run is the run's finding, not noise.
LATENCY_MULTIPLE = 3.0
REPEATED_REFUSALS = 3
DEGENERATE_VOL_BPS = 0.0
RULINGS = ("continue", "demote", "halt")


def screen_by_rule(
    figures: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep an instrument whose cost floor cleared, whose figures are not degenerate and on which
    a declared event recurs; label it with the regime code proposed. Exclude the rest by name."""
    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for f in figures:
        why = _excluded_because(f)
        if why:
            excluded.append({"instrument": f.instrument, "reason": why})
            continue
        regime = "" if f.screen is None else f.screen.proposed_regime
        reason = "" if f.screen is None else f.screen.regime_reason
        kept.append({"instrument": f.instrument, "regime": regime, "reason": reason})
    return kept, excluded


def _excluded_because(f: Any) -> str:
    if f.rows <= 0 or f.prints_per_minute <= 0:
        return "no prints"
    if f.realised_vol_bps <= DEGENERATE_VOL_BPS:
        return "no volatility: the figures are degenerate"
    if f.floor_outcome != "cleared":
        return f"cost floor: {f.floor_outcome} (margin {f.floor_margin_bps} bps)"
    observed = f.observation or {}
    if observed and observed.get("recurrence") != "recurs":
        return f"the declared event {observed.get('recurrence')}"
    return ""


@dataclass(frozen=True)
class RiskRuling:
    """The session's risk ruling: continue, demote the ladder, or halt, with the figures."""

    decision: str
    reasons: tuple[str, ...] = ()
    triggers: Mapping[str, Any] = field(default_factory=dict)

    def record(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reasons": list(self.reasons),
            "triggers": dict(self.triggers),
            "decided_by": "rule",
        }


def risk_by_rule(measured: Mapping[str, Any], stopped_by: Mapping[str, str | None]) -> RiskRuling:
    """Halt when a run was stopped by a control; demote when the measured latency, the rolling
    net or a repeating refusal says the evidence does not hold; continue otherwise."""
    tripped = {name: why for name, why in stopped_by.items() if why}
    if tripped:
        return RiskRuling(
            "halt",
            tuple(f"{name}: stopped by {why}" for name, why in sorted(tripped.items())),
            {"stopped_by": tripped},
        )
    reasons: list[str] = []
    for name, brief in sorted(measured.items()):
        reasons.extend(_demotions(name, brief))
    decision = "demote" if reasons else "continue"
    return RiskRuling(decision, tuple(reasons), {"measured": dict(measured)})


def _demotions(name: str, brief: Mapping[str, Any]) -> list[str]:
    said = []
    ratio = (brief.get("latency") or {}).get("ratio_to_declared")
    if ratio is not None and ratio >= LATENCY_MULTIPLE:
        said.append(f"{name}: latency {ratio}x the declared one")
    decay = brief.get("alpha_decay") or {}
    if decay.get("below_claim"):
        said.append(
            f"{name}: rolling net {decay.get('rolling_mean_bps')} bps is below the claimed "
            f"{decay.get('claimed_bps')} bps"
        )
    for row in brief.get("refusals") or []:
        if int(row.get("refusals", 0)) >= REPEATED_REFUSALS:
            said.append(f"{name}: {row.get('control')} refused {row.get('refusals')} times")
    return said


def allocate_evenly(kept: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """An equal share of the gross exposure for each kept instrument, summing to one."""
    if not kept:
        return []
    share = round(1.0 / len(kept), 6)
    return [
        {"instrument": str(k["instrument"]), "fraction": share, "reason": "an equal share"}
        for k in kept
    ]
