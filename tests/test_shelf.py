"""A fetched series onto the shelf in one step, with lines drawn from its own history."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.data.series import load_series_map
from trading_research_agents.data.shelf import ShelfError, add_series

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def fred(values: list[float]) -> Any:
    """FRED's own answer shape, newest first, one value a day ending yesterday."""
    rows = [
        {"date": (NOW - timedelta(days=i + 1)).strftime("%Y-%m-%d"), "value": str(v)}
        for i, v in enumerate(reversed(values))
    ]

    def get(url: str, headers: Mapping[str, str]) -> bytes:
        return json.dumps({"observations": rows}).encode()

    return get


def test_a_series_is_pulled_and_declared_with_lines_from_its_own_history(tmp_path: Path) -> None:
    added = add_series(
        tmp_path,
        "macro",
        "us10y",
        {"from": "fred", "id": "DGS10"},
        high="bearish",
        band=25,
        days=365,
        now=NOW,
        env={"FRED_API_KEY": "k" * 32},
        get=fred([float(v) for v in range(1, 101)]),
    )
    # 1..100: the 25th percentile is 25.75 and the 75th 75.25, at four significant figures.
    assert added.lower == Decimal("25.75") and added.upper == Decimal("75.25")
    [spec] = load_series_map(tmp_path / "sources" / "macro.toml")
    assert spec.threshold.bearish_above == Decimal("75.25")
    assert spec.threshold.bullish_below == Decimal("25.75")
    assert spec.fetch == {"from": "fred", "id": "DGS10"} and spec.max_age == timedelta(days=5)
    assert (tmp_path / "sources" / "macro" / "us10y.csv").is_file()
    assert "25th and 75th percentiles" in added.block


def test_which_side_is_good_flips_the_lines_and_an_asset_is_kept(tmp_path: Path) -> None:
    add_series(
        tmp_path,
        "onchain",
        "flow",
        {"from": "fred", "id": "X"},
        high="bullish",
        band=10,
        asset="eth",
        now=NOW,
        env={"FRED_API_KEY": "k" * 32},
        get=fred([float(v) for v in range(1, 101)]),
    )
    [spec] = load_series_map(tmp_path / "sources" / "onchain.toml")
    assert spec.threshold.bullish_above is not None and spec.threshold.bearish_below is not None
    assert spec.asset == "ETH"


def test_what_cannot_be_added_says_why_and_leaves_the_shelf_alone(tmp_path: Path) -> None:
    key = {"FRED_API_KEY": "k" * 32}
    with pytest.raises(ShelfError, match="bullish or bearish"):
        add_series(tmp_path, "macro", "a", {"from": "fred", "id": "X"}, high="up", now=NOW)
    with pytest.raises(ShelfError, match="too few"):
        add_series(
            tmp_path,
            "macro",
            "a",
            {"from": "fred", "id": "X"},
            high="bullish",
            now=NOW,
            env=key,
            get=fred([1.0, 2.0, 3.0]),
        )
    add_series(
        tmp_path,
        "macro",
        "b",
        {"from": "fred", "id": "X"},
        high="bullish",
        now=NOW,
        env=key,
        get=fred([float(v) for v in range(40)]),
    )
    with pytest.raises(ShelfError, match="already declares 'b'"):
        add_series(
            tmp_path,
            "macro",
            "b",
            {"from": "fred", "id": "X"},
            high="bullish",
            now=NOW,
            env=key,
            get=fred([float(v) for v in range(40)]),
        )
    assert len(load_series_map(tmp_path / "sources" / "macro.toml")) == 1
