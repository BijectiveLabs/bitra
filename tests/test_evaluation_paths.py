"""The paths row on the arena's fixture archive: read from closed bars only, the same way twice."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from test_evaluation_arena import START, archive, config  # noqa: F401  (archive is a fixture)
from trading_research_agents.evaluation.arena import run_arena
from trading_research_agents.evaluation.history import Views, load_history
from trading_research_agents.evaluation.paths import PathsPolicy
from trading_research_agents.evaluation.records import write_arena
from trading_research_agents.evaluation.rows import UnknownRowError, make_policy
from trading_research_agents.market_data.days import DAY_NS, NS_PER_S, midnight_ns

HOUR_NS = 3600 * NS_PER_S


def views(root: Path) -> Views:
    cfg = config()
    histories = {
        name: load_history(
            root,
            "bybit",
            name,
            START - timedelta(days=5),
            START + timedelta(days=12),
            (HOUR_NS, 4 * HOUR_NS),
        ).history
        for name in cfg.instruments
    }
    return Views(histories, primary_ns=HOUR_NS, context_ns=4 * HOUR_NS, context=())


def test_an_idea_comes_from_closed_bars_and_names_its_odds(archive: Path) -> None:  # noqa: F811
    row = PathsPolicy(config().costs)
    view = views(archive).view("BTCUSDT", midnight_ns(START) + 3 * DAY_NS)
    score = row.score(view)
    assert score is not None
    idea = row.decide(view)
    if idea is not None:
        assert idea.playbook == "paths" and "take first" in idea.reason
    assert row.decide(view) == idea


def test_a_view_too_short_to_draw_from_gives_no_idea(archive: Path) -> None:  # noqa: F811
    row = PathsPolicy(config().costs)
    early = views(archive).view("BTCUSDT", midnight_ns(START) - 5 * DAY_NS + 2 * HOUR_NS)
    assert row.decide(early) is None and row.score(early) is None


def test_the_paths_row_runs_in_the_arena_the_same_way_twice(
    archive: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    cfg = config(rows=("paths", "random"))
    first = run_arena(cfg, root=archive)
    assert [r["row"] for r in first.rows] == ["paths", "random"]
    a = write_arena(first, tmp_path / "a")
    b = write_arena(run_arena(cfg, root=archive), tmp_path / "b")
    assert (a / "arena.json").read_bytes() == (b / "arena.json").read_bytes()


def test_the_row_needs_the_arenas_costs() -> None:
    try:
        make_policy("paths", seed=0, end_ns=DAY_NS, interval_ns=HOUR_NS)
    except UnknownRowError as exc:
        assert "costs" in str(exc)
    else:
        raise AssertionError("the paths row was made without the arena's costs")
