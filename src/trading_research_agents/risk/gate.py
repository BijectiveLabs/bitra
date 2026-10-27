"""The one entry point: an intent, the limits and the desk's state in; a decision out."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trading_research_agents.risk.approval import KEY, sign
from trading_research_agents.risk.checks import CHECKS
from trading_research_agents.risk.limits import RiskLimits, RiskState


@dataclass(frozen=True)
class RiskDecision:
    """Approved with a signed approval, or refused with every reason that applies."""

    approved: bool
    checks: tuple[str, ...]
    reasons: tuple[str, ...] = ()
    approval: Mapping[str, Any] | None = None

    def approved_intent(self, intent: Mapping[str, Any]) -> dict[str, Any]:
        if self.approval is None:
            raise ValueError("a refused intent carries no approval")
        return {**intent, KEY: dict(self.approval)}

    def record(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "checks": list(self.checks),
            "reasons": list(self.reasons),
        }


def evaluate(
    intent: Mapping[str, Any], *, limits: RiskLimits, state: RiskState, now: float
) -> RiskDecision:
    """Every check, every time, so a refusal names all that is wrong at once.

    Fails closed: a check that cannot be evaluated refuses the intent with what went wrong.
    """
    names = tuple(name for name, _ in CHECKS)
    try:
        reasons = tuple(why for _, check in CHECKS if (why := check(intent, limits, state)))
    except Exception as exc:
        why = f"the risk gate could not evaluate the intent: {exc.__class__.__name__}: {exc}"
        return RiskDecision(approved=False, checks=names, reasons=(why,))
    if reasons:
        return RiskDecision(approved=False, checks=names, reasons=reasons)
    approval = sign(intent, checks=names, now=now, valid_for_s=limits.valid_for_s)
    return RiskDecision(approved=True, checks=names, approval=approval)
