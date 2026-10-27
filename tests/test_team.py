"""The deciding team: a lead, a blind peer on the same evidence, a flow specialist on its own.

Every answer is checked by code; the two structure reads must agree; the flow read is a voice in
conviction; the typed judge, when declared, checks that a reason describes the market the way the
measurements do.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from test_cycle import analyst_answer, cycle_config
from trading_research_agents.agents.cycle import run_cycle
from trading_research_agents.envelope.model import Envelope
from trading_research_agents.execution.conviction import Conviction, Tier
from trading_research_agents.llm.judge import Checked, FakeJudge, JudgeUnavailableError

FLOW_FIGURES = ("spread_bps", "funding_rate", "book_imbalance", "open_interest")


def flow_answer(target: str = "long", confidence: float = 0.8) -> str:
    return json.dumps(
        {
            "target": target,
            "confidence": confidence,
            "reason": "resting bids outweigh the asks near the touch",
            "figures_used": [{"figure": "interval_s", "value": "60"}],
        }
    )


def brief(result: Any, name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (Path(str(result.path)) / "briefs" / f"{name}.json").read_text("utf-8")
    )
    return loaded


def test_a_lead_alone_reads_the_whole_brief_as_before(tmp_path: Path, envelope: Envelope) -> None:
    cfg, fake, _ = cycle_config(tmp_path, envelope, {"structure_lead": analyst_answer()})
    result = run_cycle(cfg)
    assert result.acted and len(fake.calls) == 1
    assert {"spread_bps", "funding_rate"} <= set(brief(result, "structure_lead"))


def test_each_flow_figure_is_read_by_the_flow_analyst_alone(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, fake, _ = cycle_config(
        tmp_path,
        envelope,
        {"structure_lead": analyst_answer(), "flow_specialist": flow_answer("long", 0.8)},
        team=("flow_specialist",),
    )
    result = run_cycle(cfg)
    assert result.acted and len(fake.calls) == 2
    lead, flow = brief(result, "structure_lead"), brief(result, "flow_specialist")
    assert not set(FLOW_FIGURES) & set(lead)
    assert {"spread_bps", "funding_rate", "book_imbalance"} <= set(flow)
    # The flow read is a declared state conviction and confluence can weigh, from code's own check.
    said = result.context["flow_specialist"]
    assert said["declared_state"] == {"declared": "bullish", "score": "0.8"}
    assert said["read_by"] == "flow_specialist"


def test_the_peer_reads_the_same_brief_blind_and_a_disagreement_is_no_trade(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {
            "structure_lead": analyst_answer("long"),
            "blind_confirmer": analyst_answer("short"),
        },
        team=("blind_confirmer",),
    )
    result = run_cycle(cfg)
    assert not result.acted
    assert any("the structure reads disagree" in r for r in result.refusals)
    lead, peer = brief(result, "structure_lead"), brief(result, "blind_confirmer")
    assert lead == peer and "target" not in peer
    assert result.context["blind_confirmer"]["target"] == "short"


def test_two_reads_that_agree_trade_at_the_lower_confidence(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {
            "structure_lead": analyst_answer("long", confidence=0.9),
            "blind_confirmer": analyst_answer("long", confidence=0.7),
        },
        team=("blind_confirmer",),
    )
    result = run_cycle(cfg)
    assert result.acted and result.intent is not None
    assert result.intent["confidence"] == 0.7 and result.intent["team"] == {
        "structure_lead": "long",
        "blind_confirmer": "long",
    }


def test_a_peer_that_gives_nothing_usable_stops_the_trade(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {
            "structure_lead": analyst_answer("long"),
            "blind_confirmer": analyst_answer("long", cited=[{"figure": "made_up", "value": "1"}]),
        },
        team=("blind_confirmer",),
    )
    result = run_cycle(cfg)
    assert not result.acted
    assert any("blind_confirmer" in r and "both reads" in r for r in result.refusals)


def test_the_flow_read_weighs_in_conviction_against_the_direction(
    tmp_path: Path, envelope: Envelope
) -> None:
    rule = Conviction(
        weights={"structure_lead": Decimal(1), "flow_specialist": Decimal(1)},
        min_score=Decimal(0),
        ladder=(Tier(Decimal(0), Decimal(1)),),
    )
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {"structure_lead": analyst_answer("long", 0.9), "flow_specialist": flow_answer("short")},
        team=("flow_specialist",),
        conviction=rule,
    )
    result = run_cycle(cfg)
    assert result.stake is not None
    parts = result.stake.record()["score"]["contributions"]
    assert parts["flow_specialist"] == "-0.800" and parts["structure_lead"] == "0.900"


def test_the_judge_refuses_a_reason_that_contradicts_the_measured_regime(
    tmp_path: Path, envelope: Envelope
) -> None:
    for agrees, acted in ((0.1, False), (0.9, True)):
        judge = FakeJudge({"describes": Checked(agrees)}, model="jev-1.14.0")
        cfg, _, _ = cycle_config(
            tmp_path / str(agrees),
            envelope,
            {"structure_lead": analyst_answer()},
            judge=judge,
        )
        result = run_cycle(cfg)
        assert result.acted is acted
        state, _ = judge.asked[0]
        assert isinstance(state, dict) and "reason" in state and "regime" in state
        checked = result.context["structure_lead_check"]
        assert checked["model"] == "jev-1.14.0" and checked["describes"] == agrees
        refused = any("contradicting the measured regime" in r for r in result.refusals)
        assert refused is not acted


def test_a_judge_that_does_not_answer_is_absent_not_a_no(
    tmp_path: Path, envelope: Envelope
) -> None:
    class Down(FakeJudge):
        def evaluate(self, state: Any, questions: Any) -> Any:
            raise JudgeUnavailableError("timed out")

    cfg, _, _ = cycle_config(
        tmp_path, envelope, {"structure_lead": analyst_answer()}, judge=Down({})
    )
    result = run_cycle(cfg)
    assert result.acted
    assert any("judge" in r and "timed out" in r for r in result.refusals)


def test_the_config_names_the_same_team_the_cycle_runs() -> None:
    from trading_research_agents.agents.cycle.config import TEAM
    from trading_research_agents.trading import config_agents as config

    assert config.TEAM == TEAM


def test_the_report_and_the_live_table_name_every_member(
    tmp_path: Path, envelope: Envelope
) -> None:
    from trading_research_agents.cli.cycle_report import _cycle_report, _cycle_steps

    judge = FakeJudge({"describes": Checked(0.9)}, model="jev-1.14.0")
    cfg, _, usage = cycle_config(
        tmp_path,
        envelope,
        {
            "structure_lead": analyst_answer("long", 0.9),
            "blind_confirmer": analyst_answer("long", 0.8),
            "flow_specialist": flow_answer("short", 0.6),
        },
        team=("blind_confirmer", "flow_specialist"),
        judge=judge,
    )
    steps = _cycle_steps(cfg)
    assert (
        steps.index("structure_lead")
        < steps.index("blind_confirmer")
        < steps.index("flow_specialist")
    )
    assert "judge" in steps
    result = run_cycle(cfg)
    _, lines = _cycle_report(result, usage, "sandbox")
    text = "\n".join(lines)
    assert "confirmer: long" in text and "flow:      short at 0.6" in text
    assert "judge:     structure_lead" in text
    assert "flow_specialist read by code" not in text and "blind_confirmer read by code" not in text


def test_check_says_when_the_peer_is_the_same_model_as_the_lead() -> None:
    from test_config import AGENTS, agent_raw
    from trading_research_agents.cli.check_load import _load_notes
    from trading_research_agents.trading.config import parse_strategy_config

    same = parse_strategy_config(agent_raw(agents={**AGENTS, "team": ["blind_confirmer"]}))
    notes = "\n".join(_load_notes(same.agents, ()))
    assert "2 call(s)" in notes and "err together" in notes
    other = parse_strategy_config(
        agent_raw(
            agents={
                **AGENTS,
                "team": ["blind_confirmer", "flow_specialist"],
                "roles": {"blind_confirmer": {"provider": "google", "model": "other"}},
            }
        )
    )
    notes = "\n".join(_load_notes(other.agents, ()))
    assert "3 call(s)" in notes and "err together" not in notes


def test_a_desk_that_names_the_seats_as_they_were_called_before_runs_unchanged() -> None:
    import pytest

    from test_config import AGENTS, agent_raw
    from trading_research_agents.trading.config import parse_strategy_config
    from trading_research_agents.trading.config_agents import renamed_in
    from trading_research_agents.trading.config_values import ConfigError

    raw = agent_raw(
        agents={
            **AGENTS,
            "team": ["technical_peer", "flow_analyst"],
            "roles": {
                "technical_analyst": {"model": "m-lead"},
                "flow_analyst": {"model": "m-flow"},
            },
        }
    )
    cfg = parse_strategy_config(raw)
    assert cfg.agents is not None
    assert cfg.agents.team == ("blind_confirmer", "flow_specialist")
    assert cfg.agents.for_role("structure_lead").model == "m-lead"
    assert cfg.agents.for_role("flow_specialist").model == "m-flow"
    notes = renamed_in(raw)
    assert "[agents].team: technical_peer is blind_confirmer since 0.12" in notes
    assert "[agents.roles.technical_analyst] is [agents.roles.structure_lead] since 0.12" in notes
    # Every desk before 0.12 declared its decision core by the old name.
    old_core = {**raw, "strategy": {**raw["strategy"], "agent": "technical_analyst"}}
    assert parse_strategy_config(old_core).agents is not None
    assert "[strategy].agent = 'technical_analyst' is 'structure_lead' since 0.12" in renamed_in(
        old_core
    )
    both = agent_raw(agents={**AGENTS, "roles": {"technical_analyst": {}, "structure_lead": {}}})
    with pytest.raises(ConfigError, match="names structure_lead twice"):
        parse_strategy_config(both)


def test_a_cycle_written_before_the_rename_still_gives_its_previous_reading(
    tmp_path: Path, envelope: Envelope
) -> None:
    from datetime import timedelta

    from trading_research_agents.agents.cycle.briefing import _previous_reading

    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer("long", 0.9)])
    folder = Path(str(run_cycle(cfg).path))
    for kind in ("briefs", "answers"):
        (folder / kind / "structure_lead.json").rename(folder / kind / "technical.json")
    said = _previous_reading(
        cfg.root,
        cfg.instrument,
        "9999",
        None,
        now=cfg.clock() + timedelta(minutes=5),
        within_s=3600,
    )
    assert said is not None and said["target"] == "long" and said["confidence"] == 0.9
