from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.agents import (
    BoundBasis,
    Outcome,
    RoleError,
    RunConfig,
    propose,
)
from trading_research_agents.agents.pipeline import read_refusals
from trading_research_agents.agents.roles import parse_bound, parse_critique, parse_registration
from trading_research_agents.chain.preregistration import from_record
from trading_research_agents.envelope import Envelope
from trading_research_agents.ledger import Axis, Ledger, Verdict
from trading_research_agents.llm import FakeCompleter

IDEA = "Longs paying high funding into settlement get squeezed out before the payment."

BOUND_OK = {
    "magnitude_bps": "12.0",
    "basis": "dispersion",
    "explanation": "funding dispersion ~15 bps",
}
BOUND_LOW = {
    "magnitude_bps": "3.0",
    "basis": "comparable",
    "explanation": "informed-flow drift on record",
}
BOUND_NONE = {
    "magnitude_bps": "",
    "basis": "none",
    "explanation": "no comparable, no dispersion figure",
}
MEMO_OK = {
    "mechanism": "Longs paying funding are forced by margin to reduce into settlement.",
    "forced_loser": "longs paying funding",
    "forcing_structure": "margin at settlement",
}
MEMO_PATTERN = {
    "mechanism": "Price tends to mean revert after funding spikes for reasons.",
    "forced_loser": "nobody in particular",
    "forcing_structure": "none",
}
ACCEPT = {"accepted": True, "objection": ""}
REJECT = {"accepted": False, "objection": "names no party that is compelled; this is a pattern"}


def registration_draft(**overrides: Any) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "name": "funding settlement squeeze",
        "claim": "Net return over 4h after settlement is positive when funding exceeds 15 bps.",
        "metric": "net_return_bps",
        "horizon_seconds": 14400,
        "universe": {"instruments": ["BTCUSDT", "ETHUSDT"], "rule": "top 15 by 30d volume"},
        "era": {
            "research_start": "2022-01-01",
            "research_end": "2025-06-30",
            "sealed": [{"name": "held-out B", "start": "2025-07-01", "end": None}],
        },
        "cost_model": [
            {"name": "fee", "source": "venue schedule, declared tier"},
            {"name": "spread_cost", "source": "half-spread from recorded book"},
            {"name": "slippage", "source": "walk the recorded book"},
            {"name": "funding_accrual", "source": "settlement history endpoint"},
        ],
        "power": {"required_sample": 1200, "effect_bps": "8.0", "basis": "two-sided, 80 pct"},
        "kill_criteria": [
            {
                "metric": "net_return_ci95_lower_bps",
                "comparison": "<=",
                "threshold": "0",
                "verdict": "NOT PROVEN",
            },
            {
                "metric": "net_return_bps",
                "comparison": "<",
                "threshold": "0",
                "verdict": "FALSIFIED",
            },
            {
                "metric": "permutation_p_value",
                "comparison": ">=",
                "threshold": "0.05",
                "verdict": "FALSIFIED",
            },
        ],
    }
    draft.update(overrides)
    return draft


def config(envelope: Envelope, tmp_path: Path, responses: list[Any], **kw: Any) -> RunConfig:
    clock = iter(
        datetime(2026, 9, 13, 12, 0, tzinfo=UTC) + timedelta(seconds=i) for i in range(1000)
    )
    return RunConfig(
        envelope=envelope,
        completer=FakeCompleter(responses),
        ledger=Ledger(tmp_path / "ledger.jsonl"),
        refusals=tmp_path / "refusals.jsonl",
        runs_dir=tmp_path / "runs",
        clock=lambda: next(clock),
        run_id="R-1",
        **kw,
    )


def test_an_idea_below_the_floor_is_closed_on_cost(envelope: Envelope, tmp_path: Path) -> None:
    taker_only = Envelope(tuple(c for c in envelope.channels if c.name == "taker-taker"))
    cfg = config(taker_only, tmp_path, [BOUND_LOW], venue="hyperliquid")
    result = propose(IDEA, cfg)
    assert result.outcome is Outcome.STOPPED_AT_FLOOR
    assert result.floor is not None and result.floor.margin_bps == Decimal("-6.0")
    assert len(result.exchanges) == 1 and result.exchanges[0].role == "bound"
    rows = list(Ledger(tmp_path / "ledger.jsonl").entries())
    assert rows[0].entry_id == "R-1" and rows[0].verdict is Verdict.CLOSED
    assert rows[0].axis_of_death == (Axis.COST,) and rows[0].layer == 0
    assert "bound basis comparable" in rows[0].note
    assert not (tmp_path / "refusals.jsonl").exists()
    summary = json.loads((tmp_path / "runs" / "R-1" / "run.json").read_text(encoding="utf-8"))
    assert summary["outcome"] == "stopped_at_floor" and summary["ledger_entry_id"] == "R-1"


