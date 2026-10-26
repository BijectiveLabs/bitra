"""The forecast row on the arena's own fixture archive: fitted walk-forward, never on the future."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from test_evaluation_arena import START, archive, config  # noqa: F401  (archive is a fixture)
from trading_research_agents.data.schema import Direction
from trading_research_agents.evaluation.arena import run_arena
from trading_research_agents.evaluation.forecast import FitContext, ForecastPolicy
from trading_research_agents.evaluation.history import Views, load_history
from trading_research_agents.evaluation.records import write_arena
from trading_research_agents.evaluation.settle import funding_schedule
from trading_research_agents.market_data.days import DAY_NS, NS_PER_S, midnight_ns

HOUR_NS = 3600 * NS_PER_S


def policy(root: Path, **kwargs: object) -> ForecastPolicy:
    cfg = config()
    histories = {}
    for name in cfg.instruments:
        loaded = load_history(
            root,
            "bybit",
            name,
            START - timedelta(days=5),
            START + timedelta(days=12),
            (HOUR_NS, 4 * HOUR_NS),
        )
        histories[name] = loaded.history
    views = Views(histories, primary_ns=HOUR_NS, context_ns=4 * HOUR_NS, context=())
    fit = FitContext(
        views=views,
        histories=histories,
        instruments=cfg.instruments,
        costs=cfg.costs,
        funding={n: funding_schedule(h.points) for n, h in histories.items()},
        interval_ns=HOUR_NS,
        first_ns=midnight_ns(START) - 5 * DAY_NS,
    )
    return ForecastPolicy(fit, **kwargs)  # type: ignore[arg-type]


def test_a_sample_is_used_only_once_its_idea_had_settled(archive: Path) -> None:  # noqa: F811
    row = policy(archive, fit_days=10)
    at = midnight_ns(START) + 8 * DAY_NS
    columns = row.columns_before(at)
    # The fixture prints three trades a minute, so trade intensity never varies and is left out.
    assert "trade_intensity_z_24" not in columns and "mom_z_24" in columns
    rows, labels = row.samples_before(at, Direction.LONG, columns)
    assert rows and len(rows) == len(labels) and set(labels) <= {0.0, 1.0}
    for (instrument, decided, _side), found in row._labels.known.items():
        assert decided < at and instrument in ("BTCUSDT", "ETHUSDT")
        if found is not None and found[1] > at:
            # Settled after the refit: known to the cache, never handed to the fit.
            assert decided >= at - 25 * HOUR_NS


def test_without_enough_settled_outcomes_there_is_no_idea(archive: Path) -> None:  # noqa: F811
    row = policy(archive, min_samples=10**6)
    views = row.fit.views
    view = views.view("BTCUSDT", midnight_ns(START) + 2 * DAY_NS)
    assert row.decide(view) is None and row.score(view) is None


def test_the_forecast_row_runs_in_the_arena_the_same_way_twice(
    archive: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    cfg = config(rows=("forecast", "random"))
    first = run_arena(cfg, root=archive)
    names = [r["row"] for r in first.rows]
    assert names == ["forecast", "random"]
    a = write_arena(first, tmp_path / "a")
    b = write_arena(run_arena(cfg, root=archive), tmp_path / "b")
    assert (a / "arena.json").read_bytes() == (b / "arena.json").read_bytes()


def test_without_a_model_it_looks_again_the_next_day(archive: Path) -> None:  # noqa: F811
    row = policy(archive, min_samples=10**6)
    at = midnight_ns(START) + 2 * DAY_NS
    view = row.fit.views.view("BTCUSDT", at)
    row.decide(view)
    row.min_samples = 50
    assert row.decide(row.fit.views.view("BTCUSDT", at + HOUR_NS)) is None
    assert row._fitted is None
    row.decide(row.fit.views.view("BTCUSDT", at + DAY_NS))
    fitted = cast(Any, row)._fitted
    assert fitted is not None and fitted.samples >= 100


def test_days_before_the_archive_began_do_not_choose_the_features(archive: Path) -> None:  # noqa: F811
    row = policy(archive)
    # A fit context that starts a month before the archive does, as the arena's warm-up does.
    early = cast(Any, row.fit)
    object.__setattr__(early, "first_ns", midnight_ns(START) - 40 * DAY_NS)
    columns = row.columns_before(midnight_ns(START) + 8 * DAY_NS)
    assert "mom_z_24" in columns and "ret_bps_1" in columns
