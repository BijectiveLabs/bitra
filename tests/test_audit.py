"""Two auditors over the record, and the rule that a finding must point at something real.

An audit that cannot be checked is an opinion with a timestamp. So the figures come from files the
project wrote itself, every finding has to cite one of them with its value, a proposal has to name a
table the configuration actually has, and what fails either check is discarded with its reason kept.
Nothing an auditor says is ever applied by this code.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.agents.audit import AuditConfig, as_markdown, run_audit, tables_in
from trading_research_agents.agents.auditor_report import Report, checked
from trading_research_agents.agents.auditor_report import parse as parse_report
from trading_research_agents.agents.audits import (
    AuditError,
    agent_health,
    data_health,
    decisions,
    read_cycles,
    read_usage,
    refusal_kinds,
    token_spend,
)
from trading_research_agents.agents.cited import figures_of, resolve
from trading_research_agents.llm import BudgetLimits, FakeCompleter, MeteredCompleter, UsageLedger

CYCLE = {
    "cycle_id": "2026-09-28T01-00-00Z",
    "instrument": "BTCUSDT",
    "agents": {
        "agents": [
            {"agent": "technical", "ok": True, "error": "", "seconds": 6.0, "reused": False},
            {
                "agent": "event",
                "ok": False,
                "error": "ProviderError: timed out",
                "seconds": 12.0,
                "reused": False,
            },
        ]
    },
    "validators": {
        "agents": [
            {"agent": "validator_pro", "ok": True, "error": "", "seconds": 3.0, "reused": False}
        ]
    },
    "data": {
        "values": {
            "ohlcv": {"source": "ccxt:bybit", "tier": "free", "age_s": 60.0},
            "orderbook": {"source": "ccxt:bybit", "tier": "free", "age_s": 8.0},
        },
        "refused": {"open_interest": "stale"},
        "stale": ["funding"],
    },
    "refusals": ["data open_interest: stale", "confluence: macro declares undeclared"],
    "cost": {"cost": {"round_trip_bps": "12.90"}},
    "conviction": {"admitted": True, "score": {"score": "0.670"}},
    "analyst": {"target": "long"},
    "intent": {"size": "0.005", "net_bps": "12.10", "edge_bps": "25"},
    "tokens": 8000,
}

FINDING: dict[str, Any] = {
    "what": "the macro analyst failed a call",
    "cites": [{"figure": "agents.failed", "value": "2"}],
    "proposal": "give it more room or a steadier model",
    "change": {"key": "[agents].max_output_tokens", "from": "1024", "to": "2048"},
    "confidence": "medium",
}
ANSWER: dict[str, Any] = {
    "summary": "one thing worth changing",
    "nothing_to_report": False,
    "findings": [FINDING],
}


def project(tmp_path: Path, *, cycles: int = 2) -> Path:
    runs = tmp_path / "runs" / "cycles"
    for number in range(cycles):
        where = runs / f"2026-09-28T0{number}-00-00Z"
        where.mkdir(parents=True, exist_ok=True)
        record = {**CYCLE, "cycle_id": where.name}
        (where / "cycle.json").write_text(json.dumps(record), encoding="utf-8")
    (runs / "usage.jsonl").write_text(
        json.dumps({"role": "technical_analyst", "output_tokens": 500, "latency_ms": 3000.0})
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "strategy.toml").write_text(
        '[agents]\nprovider = "groq"\n\n[conviction]\nmin_score = 0.4\n', encoding="utf-8"
    )
    return runs


def test_the_record_is_read_from_disk_and_what_cannot_be_read_is_named(tmp_path: Path) -> None:
    runs = project(tmp_path)
    (runs / "broken").mkdir()
    (runs / "broken" / "cycle.json").write_text("{not json", encoding="utf-8")

    cycles = read_cycles(runs)
    assert cycles.count == 2 and len(cycles.unreadable) == 1
    assert "broken" in cycles.unreadable[0]

    with pytest.raises(AuditError, match="is not a directory"):
        read_cycles(tmp_path / "nowhere")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(AuditError, match="no cycle record"):
        read_cycles(empty)


def test_the_counts_are_arithmetic_an_operator_can_repeat(tmp_path: Path) -> None:
    cycles = read_cycles(project(tmp_path))

    assert refusal_kinds(cycles) == {"confluence": 2, "data open_interest": 2}

    health = agent_health(cycles)
    assert health["calls"] == 6 and health["failed"] == 2
    assert health["failure_rate"] == round(2 / 6, 4)
    assert health["median_seconds"]["event"] == 12.0
    assert health["failures"] == {"event: ProviderError": 2}

    data = data_health(cycles)
    assert data["refused"] == {"open_interest": 2} and data["used_stale"] == {"funding": 2}
    assert data["median_age_s"]["orderbook"] == 8.0
    assert data["sources"]["ohlcv <- ccxt:bybit"] == 2

    spend = token_spend(cycles, read_usage(tmp_path / "runs" / "cycles" / "usage.jsonl"))
    assert spend["tokens_total"] == 16000 and spend["intents"] == 2
    assert spend["tokens_per_intent"] == 8000.0
    assert spend["output_tokens_by_role"] == {"technical_analyst": 500}

    made = decisions(cycles)
    assert made["targets"] == {"long": 2} and made["intents"] == 2 and made["placed"] == 0
    assert made["median_claimed_edge_bps"] == "25.0000"
    assert made["median_net_after_cost_bps"] == "12.1000"
    assert made["median_conviction"] == "0.6700"


def test_a_leaf_name_is_resolved_only_when_it_can_mean_one_figure() -> None:
    figures = {"decisions.median_net_after_cost_bps": "12.1", "agents.failed": "1"}
    assert resolve("median_net_after_cost_bps", figures)[0] == "decisions.median_net_after_cost_bps"
    assert resolve("agents.failed", figures)[0] == "agents.failed"

    _, why = resolve("missing", figures)
    assert "is not a figure" in why

    both = {"a.count": "1", "b.count": "2"}
    _, ambiguous = resolve("count", both)
    assert "could be a.count or b.count" in ambiguous


def test_absence_is_a_fact_a_reader_may_point_at() -> None:
    figures = figures_of({"round_trips": {"closed": 0, "median_realised_net_bps": None}})
    assert figures["round_trips.median_realised_net_bps"] == "null"


def test_a_finding_that_cannot_be_verified_is_discarded_with_its_reason() -> None:
    report = parse_report(json.dumps(ANSWER), role="tech_auditor")
    figures = {"agents.failed": "2"}

    kept = checked(report, figures=figures, tables=["agents", "conviction"])
    assert len(kept.findings) == 1 and kept.discarded == () and not kept.nothing_to_report

    wrong = parse_report(
        json.dumps(
            {
                **ANSWER,
                "findings": [{**FINDING, "cites": [{"figure": "agents.failed", "value": "9"}]}],
            }
        ),
        role="tech_auditor",
    )
    thrown = checked(wrong, figures=figures, tables=["agents"])
    assert thrown.findings == () and "was 2, not 9" in thrown.discarded[0]
    # Nothing survived, so the report says there is nothing to report rather than implying one.
    assert thrown.nothing_to_report


def test_a_proposal_names_a_table_the_project_has(tmp_path: Path) -> None:
    project(tmp_path)
    assert tables_in(tmp_path / "strategy.toml") == ("agents", "conviction")
    assert tables_in(tmp_path / "nothing.toml") == ()

    report = parse_report(json.dumps(ANSWER), role="tech_auditor")
    unknown = checked(report, figures={"agents.failed": "2"}, tables=["conviction"])
    assert unknown.findings == () and "[agents] is not a table" in unknown.discarded[0]

    shapeless = parse_report(
        json.dumps(
            {
                **ANSWER,
                "findings": [
                    {
                        **ANSWER["findings"][0],
                        "change": {"key": "make it bigger", "from": "1", "to": "2"},
                    }
                ],
            }
        ),
        role="tech_auditor",
    )
    refused = checked(shapeless, figures={"agents.failed": "2"}, tables=["agents"])
    assert "is not a configuration key" in refused.discarded[0]


def test_a_proposal_cannot_invent_a_setting() -> None:
    def proposing(key: str) -> Report:
        change = {"key": key, "from": "1", "to": "2"}
        finding = {**ANSWER["findings"][0], "change": change}
        return parse_report(json.dumps({**ANSWER, "findings": [finding]}), role="quant_auditor")

    figures = {"agents.failed": "2"}
    made_up = checked(proposing("[targets].min_edge_multiple_strict"), figures=figures)
    assert made_up.findings == () and "there is no such rule" in made_up.discarded[0]
    per_role = checked(proposing("[agents.roles.validator].veto_power"), figures=figures)
    assert per_role.findings == ()
    # A role that asks no model has no table to change.
    retired = checked(proposing("[agents.roles.macro_analyst].model"), figures=figures)
    assert retired.findings == () and "is not a role that asks a model" in retired.discarded[0]

    assert checked(proposing("[agents].max_output_tokens"), figures=figures).findings
    assert checked(proposing("[agents.roles.validator].model"), figures=figures).findings
    # A table without a fixed list of settings is judged by the table check alone.
    assert checked(proposing("[sources.rates].file"), figures=figures).findings


def test_a_finding_with_no_citation_is_worth_nothing() -> None:
    bare = parse_report(
        json.dumps({**ANSWER, "findings": [{**FINDING, "cites": []}]}),
        role="quant_auditor",
    )
    assert checked(bare, figures={}).discarded == (
        "'the macro analyst failed a call' cites no figure",
    )


def audit_config(tmp_path: Path, answers: dict[str, Any], **overrides: Any) -> AuditConfig:
    runs = project(tmp_path)
    usage = UsageLedger(tmp_path / "audits" / "usage.jsonl")
    limits = BudgetLimits(token_budget=100_000, max_calls_per_hour=100, min_interval_s=0)
    fakes = {role: FakeCompleter([text]) for role, text in answers.items()}

    def completer_for(role: str) -> MeteredCompleter:
        return MeteredCompleter(
            fakes[role],
            role=role,
            provider="fake",
            limits=limits,
            ledger=usage,
            clock=lambda: 0.0,
        )

    settings: dict[str, Any] = {
        "runs": runs,
        "completer_for": completer_for,
        "usage": usage,
        "root": tmp_path / "audits",
        "config": tmp_path / "strategy.toml",
        "clock": lambda: datetime(2026, 9, 28, 9, 0, tzinfo=UTC),
    }
    settings.update(overrides)
    return AuditConfig(**settings)


def test_an_audit_writes_the_brief_the_answer_and_a_page_a_person_can_read(tmp_path: Path) -> None:
    quiet = json.dumps({"summary": "nothing stands out", "nothing_to_report": True, "findings": []})
    cfg = audit_config(tmp_path, {"tech_auditor": json.dumps(ANSWER), "quant_auditor": quiet})
    result = run_audit(cfg)

    assert result.cycles == 2 and result.findings == 1
    where = result.path
    assert json.loads((where / "brief-tech_auditor.json").read_text("utf-8"))["cycles"] == 2
    assert (where / "answer-quant_auditor.json").is_file()
    saved = json.loads((where / "audit.json").read_text("utf-8"))
    assert saved["reports"]["tech_auditor"]["findings"][0]["change"]["to"] == "2048"

    page = (where / "proposals.md").read_text("utf-8")
    assert "nothing in this file has been applied" in page.lower()
    assert "[agents].max_output_tokens" in page and "Nothing to report" in page
    # The report is a proposal, not an edit: the project's own configuration is untouched.
    assert (tmp_path / "strategy.toml").read_text(
        "utf-8"
    ) == '[agents]\nprovider = "groq"\n\n[conviction]\nmin_score = 0.4\n'


def test_an_auditor_that_cannot_answer_is_recorded_and_the_other_still_reports(
    tmp_path: Path,
) -> None:
    cfg = audit_config(
        tmp_path,
        {"tech_auditor": json.dumps(ANSWER), "quant_auditor": "not json at all"},
    )
    result = run_audit(cfg)

    assert "quant_auditor" in result.failed and "tech_auditor" in result.reports
    assert "Did not report" in as_markdown(result)


def test_an_audit_may_ask_one_auditor_and_refuses_a_name_it_does_not_have(tmp_path: Path) -> None:
    cfg = audit_config(tmp_path, {"tech_auditor": json.dumps(ANSWER)}, auditors=("tech_auditor",))
    result = run_audit(cfg)
    assert set(result.reports) == {"tech_auditor"}

    with pytest.raises(AuditError, match="is not an auditor"):
        audit_config(tmp_path, {}, auditors=("risk_manager",))


def test_neither_auditor_can_reach_a_venue_or_a_key() -> None:
    from trading_research_agents.agents import quant_auditor, tech_auditor
    from trading_research_agents.agents.scope import scope_of

    for module in (tech_auditor, quant_auditor):
        allowed = scope_of(module.ROLE)
        assert "nothing" in allowed.decides
        for forbidden in ("size", "venue", "key", "instrument"):
            assert forbidden not in allowed.reads