def test_no_bound_after_revisions_is_a_refusal_not_a_verdict(
    envelope: Envelope, tmp_path: Path
) -> None:
    cfg = config(envelope, tmp_path, [BOUND_NONE, BOUND_NONE, BOUND_NONE])
    result = propose(IDEA, cfg)
    assert result.outcome is Outcome.REFUSED_LAYER_0
    assert result.refusal is not None and result.refusal.layer == 0
    assert "no bound could be given" in result.refusal.reason
    assert list(Ledger(tmp_path / "ledger.jsonl").entries()) == []
    refusals = read_refusals(tmp_path / "refusals.jsonl")
    assert len(refusals) == 1 and refusals[0].run_id == "R-1"
    assert "Revise. Previous attempt" in cfg.completer.calls[1].user  # type: ignore[attr-defined]


def test_a_pattern_is_refused_at_layer_two_after_two_revisions(
    envelope: Envelope, tmp_path: Path
) -> None:
    cfg = config(
        envelope,
        tmp_path,
        [BOUND_OK, MEMO_PATTERN, REJECT, MEMO_PATTERN, REJECT, MEMO_PATTERN, REJECT],
    )
    result = propose(IDEA, cfg)
    assert result.outcome is Outcome.REFUSED_LAYER_2
    assert (
        result.refusal is not None and result.refusal.layer == 2 and result.refusal.revisions == 2
    )
    assert result.refusal.reason == REJECT["objection"]
    roles = [e.role for e in result.exchanges]
    assert roles == ["bound", "memo", "critique", "memo", "critique", "memo", "critique"]
    assert "Revise. Objection: names no party" in cfg.completer.calls[3].user  # type: ignore[attr-defined]
    assert list(Ledger(tmp_path / "ledger.jsonl").entries()) == []


def test_a_malformed_memo_is_returned_with_the_gates_own_words(
    envelope: Envelope, tmp_path: Path
) -> None:
    two_sentences = {**MEMO_OK, "mechanism": "Longs pay funding. They must reduce into settlement."}
    cfg = config(
        envelope, tmp_path, [BOUND_OK, two_sentences, MEMO_OK, ACCEPT, registration_draft()]
    )
    result = propose(IDEA, cfg)
    assert result.outcome is Outcome.REGISTERED
    assert "one sentence" in cfg.completer.calls[2].user  # type: ignore[attr-defined]


def test_a_registration_is_revised_then_sealed_and_opened_in_the_ledger(
    envelope: Envelope, tmp_path: Path
) -> None:
    missing_term = registration_draft(cost_model=registration_draft()["cost_model"][:3])
    cfg = config(
        envelope, tmp_path, [BOUND_OK, MEMO_OK, ACCEPT, missing_term, registration_draft()]
    )
    result = propose(IDEA, cfg)
    assert result.outcome is Outcome.REGISTERED
    assert "missing funding_accrual" in cfg.completer.calls[4].user  # type: ignore[attr-defined]
    assert result.preregistration is not None
    assert result.preregistration.mechanism == MEMO_OK["mechanism"]
    assert result.preregistration.horizon == timedelta(hours=4)

    rows = list(Ledger(tmp_path / "ledger.jsonl").entries())
    assert rows[0].verdict is Verdict.OPEN and rows[0].blocking_axis is Axis.POWER
    assert rows[0].layer == 3 and rows[0].evidence == (
        f"preregistration:{result.preregistration.seal()}",
    )
    assert "0 of 1200 samples" in rows[0].note and "bound 12.0 bps (dispersion)" in rows[0].note

    written = json.loads(
        (tmp_path / "runs" / "R-1" / "preregistration.json").read_text(encoding="utf-8")
    )
    assert from_record(written) == result.preregistration
    files = sorted(p.name for p in (tmp_path / "runs" / "R-1").iterdir())
    assert files == [
        "01-bound.json",
        "02-memo.json",
        "03-critique.json",
        "04-registration.json",
        "05-registration.json",
        "preregistration.json",
        "run.json",
    ]
    exchange = json.loads((tmp_path / "runs" / "R-1" / "01-bound.json").read_text(encoding="utf-8"))
    assert exchange["schema"]["required"] == ["magnitude_bps", "basis", "explanation"]


