"""What a person reads after a cycle: the venue answer, the universe, conviction, the hold."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.console import Console

from test_cycle import analyst_answer, cycle_config
from trading_research_agents.agents.cycle import run_cycle
from trading_research_agents.cli.tui import cycle_view, screen_view
from trading_research_agents.envelope.model import Envelope


def shown(view: Any) -> str:
    console = Console(record=True, width=220)
    console.print(view)
    return console.export_text()


def test_an_order_the_venue_hand_refused_is_said_plainly(
    tmp_path: Path, envelope: Envelope
) -> None:
    def hand(intent: dict[str, Any]) -> dict[str, Any]:
        return {"sent": False, "refused": "the book moved 11.89 bps against the trade"}

    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer()], mode="venue", place=hand)
    result = run_cycle(cfg)
    assert "venue: not sent, the book moved 11.89 bps against the trade" in result.refusals
    assert "NOT SENT: the book moved 11.89 bps against the trade" in shown(
        cycle_view(result.record())
    )


def test_funding_is_priced_over_the_hold_the_desk_keeps(tmp_path: Path, envelope: Envelope) -> None:
    cfg, _, _ = cycle_config(
        tmp_path, envelope, [analyst_answer(horizon=5)], hold_limit="timeout", timeout_bars=240
    )
    funding = next(
        c
        for c in run_cycle(cfg).record()["cost"]["cost"]["components"]
        if c["name"] == "funding_accrual"
    )
    assert "inside a 14400s horizon" in funding["source"]

    cfg, _, _ = cycle_config(
        tmp_path / "h",
        envelope,
        [analyst_answer(horizon=5)],
        hold_limit="horizon",
        timeout_bars=240,
    )
    funding = next(
        c
        for c in run_cycle(cfg).record()["cost"]["cost"]["components"]
        if c["name"] == "funding_accrual"
    )
    assert "inside a 300s horizon" in funding["source"]


def test_an_admitted_instrument_that_did_not_run_says_why() -> None:
    record = {
        "chosen": ["ETHUSDT"],
        "code_order": ["BTCUSDT", "ETHUSDT"],
        "asked_screener": True,
        "candidates": [
            {
                "instrument": "BTCUSDT",
                "admitted": True,
                "kept_by_screener": True,
                "pick_rank": 2,
                "setup": "range_reversion",
                "screener_said": "#2 range_reversion: a burst large enough for reversion",
                "features": {"regime": "ranging", "zscore": 1.2, "quote_volume_24h": "4167294740"},
            },
            {
                "instrument": "ETHUSDT",
                "admitted": True,
                "kept_by_screener": True,
                "pick_rank": 1,
                "setup": "range_breakout",
                "screener_said": "#1 range_breakout: the range is giving way",
                "features": {"regime": "mixed", "zscore": 2.85},
            },
        ],
        "discovered": {"markets": 782, "kept": [{}, {}], "dropped": {"not crypto": 260}},
    }
    text = shown(screen_view(record))
    assert "waiting" in text and "max_per_cycle was taken by ETHUSDT" in text
    assert "ahead on the screener's order" in text
    assert "#1 range_breakout" in text and "4,167M" in text
    assert "the screener chose, code's own order was BTCUSDT, ETHUSDT" in text
    assert "2 of 782 perpetuals kept by code (260 not crypto)" in text


def test_each_conviction_voice_says_which_way_it_pulled() -> None:
    record = {
        "conviction": {
            "admitted": True,
            "size": "0.002",
            "multiple": "0.5",
            "score": {
                "score": "0.563",
                "weights": {"macro_analyst": "1.0", "sentiment_analyst": "0.5"},
                "contributions": {"macro_analyst": "0.465", "sentiment_analyst": "-0.186"},
            },
        }
    }
    text = shown(cycle_view(record))
    assert "+0.465 of 1.0, for the trade" in text
    assert "-0.186 of 0.5, against it" in text


def test_a_malformed_answer_is_asked_for_once_more() -> None:
    from types import SimpleNamespace

    from trading_research_agents.agents import validator
    from trading_research_agents.agents.cycle import _ask_readable
    from trading_research_agents.llm.completer import FakeCompleter

    good = {"stance": "contra", "claims": [], "verdict": "abstain", "reason": "nothing settles it"}
    fake = FakeCompleter(['{"stance": "contra", "claims": [], "reason", ":"}', good])
    cfg = SimpleNamespace(completer_for=lambda role: fake)
    text = _ask_readable(cfg, validator.ROLE, {}, validator.SCHEMA, validator.parse)  # type: ignore[arg-type]
    assert validator.parse(text).verdict == "abstain" and len(fake.calls) == 2


def test_a_server_that_cannot_enforce_a_schema_falls_back_to_json_mode() -> None:
    from types import SimpleNamespace

    from trading_research_agents.llm.providers.openai import OpenAICompleter

    sent: list[dict[str, Any]] = []

    def create(**body: Any) -> Any:
        sent.append(body)
        if body["response_format"]["type"] == "json_schema":
            raise RuntimeError("Error code: 400 - response_format json_schema is not supported")
        message = SimpleNamespace(content='{"ok": true}', refusal=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            model="m",
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    completer = OpenAICompleter("m", client=client, key_env=None)
    assert (
        completer.complete(system="s", user="u", schema={"type": "object"}).text == '{"ok": true}'
    )
    assert [b["response_format"]["type"] for b in sent] == ["json_schema", "json_object"]
    assert completer.structured == "json_object"


def test_an_answer_that_came_back_unreadable_is_not_shown_as_ok() -> None:
    record = {
        "agents": {"agents": [{"agent": "validator_contra", "ok": True, "seconds": 15.6}]},
        "context": {"validator_contra": {"stance": "contra", "error": "lacks verdict"}},
    }
    assert "unreadable" in shown(cycle_view(record))


def test_a_validator_counts_for_what_it_says_beyond_its_stance() -> None:
    from types import SimpleNamespace

    from trading_research_agents.agents.cycle import _informative_votes

    def votes(pro: str, contra: str, neutral: str) -> tuple[str, ...]:
        context = {
            "validator_pro": {"verdict": pro},
            "validator_contra": {"verdict": contra},
            "validator_neutral": {"verdict": neutral},
            "macro_analyst": {"regime": "risk-off"},
        }
        return _informative_votes(SimpleNamespace(context=context))  # type: ignore[arg-type]

    # Pro for and contra against, as assigned: only neutral says anything.
    assert votes("support", "oppose", "support") == ("abstain", "support", "abstain")
    # A pro that cannot argue for it, and a contra that cannot argue against it, both count.
    assert votes("abstain", "abstain", "oppose") == ("support", "oppose", "oppose")
    assert votes("oppose", "support", "abstain") == ("support", "abstain", "oppose")


def test_a_cycle_that_ended_early_says_which_panels_it_never_reached() -> None:
    record = {
        "analyst": {"target": "long", "setup": "range_reversion"},
        "conviction": {"admitted": False, "score": {"score": "0.337"}},
        "refusals": ["conviction: score 0.337 is below the declared floor 0.4"],
    }
    text = shown(cycle_view(record))
    assert "not reached" in text and "risk per trade, desk, targets, intent" in text
    assert "the cycle ended at conviction" in text
