from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.cli import market
from trading_research_agents.cli.main import HANDLERS, build_parser
from trading_research_agents.data.parquet import write_trades
from trading_research_agents.data.schema import Side, Trade
from trading_research_agents.market_data.archive import DERIV, FLOW, day_file
from trading_research_agents.market_data.archive_deriv import read_deriv_day
from trading_research_agents.market_data.days import NS_PER_S, midnight_ns

pytest.importorskip("pyarrow")


class Venue:
    # Spelled as ccxt spells it: the budget reads the client's own attribute.
    rateLimit = 20  # noqa: N815
    has = {"fetchFundingRateHistory": True}
    markets_by_id = {"BTCUSDT": [{"symbol": "BTC/USDT:USDT", "swap": True, "linear": True}]}
    markets = {"BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "swap": True, "linear": True}}

    def load_markets(self) -> dict[str, Any]:
        return self.markets

    def fetch_funding_rate_history(
        self, symbol: str, since: int | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        start = midnight_ns(date(2025, 7, 1)) // 1_000_000
        rows = [
            {"timestamp": start + h * 3_600_000, "fundingRate": 0.0001}
            for h in (0, 8, 16, 24, 32, 40)
        ]
        return [r for r in rows if r["timestamp"] >= (since or 0)][: limit or 6]


def fake_fetch(out: Path, refuse: set[str] | None = None) -> Any:
    def fetch(args: argparse.Namespace) -> int:
        if refuse and args.when in refuse:
            raise ValueError("HTTP 404 from the archive")
        start = midnight_ns(date.fromisoformat(args.when))
        trades = [
            Trade(
                ts_ns=start + 5 * NS_PER_S,
                venue="bybit",
                instrument="BTCUSDT",
                price=Decimal("93530.10"),
                qty=Decimal("0.010"),
                side=Side.BUY,
                trade_id="a",
            ),
            Trade(
                ts_ns=start + 65 * NS_PER_S,
                venue="bybit",
                instrument="BTCUSDT",
                price=Decimal("93531.20"),
                qty=Decimal("0.020"),
                side=Side.SELL,
                trade_id="b",
            ),
        ]
        write_trades(trades, out / f"BTCUSDT-{args.when}.parquet", price_scale=2, qty_scale=3)
        print("ready: fake archive")
        return 0

    return fetch


def args_for(out: Path, *extra: str) -> argparse.Namespace:
    return build_parser().parse_args(
        ["data", "bars", "BTCUSDT", "2025-07-01", "--venue", "bybit", "--out", str(out), *extra]
    )


def test_the_command_is_wired() -> None:
    assert ("data", "bars") in HANDLERS
    parsed = build_parser().parse_args(["runs", "verify", "--market", "data"])
    assert parsed.market == Path("data")


def test_a_day_is_built_with_its_derivatives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(market, "build", lambda venue, config: Venue())
    payload, _, code = market.data_bars(args_for(tmp_path), fetch=fake_fetch(tmp_path))
    assert code == 0 and payload["built"] == ["2025-07-01"]
    assert day_file(tmp_path, "bybit", "BTCUSDT", FLOW, date(2025, 7, 1)).is_file()
    funding = read_deriv_day(tmp_path, "bybit", "BTCUSDT", date(2025, 7, 1))
    assert len(funding) == 3
    assert payload["derivatives"]["funding"].startswith("3 point(s)")
    assert payload["derivatives"]["index"] == "unsupported by the venue"
    assert day_file(tmp_path, "bybit", "BTCUSDT", DERIV, date(2025, 7, 1)).is_file()


def test_a_rerun_keeps_valid_days_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(market, "build", lambda venue, config: Venue())
    market.data_bars(args_for(tmp_path, "--no-deriv"), fetch=fake_fetch(tmp_path))
    path = day_file(tmp_path, "bybit", "BTCUSDT", FLOW, date(2025, 7, 1))
    first = path.read_bytes()
    payload, _, code = market.data_bars(
        args_for(tmp_path, "--no-deriv"), fetch=fake_fetch(tmp_path)
    )
    assert code == 0 and payload["kept"] == ["2025-07-01"] and payload["built"] == []
    assert path.read_bytes() == first


def test_a_failed_day_is_named_and_the_rest_are_built(tmp_path: Path) -> None:
    payload, lines, code = market.data_bars(
        args_for(tmp_path, "--to", "2025-07-02", "--no-deriv"),
        fetch=fake_fetch(tmp_path, refuse={"2025-07-01"}),
    )
    assert code == 1 and payload["built"] == ["2025-07-02"]
    assert "HTTP 404" in payload["failed"]["2025-07-01"]
    assert any("2025-07-01" in line and "HTTP 404" in line for line in lines)


def test_a_range_too_long_or_backwards_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="before"):
        market.data_bars(args_for(tmp_path, "--to", "2025-06-30"), fetch=fake_fetch(tmp_path))
    with pytest.raises(ValueError, match="366"):
        market.data_bars(args_for(tmp_path, "--to", "2026-07-02"), fetch=fake_fetch(tmp_path))


def test_verify_market_names_an_edited_day(tmp_path: Path) -> None:
    market.data_bars(args_for(tmp_path, "--no-deriv"), fetch=fake_fetch(tmp_path))
    payload, lines, code = market.verify_market(tmp_path)
    assert code == 0 and payload["days"] == 1
    path = day_file(tmp_path, "bybit", "BTCUSDT", FLOW, date(2025, 7, 1))
    path.write_bytes(path.read_bytes() + b"x")
    payload, lines, code = market.verify_market(tmp_path)
    assert code == 1 and any("differs" in line for line in lines)


class Refusing(Venue):
    def fetch_funding_rate_history(
        self, symbol: str, since: int | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        raise ConnectionError("reset by peer")


def test_a_failed_series_writes_no_derivatives_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(market, "build", lambda venue, config: Refusing())
    payload, lines, code = market.data_bars(args_for(tmp_path), fetch=fake_fetch(tmp_path))
    assert code == 1 and payload["derivatives"]["funding"].startswith("failed")
    assert not day_file(tmp_path, "bybit", "BTCUSDT", DERIV, date(2025, 7, 1)).exists()
    assert any("reset by peer" in line for line in lines)


def test_a_rerun_keeps_a_complete_derivatives_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def counted(venue: str, config: Any) -> Venue:
        asked.append(venue)
        return Venue()

    monkeypatch.setattr(market, "build", counted)
    market.data_bars(args_for(tmp_path), fetch=fake_fetch(tmp_path))
    path = day_file(tmp_path, "bybit", "BTCUSDT", DERIV, date(2025, 7, 1))
    first = path.read_bytes()
    payload, _, code = market.data_bars(args_for(tmp_path), fetch=fake_fetch(tmp_path))
    assert code == 0 and path.read_bytes() == first and len(asked) == 1
    assert payload["derivatives"] == {"kept": "1 day(s) already complete"}
