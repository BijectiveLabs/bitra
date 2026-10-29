"""The neutral review by the typed judge: two checked cases in, a probability per verdict out.

Weighing a case for against a case against, each claim already checked against the figures by
code, is a closed choice between three verdicts, which is the job a typed judge is built for and a
reasoning model is not: the judge returns a probability for each verdict rather than a sentence,
answers in a tenth of a second, and its versioned model is recorded on every verdict. A verdict
counts only when the judge gives it at least `DECIDES`; below that the neutral seat abstains.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from trading_research_agents.agents.cycle.config import CycleConfig
from trading_research_agents.agents.cycle.result import CycleResult
from trading_research_agents.agents.progress import DONE, FAILED, announce
from trading_research_agents.llm.judge import JudgeError, Pick, Picked

NAME = "reviewer_neutral"
DECIDES = 0.5
WEIGH = Pick(
    "Two reviewers argued about one trade proposal, each claim already checked against the "
    "figures it cites. Weighing only these two cases, which verdict on the proposal do they "
    "support?",
    {
        "support": "the case for the proposal outweighs the case against it",
        "oppose": "the case against the proposal outweighs the case for it",
        "abstain": "neither case outweighs the other",
    },
)


def _claims(claims: Sequence[Mapping[str, Any]]) -> list[str]:
    """Each checked claim as the judge reads it: what it says, and the figure it rests on."""
    return [f"{c.get('says')} ({c.get('figure')} = {c.get('value')})" for c in claims]


def judge_the_cases(
    cfg: CycleConfig,
    result: CycleResult,
    *,
    proposal: Mapping[str, Any],
    case_for: Sequence[Mapping[str, Any]],
    case_against: Sequence[Mapping[str, Any]],
) -> None:
    """Ask the declared judge for the neutral verdict; a judge that does not answer is an error,
    handled as an unanswered stance is."""
    judge = cfg.judge
    if judge is None:
        raise ValueError("the neutral review by the judge needs [judge] declared")
    state = {
        "proposal": {k: proposal.get(k) for k in ("target", "setup", "reason")},
        "case_for": _claims(case_for),
        "case_against": _claims(case_against),
    }
    try:
        said = judge.evaluate(state, {"verdict": WEIGH})
    except (JudgeError, LookupError) as exc:
        error = f"{exc.__class__.__name__}: {exc}"
        result.context[NAME] = {"stance": "neutral", "error": error, "judged": "judge"}
        result.refusals.append(f"{NAME}: the judge did not answer, {error}")
        announce(cfg.watch, NAME, FAILED, exc.__class__.__name__)
        return
    answer = said["verdict"]
    if not isinstance(answer, Picked):
        raise TypeError("the judge answered a pick with something else")
    verdict = answer.label if answer.probability(answer.label) >= DECIDES else "abstain"
    odds = {label: round(answer.probability(label), 4) for label in WEIGH.options}
    result.context[NAME] = {
        "stance": "neutral",
        "verdict": verdict,
        "claims": [],
        "discarded": [],
        "reason": f"the judge weighed the two checked cases: {odds}",
        "probabilities": odds,
        "model": said.model,
        "judged": "judge",
    }
    announce(cfg.watch, NAME, DONE, f"{verdict} ({said.model})")
