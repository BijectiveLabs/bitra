"""External series: known only once published, measured with a sample size, thresholds declared."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from trading_research_agents.data.series import (
    Point,
    Series,
    SeriesError,
    SeriesSpec,
    Threshold,
    align_asof,
    correlation,
    lead_lag,
    load_all,
    load_news,
    load_news_map,
    load_series_map,
    net_flow,
    read_events,
    read_series,
    threshold_state,
    upcoming,
    zscore,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
NS = 1_000_000_000


def ns(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * NS)


def spec(name: str = "cpi", lag: str = "14d", **threshold: float) -> SeriesSpec:
    return SeriesSpec(
        name=name,
        file=f"{name}.csv",
        ts="date",
        ts_unit="iso8601",
        value="value",
        unit="percent",
        publish_lag=timedelta(days=int(lag[:-1])) if lag.endswith("d") else timedelta(0),
        source="test",
        threshold=Threshold(**{k: Decimal(str(v)) for k, v in threshold.items()}),
    )


def test_the_example_map_declares_thresholds_and_never_a_correlation() -> None:
    specs = load_series_map(EXAMPLES / "series.toml")
    names = [s.name for s in specs]
    assert "us10y" in names and "cpi_yoy" in names and "exchange_netflow_btc" in names
    cpi = next(s for s in specs if s.name == "cpi_yoy")
    assert cpi.publish_lag == timedelta(days=14)
    assert cpi.threshold.bullish_above == Decimal("3.0") and cpi.threshold.declared
    fed = next(s for s in specs if s.name == "fed_funds")
    assert not fed.threshold.declared
    text = (EXAMPLES / "series.toml").read_text("utf-8").lower()
    assert "correlation" not in text.replace("the correlation of each", "")


def test_a_print_is_not_known_before_it_is_published(tmp_path: Path) -> None:
    (tmp_path / "cpi.csv").write_text(
        "date,value\n2025-01-31T00:00:00Z,3.1\n2025-02-28T00:00:00Z,2.8\n", encoding="utf-8"
    )
    series = read_series(spec(), tmp_path)
    assert len(series.points) == 2
    # The January print is observed on the 31st and published fourteen days later.
    assert series.as_of(ns("2025-02-01T00:00:00")) is None
    assert series.as_of(ns("2025-02-14T00:00:00")) is None  # strictly before, not at
    known = series.as_of(ns("2025-02-14T00:00:01"))
    assert known is not None and known.value == Decimal("3.1")
    later = series.as_of(ns("2025-03-20T00:00:00"))
    assert later is not None and later.value == Decimal("2.8")
    values = align_asof(series, [ns("2025-02-01T00:00:00"), ns("2025-03-20T00:00:00")])
    assert values == [None, Decimal("2.8")]


def test_threshold_state_is_labelled_as_the_operators_declaration(tmp_path: Path) -> None:
    (tmp_path / "y.csv").write_text("date,value\n2025-01-01T00:00:00Z,3.2\n", encoding="utf-8")
    s = read_series(spec("y", "0d", bullish_below=3.5, bearish_above=4.25), tmp_path)
    state = threshold_state(s, ns("2025-01-02T00:00:00"))
    assert state["state"] == "bullish" and state["basis"] == "operator-declared threshold"
    assert state["value"] == "3.2" and state["known_since"].startswith("2025-01-01")
    undeclared = read_series(spec("y", "0d"), tmp_path)
    assert threshold_state(undeclared, ns("2025-01-02T00:00:00"))["state"] == "undeclared"
    assert threshold_state(undeclared, ns("2024-12-31T00:00:00"))["state"] == "unknown"
    assert Threshold(bearish_above=Decimal("4.25")).state(Decimal("5")) == "bearish"
    assert Threshold(bullish_above=Decimal("3")).state(Decimal("2")) == "neutral"


def test_correlation_carries_its_sample_size_and_refuses_too_few() -> None:
    points = tuple(Point(i * NS, i * NS, Decimal(i)) for i in range(10))
    s = Series(spec("lin", "0d"), points)
    times = [(i + 1) * NS for i in range(10)]
    up = [float(i) for i in range(10)]
    m = correlation(s, times, up, window="10 steps")
    assert m.value is not None and m.value == pytest.approx(1.0) and m.n == 10
    down = [float(-i) for i in range(10)]
    assert correlation(s, times, down, window="w").value == pytest.approx(-1.0)
    few = correlation(s, times[:2], up[:2], window="w")
    assert few.value is None and few.n == 2 and "fewer than three" in few.note
    flat = correlation(
        Series(spec("f", "0d"), tuple(Point(i * NS, i * NS, Decimal(1)) for i in range(10))),
        times,
        up,
        window="w",
    )
    assert flat.value is None


def test_lead_lag_differences_the_series_and_finds_the_shift() -> None:
    # The series changes one step before the returns move: lag +1 should correlate.
    values = [0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6]
    points = tuple(Point(i * NS, i * NS, Decimal(v)) for i, v in enumerate(values))
    s = Series(spec("lead", "0d"), points)
    times = [(i + 1) * NS for i in range(len(values))]
    changes = [0.0, *(float(b - a) for a, b in pairwise(values))]
    returns = [0.0, *changes[:-1]]  # the market follows one step later
    table = lead_lag(s, times, returns, max_lag=2, window="12 steps")
    by_lag = {row["lag"]: row for row in table}
    assert by_lag[1]["value"] == pytest.approx(1.0) and by_lag[1]["n"] >= 9
    assert by_lag[0]["value"] is not None and by_lag[0]["value"] < 0.5


def test_zscore_and_net_flow_use_only_what_was_known(tmp_path: Path) -> None:
    rows = "\n".join(
        f"2025-01-{d:02d}T00:00:00Z,{v}" for d, v in enumerate([10, 10, 10, 10, 20], start=1)
    )
    (tmp_path / "flow.csv").write_text("date,value\n" + rows + "\n", encoding="utf-8")
    s = read_series(spec("flow", "0d"), tmp_path)
    z = zscore(s, ns("2025-01-06T00:00:00"), window_points=5)
    assert z.value is not None and z.value > 1.5 and z.n == 5
    assert zscore(s, ns("2025-01-02T00:00:00"), window_points=5).value is None
    flow = net_flow(s, ns("2025-01-06T00:00:00"), window=timedelta(days=3))
    assert flow.value == 40.0 and flow.n == 3
    assert net_flow(s, ns("2024-12-01T00:00:00"), window=timedelta(days=3)).n == 0


def test_bad_files_stop_with_the_row(tmp_path: Path) -> None:
    # A bare date is read as UTC midnight, and the operator's publication lag is what makes it
    # knowable. A time of day with no zone is the ambiguous case, and that one stops the file.
    (tmp_path / "plain.csv").write_text("date,value\n2025-01-01,3\n", encoding="utf-8")
    assert read_series(spec("plain", "0d"), tmp_path).points[0].value == Decimal("3")
    (tmp_path / "bad.csv").write_text("date,value\n2025-01-01 09:30:00,3\n", encoding="utf-8")
    with pytest.raises(SeriesError, match="row 2"):
        read_series(spec("bad", "0d"), tmp_path)
    (tmp_path / "nan.csv").write_text("date,value\n2025-01-01T00:00:00Z,NaN\n", encoding="utf-8")
    with pytest.raises(SeriesError, match="not finite"):
        read_series(spec("nan", "0d"), tmp_path)
    with pytest.raises(SeriesError, match="does not exist"):
        read_series(spec("missing", "0d"), tmp_path)
    (tmp_path / "series.toml").write_text('[[series]]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(SeriesError, match="names its file"):
        load_series_map(tmp_path / "series.toml")
    (tmp_path / "series.toml").write_text(
        '[[series]]\nname = "x"\nfile = "x.csv"\npublish_lag = "soon"\n', encoding="utf-8"
    )
    with pytest.raises(SeriesError, match="publish_lag"):
        load_series_map(tmp_path / "series.toml")


def test_json_series_and_events_load(tmp_path: Path) -> None:
    (tmp_path / "series.toml").write_text(
        '[[series]]\nname = "j"\nfile = "j.json"\nts = "t"\nvalue = "v"\npublish_lag = "1h"\n',
        encoding="utf-8",
    )
    (tmp_path / "j.json").write_text(
        '[{"t": "2025-01-01T00:00:00Z", "v": "1.5"}, {"t": "2025-01-01T01:00:00Z", "v": "2"}]',
        encoding="utf-8",
    )
    (series,) = load_all(tmp_path / "series.toml")
    assert series.points[0].published_ns - series.points[0].observed_ns == 3600 * NS
    (tmp_path / "events.json").write_text(
        '[{"when": "2025-01-10T13:30:00Z", "what": "CPI release", "source": "calendar"},'
        ' {"when": "2025-01-03T00:00:00Z", "what": "earlier"}]',
        encoding="utf-8",
    )
    events = read_events(tmp_path / "events.json")
    assert [e.what for e in events] == ["earlier", "CPI release"]
    soon = upcoming(events, ns("2025-01-09T00:00:00"), horizon=timedelta(days=2))
    assert [e.what for e in soon] == ["CPI release"] and soon[0].record()["when"].startswith(
        "2025-01-10"
    )


def test_news_declares_dated_text_files_and_reads_them_as_one_list(tmp_path: Path) -> None:
    (tmp_path / "news.toml").write_text(
        '[[news]]\nname = "policy"\nfile = "news/policy.csv"\nsource = "central bank"\n'
        '[[news]]\nname = "desk"\nfile = "news/desk.csv"\n',
        encoding="utf-8",
    )
    (tmp_path / "news").mkdir()
    (tmp_path / "news" / "policy.csv").write_text(
        "when,what\n2025-02-01T13:30:00Z,no change\n", encoding="utf-8"
    )
    (tmp_path / "news" / "desk.csv").write_text(
        "when,what,source\n2025-01-20T09:00:00Z,thin book into the close,the desk\n",
        encoding="utf-8",
    )

    specs = load_news_map(tmp_path / "news.toml")
    assert [s.name for s in specs] == ["policy", "desk"]

    events = load_news(tmp_path / "news.toml")
    assert [e.what for e in events] == ["thin book into the close", "no change"]
    # A declared source fills a blank column; a row that names its own is left alone.
    assert [e.source for e in events] == ["the desk", "central bank"]


def test_a_news_declaration_says_what_is_wrong_with_it(tmp_path: Path) -> None:
    (tmp_path / "news.toml").write_text("[news]\nfile = 'x.csv'\n", encoding="utf-8")
    with pytest.raises(SeriesError, match=r"\[\[news\]\] entry naming a file"):
        load_news_map(tmp_path / "news.toml")

    (tmp_path / "news.toml").write_text('[[news]]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(SeriesError, match="news entry 1 names its file"):
        load_news_map(tmp_path / "news.toml")

    (tmp_path / "news.toml").write_text('[[news]]\nfile = "gone.csv"\n', encoding="utf-8")
    with pytest.raises(SeriesError, match="does not exist"):
        load_news(tmp_path / "news.toml")


def test_the_example_shelf_is_a_form_nobody_has_filled_in_yet() -> None:
    shelf = EXAMPLES / "sources"
    assert {path.name for path in shelf.glob("*.toml")} == {
        "macro.toml",
        "onchain.toml",
        "derivatives.toml",
        "sentiment.toml",
        "news.toml",
    }
    for path in shelf.glob("*.toml"):
        text = path.read_text(encoding="utf-8")
        assert "[[" not in text.replace("# [[", ""), f"{path.name} declares an entry out of the box"
        # A fetch is shown as a commented example; out of the box nothing reaches a network.
        live = [line for line in text.splitlines() if line.strip().startswith("fetch")]
        assert not live, f"{path.name} switches a fetch on out of the box"
