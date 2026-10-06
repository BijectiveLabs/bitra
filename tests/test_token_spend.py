"""Spending tokens only where they can change something."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from test_cycle import analyst_answer, cycle_config, reading_answer, series_for
from trading_research_agents.agents.cycle import run_cycle
from trading_research_agents.envelope.model import Envelope
from trading_research_agents.execution.targets import Targets
from trading_research_agents.store.reuse import key_of, lookup, remember

START = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)


def test_the_key_is_the_figures_not_the_clock_or_the_instrument() -> None:
    one = {"as_of": "10:00", "instrument": "BTCUSDT", "series": [{"value": "4.4", "age_h": 3.0}]}
    later = {"as_of": "10:05", "instrument": "ETHUSDT", "series": [{"value": "4.4", "age_h": 3.1}]}
    moved = {"as_of": "10:05", "instrument": "BTCUSDT", "series": [{"value": "4.5", "age_h": 0.1}]}
    assert key_of(one) == key_of(later) != key_of(moved)


def test_a_reused_answer_stands_only_inside_its_window(tmp_path: Path) -> None:
    remember(tmp_path, "macro_analyst", "k", "answer", now=START)
    assert lookup(tmp_path, "macro_analyst", "k", window_s=3600, now=START + timedelta(minutes=59))
    assert not lookup(tmp_path, "macro_analyst", "k", window_s=3600, now=START + timedelta(hours=2))
    assert not lookup(tmp_path, "macro_analyst", "k", window_s=0, now=START)
    assert not lookup(tmp_path, "macro_analyst", "other", window_s=3600, now=START)


def test_unchanged_reading_figures_are_not_paid_for_twice(
    tmp_path: Path, envelope: Envelope
) -> None:
    macro = series_for(tmp_path, "us10y", "4.4", "{ bearish_above = 4.25 }")
    answers: dict[str, Any] = {
        "technical_analyst": analyst_answer(),
        "macro_analyst": reading_answer("macro_analyst"),
    }
    common: dict[str, Any] = {
        "series": {"macro_analyst": (macro,)},
        "analysts": ("technical_analyst", "macro_analyst"),
        "reuse_reading_s": 3600.0,
    }
    cfg, first_fake, _ = cycle_config(tmp_path, envelope, dict(answers), **common)
    first = run_cycle(cfg)
    assert not first.record().get("reused")
    assert len(first_fake.per_role["macro_analyst"].calls) == 1

    # Five minutes later, the same series: the macro analyst is not asked again.
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {"technical_analyst": analyst_answer()},
        clock=lambda: START + timedelta(minutes=5),
        **common,
    )
    second = run_cycle(cfg)
    assert second.record()["reused"] == {"macro_analyst": 300}
    assert second.context["macro_analyst"]["regime"] == "risk-off"
    assert any(o.name == "macro_analyst" and o.reused for o in second.fan.outcomes)

    # New data asks again, inside the window or not.
    (tmp_path / "us10y.csv").write_text(
        "date,value\n2026-09-20,4.4\n2026-09-26,4.6\n", encoding="utf-8"
    )
    cfg, third_fake, _ = cycle_config(
        tmp_path, envelope, dict(answers), clock=lambda: START + timedelta(minutes=10), **common
    )
    assert not run_cycle(cfg).record().get("reused")
    assert len(third_fake.per_role["macro_analyst"].calls) == 1


def test_validators_are_not_asked_when_code_will_refuse_anyway(
    tmp_path: Path, envelope: Envelope
) -> None:
    # The fixture's round trip is about 13.8 bps without impact; 40 bps claimed is under 4x.
    cfg, fake, _ = cycle_config(
        tmp_path,
        envelope,
        {"technical_analyst": analyst_answer()},
        validators=("pro", "contra", "neutral"),
        targets=Targets(min_edge_multiple=Decimal("4")),
    )
    result = run_cycle(cfg)
    assert "validator" not in fake.per_role
    assert any(
        r.startswith("targets: the claimed edge is at most") and "validators were not asked" in r
        for r in result.refusals
    )


def test_table_readers_think_low_only_where_an_effort_is_declared() -> None:
    from test_config import agent_raw
    from trading_research_agents.trading.config import parse_strategy_config

    plain = parse_strategy_config(agent_raw()).agents
    assert plain is not None and plain.for_role("validator").effort is None
    raw = agent_raw()
    raw["agents"] = {
        **raw["agents"],
        "effort": "medium",
        "roles": {"macro_analyst": {"effort": "high"}},
    }
    agents = parse_strategy_config(raw).agents
    assert agents is not None
    assert agents.for_role("validator").effort == "low"
    assert agents.for_role("market_screener").effort == "low"
    assert agents.for_role("technical_analyst").effort == "medium"
    assert agents.for_role("macro_analyst").effort == "high"
    assert agents.reuse_reading_s == 3600.0


def test_briefs_go_to_the_model_without_spaces() -> None:
    from trading_research_agents.agents.cycle import _ask
    from trading_research_agents.llm.completer import FakeCompleter

    fake = FakeCompleter(['{"x": 1}'])

    class Config:
        @staticmethod
        def completer_for(role: str) -> FakeCompleter:
            return fake

    _ask(Config(), "macro_analyst", {"a": 1, "b": [1, 2]}, {})  # type: ignore[arg-type]
    assert fake.calls[0].user == json.dumps({"a": 1, "b": [1, 2]}, separators=(",", ":"))
