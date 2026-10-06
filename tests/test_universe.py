"""A pass over the instruments a desk watches, before any of them costs a roster of models.

The screen is the trigger asked across a row instead of down one, so these tests check the two
things that follow from that: which instruments a pass admits, and the order it takes them in. The
screener role can only narrow what code admitted, and that is checked here too.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from test_cycle import chains, snapshot_with
from trading_research_agents.agents import market_screener
from trading_research_agents.agents.screen import figures_of, look, sweep
from trading_research_agents.execution.targets import intents_today
from trading_research_agents.execution.trigger import Trigger
from trading_research_agents.execution.universe import (
    Candidate,
    Universe,
    UniverseError,
    order,
    take,
)
from trading_research_agents.store.snapshot import cycle_id
from trading_research_agents.trading.config import ConfigError

NOW = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)


def candidate(name: str, **fields: Any) -> Candidate:
    base: dict[str, Any] = {"instrument": name, "admitted": True}
    base.update(fields)
    return Candidate(**base)


def cycle_on_disk(root: Path, name: str, instrument: str, **fields: Any) -> None:
    """A cycle on disk: the summary the trigger reads, and the intents the counter reads."""
    where = root / name
    where.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"instrument": instrument, "intent": None}
    payload.update(fields)
    (where / "cycle.json").write_text(json.dumps(payload), encoding="utf-8")
    if payload.get("intent") is not None:
        rows = json.dumps(payload["intent"]) + "\n"
        (where / "intents.jsonl").write_text(rows, encoding="utf-8")


def test_a_cycle_id_says_when_and_what_it_was_about() -> None:
    assert cycle_id(NOW) == "2026-09-27T10-00-00Z"
    assert cycle_id(NOW, "BTCUSDT") == "2026-09-27T10-00-00Z-BTCUSDT"
    # A venue symbol with a slash in it is still a directory name.
    assert cycle_id(NOW, "BTC/USDT:USDT") == "2026-09-27T10-00-00Z-BTCUSDTUSDT"


def test_the_order_is_movement_against_the_toll_then_the_operators_own_order() -> None:
    declared = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
    rows = [
        candidate("XRPUSDT"),  # never looked at before: no move to compare
        candidate("BTCUSDT", moved_bps=Decimal("20"), spread_bps=Decimal("10")),  # 2.0
        candidate("SOLUSDT", moved_bps=Decimal("45"), spread_bps=Decimal("9")),  # 5.0
        candidate("ETHUSDT", moved_bps=Decimal("30"), spread_bps=Decimal("10")),  # 3.0
    ]
    assert [c.instrument for c in order(rows, declared)] == [
        "SOLUSDT",
        "ETHUSDT",
        "BTCUSDT",
        "XRPUSDT",
    ]
    assert take(Universe(instruments=declared, max_per_cycle=2), rows) == ("SOLUSDT", "ETHUSDT")


def test_only_what_the_trigger_admitted_and_the_screener_kept_is_taken() -> None:
    declared = ("BTCUSDT", "ETHUSDT")
    rows = [
        candidate("BTCUSDT", admitted=False, moved_bps=Decimal("1"), spread_bps=Decimal("10")),
        candidate(
            "ETHUSDT",
            moved_bps=Decimal("30"),
            spread_bps=Decimal("10"),
            kept_by_screener=False,
        ),
    ]
    assert take(Universe(instruments=declared), rows) == ()


def test_a_universe_is_declared_once_and_checked() -> None:
    with pytest.raises(UniverseError, match="each instrument once"):
        Universe(instruments=("BTCUSDT", "BTCUSDT"))
    with pytest.raises(UniverseError, match="at least 1"):
        Universe(instruments=("BTCUSDT",), max_per_cycle=0)
    with pytest.raises(UniverseError, match="nothing to screen"):
        Universe(screener=True)
    assert Universe().idle and not Universe(instruments=("BTCUSDT",)).idle


def test_the_figures_of_one_instrument_are_free_and_say_how_old_they_are() -> None:
    figures = figures_of(snapshot_with())
    assert figures["mid"] == Decimal("102") and figures["spread_bps"] == Decimal("9.80")
    # closes 100, 101, 99, 100, 102 against a mid of 102: a range of three, in basis points.
    assert figures["range_bps"] == Decimal("294.12")
    assert figures["age_s"] is not None

    blind = figures_of(snapshot_with(book=False))
    assert blind["spread_bps"] is None and blind["mid"] == Decimal("102")


def test_an_instrument_nobody_has_looked_at_is_admitted_and_a_quiet_one_is_not(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cycles"
    root.mkdir(parents=True)
    first = look(
        "BTCUSDT", chains(), trigger=Trigger(min_move_bps=Decimal("20")), root=root, now=NOW
    )
    assert first.admitted and first.moved_bps is None

    cycle_on_disk(root, "2026-09-27T09-55-00Z-BTCUSDT", "BTCUSDT", seen={"mid": "102"})
    quiet = look(
        "BTCUSDT", chains(), trigger=Trigger(min_move_bps=Decimal("20")), root=root, now=NOW
    )
    assert not quiet.admitted and quiet.moved_bps == Decimal("0.00")
    assert any("nothing new to read" in r for r in quiet.reasons)


def test_a_pass_looks_at_everything_and_spends_a_cycle_on_the_top_of_the_order(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cycles"
    root.mkdir(parents=True)
    # Both instruments answer from the same stubbed book, so the one that moved is the one that
    # was last seen further away from it.
    cycle_on_disk(root, "2026-09-27T09-55-00Z-BTCUSDT", "BTCUSDT", seen={"mid": "102"})
    cycle_on_disk(root, "2026-09-27T09-55-00Z-ETHUSDT", "ETHUSDT", seen={"mid": "101"})

    screened = sweep(
        Universe(instruments=("BTCUSDT", "ETHUSDT"), max_per_cycle=1),
        chains(),
        trigger=Trigger(min_move_bps=Decimal("20")),
        root=root,
        now=NOW,
    )
    assert screened.chosen == ("ETHUSDT",)
    assert [c.instrument for c in screened.candidates] == ["ETHUSDT", "BTCUSDT"]
    assert screened.at == "2026-09-27T10-00-00Z"
    taken = {c.instrument: c for c in screened.candidates}
    assert taken["ETHUSDT"].admitted and not taken["BTCUSDT"].admitted
    assert not screened.asked_screener


def test_a_desk_that_has_traded_enough_today_stops_before_the_screen_chooses(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cycles"
    root.mkdir(parents=True)
    for i, name in enumerate(("BTCUSDT", "ETHUSDT")):
        cycle_on_disk(
            root,
            f"2026-09-27T09-5{i}-00Z-{name}",
            name,
            intent={"at": f"2026-09-27T09-5{i}-00Z-{name}", "instrument": name},
        )
    assert intents_today(root, at=NOW, instrument=None) == 2
    assert intents_today(root, at=NOW, instrument="BTCUSDT") == 1

    screened = sweep(
        Universe(instruments=("BTCUSDT", "ETHUSDT"), max_intents_per_day=2),
        chains(),
        trigger=Trigger(),
        root=root,
        now=NOW,
    )
    assert screened.chosen == ()
    assert any("across every instrument" in r for r in screened.refusals)


def test_the_screener_orders_what_code_admitted_and_cannot_add_or_pick_blind() -> None:
    from trading_research_agents.agents.screen import choose, screener_brief

    rows = [
        candidate("BTCUSDT", spread_bps=Decimal("0.01"), features={"zscore": 0.4}),
        candidate("ETHUSDT", spread_bps=Decimal("0.04"), features={"zscore": 2.85}),
        candidate("SOLUSDT", spread_bps=Decimal("0.84"), features={"regime": "ranging"}),
    ]
    brief = screener_brief(rows, mechanism="reversion after a burst", at="2026-09-27T10-00-00Z")
    said = market_screener.parse_live(
        json.dumps(
            {
                "picks": [
                    {
                        "instrument": "ETHUSDT",
                        "setup": "range_reversion",
                        "reason": "stretched",
                        "figures_used": [{"figure": "zscore", "value": "2.85"}],
                    },
                    {
                        "instrument": "DOGEUSDT",
                        "setup": "range_breakout",
                        "reason": "invented",
                        "figures_used": [{"figure": "zscore", "value": "1"}],
                    },
                    {
                        "instrument": "BTCUSDT",
                        "setup": "trend_continuation",
                        "reason": "made up",
                        "figures_used": [{"figure": "zscore", "value": "3.1"}],
                    },
                    {
                        "instrument": "SOLUSDT",
                        "setup": "range_reversion",
                        "reason": "a range",
                        "figures_used": [{"figure": "regime", "value": "ranging"}],
                    },
                ],
                "excluded": [],
            }
        )
    )
    chosen, refusals = choose(rows, said, brief)
    by = {c.instrument: c for c in chosen}
    assert by["ETHUSDT"].pick_rank == 1 and by["ETHUSDT"].setup == "range_reversion"
    # The second valid pick is ranked second, whatever position it had in the answer.
    assert by["SOLUSDT"].pick_rank == 2
    # A pick whose only citation does not hold is not taken.
    assert not by["BTCUSDT"].kept_by_screener and by["BTCUSDT"].pick_rank is None
    assert any("picked an instrument it was not given: 'DOGEUSDT'" in r for r in refusals)
    assert any("BTCUSDT picked on no figure from its own row" in r for r in refusals)
    ordered = [c.instrument for c in order(chosen, ("BTCUSDT", "ETHUSDT", "SOLUSDT")) if c.taken]
    assert ordered == ["ETHUSDT", "SOLUSDT"]


def test_a_screener_that_cannot_answer_does_not_stop_the_pass(tmp_path: Path) -> None:
    from trading_research_agents.agents.screen import asking

    class Broken:
        def complete(self, **_: Any) -> Any:
            raise TimeoutError("the provider did not answer")

    ask = asking(Broken(), mechanism="reversion after a burst of prints", at="2026-09-27T10-00-00Z")
    rows = [candidate("BTCUSDT")]
    narrowed, refusals = ask(rows)
    # Code already admitted these, so a silent screener leaves them where they were.
    assert [c.instrument for c in narrowed] == ["BTCUSDT"] and narrowed[0].taken
    assert any("TimeoutError" in r for r in refusals)


def test_the_universe_is_read_from_the_project() -> None:
    from test_config import agent_raw
    from trading_research_agents.trading.config import parse_strategy_config

    assert parse_strategy_config(agent_raw()).universe.idle

    cfg = parse_strategy_config(
        agent_raw(
            universe={
                "instruments": ["BTCUSDT", "ETHUSDT"],
                "max_per_cycle": 2,
                "max_intents_per_day": 6,
                "screener": True,
            }
        )
    )
    assert cfg.universe.instruments == ("BTCUSDT", "ETHUSDT")
    assert cfg.universe.max_per_cycle == 2 and cfg.universe.screener
    with pytest.raises(ConfigError, match="list of instrument names"):
        parse_strategy_config(agent_raw(universe={"instruments": "BTCUSDT"}))
    with pytest.raises(ConfigError, match="true or false"):
        parse_strategy_config(agent_raw(universe={"instruments": ["X"], "screener": "yes"}))
    with pytest.raises(ConfigError, match="unknown"):
        parse_strategy_config(agent_raw(universe={"symbols": ["X"]}))


def test_the_move_is_measured_even_when_no_minimum_is_declared(tmp_path: Path) -> None:
    """A pass is ordered by movement, so the figure cannot depend on a threshold being set."""
    root = tmp_path / "cycles"
    root.mkdir(parents=True)
    cycle_on_disk(root, "2026-09-27T09-55-00Z-BTCUSDT", "BTCUSDT", seen={"mid": "101"})
    # No trigger at all: everything is admitted, and the move is still on the record.
    seen = look("BTCUSDT", chains(), trigger=Trigger(), root=root, now=NOW)
    assert seen.admitted and seen.moved_bps == Decimal("99.01")
    assert seen.pressure == Decimal("10.10")


def test_an_empty_pass_says_which_rule_held_each_instrument() -> None:
    from trading_research_agents.execution.universe import why_no_cycle

    rows = [
        {
            "instrument": "ETHUSDT",
            "admitted": False,
            "reasons": ["a position is open at the venue; the next entry waits for its exits"],
        },
        {
            "instrument": "BTCUSDT",
            "admitted": False,
            "correlated_with": "ETHUSDT",
            "reasons": ["has been moving with ETHUSDT at 0.840 over the window"],
        },
        {
            "instrument": "SOLUSDT",
            "admitted": False,
            "correlated_with": "ETHUSDT",
            "reasons": ["has been moving with ETHUSDT at 0.824 over the window"],
        },
    ]
    said = why_no_cycle(rows)
    assert said.startswith(
        "ETHUSDT open at the venue; BTCUSDT, SOLUSDT moving with the open ETHUSDT"
    )
    assert "one position at a time" in said
    quiet = [
        {
            "instrument": "BTCUSDT",
            "admitted": False,
            "reasons": ["the mid has moved 3.2 bps since the last cycle"],
        }
    ]
    assert why_no_cycle(quiet) == "BTCUSDT not moved enough for [trigger]"


def test_a_full_desk_says_so_instead_of_paying_for_a_refused_cycle() -> None:
    from trading_research_agents.execution.universe import why_no_cycle

    rows = [
        {
            "instrument": "ETHUSDT",
            "admitted": False,
            "reasons": ["a position is open at the venue"],
        },
        {
            "instrument": "SOLUSDT",
            "admitted": False,
            "reasons": ["a position is open at the venue"],
        },
        {
            "instrument": "BTCUSDT",
            "admitted": False,
            "reasons": ["the desk holds 2 position(s), its declared [desk].max_open_positions"],
        },
    ]
    said = why_no_cycle(rows)
    assert "ETHUSDT, SOLUSDT open at the venue" in said
    assert "BTCUSDT waiting: the desk is at [desk].max_open_positions" in said


def test_an_open_position_is_named_first_even_when_the_trigger_held_it_too() -> None:
    from trading_research_agents.execution.universe import why_no_cycle

    # Seen live: ETH and SOL open, the desk full, and all three also under the trigger's line.
    rows = [
        {
            "instrument": "ETHUSDT",
            "admitted": False,
            "reasons": ["a position is open at the venue", "the mid has moved 2.1 bps since"],
        },
        {
            "instrument": "SOLUSDT",
            "admitted": False,
            "reasons": ["a position is open at the venue", "the mid has moved 4.0 bps since"],
        },
        {
            "instrument": "BTCUSDT",
            "admitted": False,
            "reasons": [
                "the desk holds 2 position(s), its declared [desk].max_open_positions",
                "the mid has moved 3.3 bps since",
            ],
        },
    ]
    said = why_no_cycle(rows)
    assert said == (
        "ETHUSDT, SOLUSDT open at the venue; "
        "BTCUSDT waiting: the desk is at [desk].max_open_positions"
    )


def test_a_pass_measures_every_candidate_and_runs_what_the_screener_put_first(
    tmp_path: Path,
) -> None:
    from trading_research_agents.agents.screen import asking
    from trading_research_agents.llm.completer import FakeCompleter

    answer = {
        "picks": [
            {
                "instrument": "SOLUSDT",
                "setup": "range_reversion",
                "reason": "a range",
                "figures_used": [{"figure": "spread_bps", "value": "9.80"}],
            },
            {
                "instrument": "ETHUSDT",
                "setup": "range_reversion",
                "reason": "also a range",
                "figures_used": [{"figure": "spread_bps", "value": "9.80"}],
            },
        ],
        "excluded": [{"instrument": "BTCUSDT", "reason": "nothing on its own row"}],
    }
    screened = sweep(
        Universe(instruments=("BTCUSDT", "ETHUSDT", "SOLUSDT"), max_per_cycle=1, screener=True),
        chains(),
        trigger=Trigger(),
        root=tmp_path / "cycles",
        now=NOW,
        ask=asking(FakeCompleter([json.dumps(answer)]), mechanism="reversion", at="x"),
        listed={"SOLUSDT": {"quote_volume_24h": "592230876", "change_24h_pct": "-0.17"}},
    )
    assert screened.chosen == ("SOLUSDT",)
    # Code's own order is kept beside the screener's, for the comparison later.
    assert screened.code_order == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    by = {c.instrument: c for c in screened.candidates}
    assert by["SOLUSDT"].pick_rank == 1 and by["ETHUSDT"].pick_rank == 2
    assert by["SOLUSDT"].features["quote_volume_24h"] == "592230876"
    assert "regime" in by["BTCUSDT"].features and "zscore" in by["BTCUSDT"].features
    assert not by["BTCUSDT"].kept_by_screener
