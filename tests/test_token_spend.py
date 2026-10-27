"""Spending tokens only where they can change something."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from test_cycle import analyst_answer, cycle_config
from trading_research_agents.agents.cycle import run_cycle
from trading_research_agents.envelope.model import Envelope
from trading_research_agents.execution.targets import Targets

START = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)


def test_reviewers_are_not_asked_when_code_will_refuse_anyway(
    tmp_path: Path, envelope: Envelope
) -> None:
    # The fixture's round trip is about 13.8 bps without impact; 40 bps claimed is under 4x.
    cfg, fake, _ = cycle_config(
        tmp_path,
        envelope,
        {"structure_lead": analyst_answer()},
        reviewers=("pro", "contra", "neutral"),
        targets=Targets(min_edge_multiple=Decimal("4")),
    )
    result = run_cycle(cfg)
    assert "reviewer" not in fake.per_role
    assert any(
        r.startswith("targets: the claimed edge is at most") and "reviewers were not asked" in r
        for r in result.refusals
    )


def test_table_readers_think_low_only_where_an_effort_is_declared() -> None:
    from test_config import agent_raw
    from trading_research_agents.trading.config import parse_strategy_config

    plain = parse_strategy_config(agent_raw()).agents
    assert plain is not None and plain.for_role("reviewer").effort is None
    raw = agent_raw()
    raw["agents"] = {
        **raw["agents"],
        "effort": "medium",
        "roles": {"reviewer": {"effort": "high"}},
    }
    agents = parse_strategy_config(raw).agents
    assert agents is not None
    assert agents.for_role("reviewer").effort == "high"
    assert agents.for_role("market_screener").effort == "low"
    assert agents.for_role("structure_lead").effort == "medium"


def test_briefs_go_to_the_model_without_spaces() -> None:
    from trading_research_agents.agents.cycle.asking import _ask
    from trading_research_agents.llm.completer import FakeCompleter

    fake = FakeCompleter(['{"x": 1}'])

    class Config:
        @staticmethod
        def completer_for(role: str) -> FakeCompleter:
            return fake

    _ask(Config(), "reviewer", {"a": 1, "b": [1, 2]}, {})  # type: ignore[arg-type]
    assert fake.calls[0].user == json.dumps({"a": 1, "b": [1, 2]}, separators=(",", ":"))


def test_a_quote_cap_holds_every_instrument_to_the_same_money() -> None:
    from types import SimpleNamespace

    from trading_research_agents.agents.cycle.pricing import _position_cap

    # 800 USDT of DOGE at 0.2 is 4,000 units; a 10-unit cap written for BTC must not apply.
    doge = SimpleNamespace(max_position=None, max_position_quote=Decimal(800), size_step=Decimal(1))
    assert _position_cap(doge, Decimal("0.2"), Decimal(2000)) == Decimal(4000)  # type: ignore[arg-type]
    btc = SimpleNamespace(
        max_position=Decimal(10), max_position_quote=Decimal(800), size_step=Decimal("0.001")
    )
    assert _position_cap(btc, Decimal(80000), Decimal("0.005")) == Decimal("0.010")  # type: ignore[arg-type]
    neither = SimpleNamespace(max_position=None, max_position_quote=None, size_step=Decimal(1))
    assert _position_cap(neither, Decimal(1), Decimal(7)) == Decimal(7)  # type: ignore[arg-type]
