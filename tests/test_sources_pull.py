"""Where the reading analysts' figures come from, how fresh they are, and refreshing them."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_research_agents.data.pull import PullError, describe, due, plan, pull, where_from
from trading_research_agents.data.series import load_all, load_series_map, threshold_state
from trading_research_agents.envelope.model import Envelope

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
NOW_NS = int(NOW.timestamp() * 1_000_000_000)


def declare(tmp_path: Path, *extra: str, rows: str = "") -> Path:
    if rows:
        (tmp_path / "s.csv").write_text("date,value\n" + rows, encoding="utf-8")
    path = tmp_path / "macro.toml"
    path.write_text(
        "\n".join(
            [
                "[[series]]",
                'name = "us10y"',
                'file = "s.csv"',
                'ts = "date"',
                'ts_unit = "iso8601"',
                'value = "value"',
                'source = "FRED, DGS10"',
                "threshold = { bullish_below = 4.05, bearish_above = 4.25 }",
                *extra,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_a_value_past_its_max_age_is_shown_and_never_counted(tmp_path: Path) -> None:
    path = declare(tmp_path, 'max_age = "2d"', rows="2026-09-26,4.30\n2026-10-04,4.30\n")
    fresh = threshold_state(load_all(path)[0], NOW_NS)
    assert fresh["state"] == "bearish" and fresh["age_h"] == 36.0
    assert fresh["origin"] == "a file kept by hand"

    (tmp_path / "s.csv").write_text("date,value\n2026-09-26,4.30\n", encoding="utf-8")
    old = threshold_state(load_all(path)[0], NOW_NS)
    assert old["state"] == "stale" and old["stale"] is True and "strength" not in old
    assert old["max_age"] == "2d"


def test_the_default_max_age_is_four_days(tmp_path: Path) -> None:
    spec = load_series_map(declare(tmp_path))[0]
    assert spec.max_age == timedelta(days=4) and spec.fetch is None


class Vendor:
    """An endpoint that answers from a script and remembers what it was asked."""

    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.asked: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: Mapping[str, str]) -> bytes:
        self.asked.append((url, dict(headers)))
        if isinstance(self.answer, Exception):
            raise self.answer
        return json.dumps(self.answer).encode()


def test_fred_is_pulled_with_its_key_merged_and_a_missing_day_skipped(tmp_path: Path) -> None:
    path = declare(tmp_path, 'fetch = { from = "fred", id = "DGS10" }', rows="2026-10-01,4.10\n")
    spec = load_series_map(path)[0]
    vendor = Vendor(
        {
            "observations": [
                {"date": "2026-10-01", "value": "4.12"},
                {"date": "2026-10-02", "value": "."},
                {"date": "2026-10-03", "value": "4.20"},
            ]
        }
    )
    got = pull(spec, tmp_path, env={"FRED_API_KEY": "k" * 32}, get=vendor)
    assert got.added == 1 and got.rows == 2 and str(got.last).startswith("2026-10-03")
    url = vendor.asked[0][0]
    assert url.startswith("https://api.stlouisfed.org/") and "series_id=DGS10" in url
    assert "api_key=" + "k" * 32 in url
    series = load_all(path)[0]
    assert [str(p.value) for p in series.points] == ["4.12", "4.20"]
    assert where_from(spec) == "fetched from fred DGS10 every 1h"


def test_a_paid_vendor_is_one_http_line_with_its_key_in_a_header(tmp_path: Path) -> None:
    path = declare(
        tmp_path,
        'fetch = { from = "http", url = "https://api.example.com/v1/flow", rows = "data", '
        'ts = "t", ts_unit = "s", value = "v", key_env = "VENDOR_KEY", '
        'key_header = "Authorization", key_prefix = "Bearer " }',
    )
    vendor = Vendor({"data": [{"t": 1_791_072_000, "v": 1500}, {"t": "x", "v": 1}]})
    got = pull(load_series_map(path)[0], tmp_path, env={"VENDOR_KEY": "secret-token"}, get=vendor)
    assert got.rows == 1
    url, headers = vendor.asked[0]
    assert "secret-token" not in url and headers["Authorization"] == "Bearer secret-token"


def test_a_pull_that_fails_names_why_and_never_shows_the_key(tmp_path: Path) -> None:
    path = declare(tmp_path, 'fetch = { from = "fred", id = "DGS10" }', rows="2026-10-01,4.1\n")
    spec = load_series_map(path)[0]
    with pytest.raises(PullError, match="FRED_API_KEY is not in the environment"):
        pull(spec, tmp_path, env={}, get=Vendor({}))
    leaky = Vendor(OSError("refused for https://api.stlouisfed.org/?api_key=" + "k" * 32))
    with pytest.raises(PullError) as caught:
        pull(spec, tmp_path, env={"FRED_API_KEY": "k" * 32}, get=leaky)
    assert "k" * 32 not in str(caught.value) and "***" in str(caught.value)
    with pytest.raises(PullError, match="no row carried"):
        pull(spec, tmp_path, env={"FRED_API_KEY": "k" * 32}, get=Vendor({"observations": []}))
    assert (tmp_path / "s.csv").read_text(encoding="utf-8") == "date,value\n2026-10-01,4.1\n"


def test_a_fetch_is_checked_before_anything_is_requested() -> None:
    with pytest.raises(PullError, match="https"):
        plan({"from": "http", "url": "http://plain.example.com/x"}, "s")
    with pytest.raises(PullError, match="needs id"):
        plan({"from": "fred"}, "s")
    with pytest.raises(PullError, match=r"fetch.from is"):
        plan({"from": "somewhere"}, "s")
    with pytest.raises(PullError, match="unknown"):
        plan({"from": "alternative.me", "secret": "x"}, "s")


def test_describe_and_due_say_what_a_cycle_would_count(tmp_path: Path) -> None:
    path = declare(
        tmp_path,
        'max_age = "2d"',
        'fetch = { from = "alternative.me" }',
        'refresh = "1h"',
        rows="2026-09-26,4.3\n",
    )
    spec = load_series_map(path)[0]
    seen = describe(spec, tmp_path, NOW)
    assert seen["counted"] is False and seen["from"] == "fetched from alternative.me every 1h"
    assert due(spec, tmp_path, datetime.now(UTC) + timedelta(hours=2))
    assert not due(spec, tmp_path, datetime.now(UTC))
    (tmp_path / "s.csv").unlink()
    assert due(spec, tmp_path, NOW)
    assert describe(spec, tmp_path, NOW)["counted"] is False


def test_the_cycle_names_a_stale_series_and_skips_a_role_with_nothing_current(
    tmp_path: Path, envelope: Envelope
) -> None:
    from test_cycle import analyst_answer, cycle_config
    from trading_research_agents.agents.cycle import run_cycle

    path = declare(tmp_path, 'max_age = "2d"', rows="2020-01-01,4.30\n")
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {"technical_analyst": analyst_answer()},
        series={"macro_analyst": (path,)},
        analysts=("technical_analyst", "macro_analyst"),
    )
    result = run_cycle(cfg)
    assert any("us10y: last value 2020-01-01" in r for r in result.refusals)
    assert any("every series is past its max_age; not asked" in r for r in result.refusals)
    assert result.record()["series"][0]["state"] == "stale"


def test_coin_metrics_nets_two_metrics_into_one_flow(tmp_path: Path) -> None:
    path = declare(
        tmp_path, 'fetch = { from = "coinmetrics", id = "FlowInExNtv", minus = "FlowOutExNtv" }'
    )
    vendor = Vendor(
        {
            "data": [
                {
                    "asset": "btc",
                    "time": "2026-10-03T00:00:00.000000000Z",
                    "FlowInExNtv": "11665.5",
                    "FlowOutExNtv": "16605.9",
                },
                {"asset": "btc", "time": "2026-10-04T00:00:00.000000000Z", "FlowInExNtv": "9000"},
            ]
        }
    )
    got = pull(load_series_map(path)[0], tmp_path, get=vendor)
    assert got.rows == 1 and str(got.last).startswith("2026-10-03")
    assert "metrics=FlowInExNtv,FlowOutExNtv" in vendor.asked[0][0]
    assert "assets=btc" in vendor.asked[0][0]
    assert str(load_all(path)[0].points[0].value) == "-4940.4"


def test_blockchain_charts_read_their_x_and_y(tmp_path: Path) -> None:
    path = declare(tmp_path, 'fetch = { from = "blockchain.info", id = "n-unique-addresses" }')
    vendor = Vendor({"status": "ok", "values": [{"x": 1790726400, "y": 535674.0}]})
    assert pull(load_series_map(path)[0], tmp_path, get=vendor).rows == 1
    assert vendor.asked[0][0].startswith("https://api.blockchain.info/charts/n-unique-addresses?")


def test_an_analyst_nobody_declared_data_for_is_shown_as_not_asked(
    tmp_path: Path, envelope: Envelope
) -> None:
    from rich.console import Console

    from test_cycle import analyst_answer, cycle_config
    from trading_research_agents.agents.cycle import run_cycle
    from trading_research_agents.cli.tui import cycle_view

    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer()])
    record = run_cycle(cfg).record()
    assert record["not_asked"]["onchain_analyst"] == (
        "nothing declared in sources/onchain.toml or sources/derivatives.toml, so not asked"
    )
    console = Console(record=True, width=200)
    console.print(cycle_view(record))
    assert "not asked" in console.export_text()
