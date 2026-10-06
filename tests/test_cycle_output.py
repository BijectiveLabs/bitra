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
        "chosen": ["BTCUSDT"],
        "candidates": [
            {
                "instrument": "BTCUSDT",
                "admitted": True,
                "kept_by_screener": True,
                "screener_said": "pressure shows a burst large enough for short-horizon reversion",
            },
            {
                "instrument": "ETHUSDT",
                "admitted": True,
                "kept_by_screener": True,
                "screener_said": "range is wide enough",
            },
        ],
    }
    text = shown(screen_view(record))
    assert "waiting" in text and "max_per_cycle was taken by BTCUSDT" in text
    assert "large enough for short-horizon reversion" in text
    assert "the screener model read every admitted one" in text


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