def test_registration_refused_after_revisions(envelope: Envelope, tmp_path: Path) -> None:
    bad = registration_draft(kill_criteria=[])
    cfg = config(envelope, tmp_path, [BOUND_OK, MEMO_OK, ACCEPT, bad, bad, bad])
    result = propose(IDEA, cfg)
    assert result.outcome is Outcome.REFUSED_LAYER_3
    assert result.refusal is not None and "kill criterion" in result.refusal.reason
    assert list(Ledger(tmp_path / "ledger.jsonl").entries()) == []


def test_operator_magnitude_skips_the_bound_role(envelope: Envelope, tmp_path: Path) -> None:
    cfg = config(
        envelope, tmp_path, [MEMO_OK, ACCEPT, registration_draft()], magnitude_bps=Decimal("20")
    )
    result = propose(IDEA, cfg)
    assert result.outcome is Outcome.REGISTERED
    assert [e.role for e in result.exchanges] == ["memo", "critique", "registration"]
    rows = list(Ledger(tmp_path / "ledger.jsonl").entries())
    assert "bound 20 bps (declared)" in rows[0].note


def test_an_unreadable_critic_does_not_accept_by_default(
    envelope: Envelope, tmp_path: Path
) -> None:
    cfg = config(
        envelope, tmp_path, [BOUND_OK, MEMO_OK, "not json", MEMO_OK, ACCEPT, registration_draft()]
    )
    result = propose(IDEA, cfg)
    assert result.outcome is Outcome.REGISTERED
    assert "critique unreadable" in cfg.completer.calls[3].user  # type: ignore[attr-defined]


def test_role_parsers_reject_what_the_gates_cannot_use() -> None:
    with pytest.raises(RoleError, match="not valid JSON"):
        parse_bound("{")
    with pytest.raises(RoleError, match="lacks basis"):
        parse_bound(json.dumps({"magnitude_bps": "1", "explanation": "x"}))
    with pytest.raises(RoleError, match="reserved for the operator"):
        parse_bound(json.dumps({"magnitude_bps": "1", "basis": "declared", "explanation": "x"}))
    with pytest.raises(RoleError, match="positive"):
        parse_bound(json.dumps({"magnitude_bps": "-1", "basis": "comparable", "explanation": "x"}))
    assert parse_bound(json.dumps(BOUND_NONE)).basis is BoundBasis.NONE
    with pytest.raises(RoleError, match="states its objection"):
        parse_critique(json.dumps({"accepted": False, "objection": ""}))
    with pytest.raises(RoleError, match="true or false"):
        parse_critique(json.dumps({"accepted": "yes", "objection": ""}))
    now = datetime(2026, 9, 13, tzinfo=UTC)
    with pytest.raises(RoleError, match="FALSIFIED or NOT PROVEN, not CLOSED"):
        parse_registration(
            json.dumps(
                registration_draft(
                    kill_criteria=[
                        {"metric": "m", "comparison": "<", "threshold": "0", "verdict": "CLOSED"}
                    ]
                )
            ),
            mechanism=MEMO_OK["mechanism"],
            registered_at=now,
        )
    with pytest.raises(RoleError, match="must be a date"):
        parse_registration(
            json.dumps(
                registration_draft(
                    era={"research_start": "soon", "research_end": "2025-06-30", "sealed": []}
                )
            ),
            mechanism=MEMO_OK["mechanism"],
            registered_at=now,
        )
    with pytest.raises(RoleError, match="must be an integer"):
        parse_registration(
            json.dumps(registration_draft(horizon_seconds="4h")),
            mechanism=MEMO_OK["mechanism"],
            registered_at=now,
        )


def test_config_rejects_nonsense(envelope: Envelope) -> None:
    with pytest.raises(ValueError, match="max_revisions"):
        RunConfig(envelope=envelope, completer=FakeCompleter([]), max_revisions=-1)
    with pytest.raises(ValueError, match="positive"):
        RunConfig(envelope=envelope, completer=FakeCompleter([]), magnitude_bps=Decimal("0"))
    with pytest.raises(ValueError, match="idea is required"):
        propose("  ", RunConfig(envelope=envelope, completer=FakeCompleter([])))
    with pytest.raises(RuntimeError, match="no response for call 1"):
        propose(IDEA, RunConfig(envelope=envelope, completer=FakeCompleter([])))
