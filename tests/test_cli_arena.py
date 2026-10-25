from __future__ import annotations

import argparse
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.cli.arena import arena_report
from trading_research_agents.cli.main import HANDLERS, build_parser
from trading_research_agents.envelope.model import EnvelopeError
from trading_research_agents.market_data.archive import SourceFile, write_deriv_day, write_flow_day
from trading_research_agents.market_data.bars import FlowBar, GapState
from trading_research_agents.market_data.days import NS_PER_MINUTE, midnight_ns
from trading_research_agents.market_data.deriv import DerivPoint, Series

pytest.importorskip("pyarrow")

FIRST = date(2025, 7, 1)
HOUR = 60 * NS_PER_MINUTE
ENVELOPE = """
[[channel]]
venue = "bybit"
name = "taker-taker"
entry_fee_bps = 5.5
exit_fee_bps = 5.5

[[channel]]
venue = "bybit"
name = "maker-taker"
entry_fee_bps = 2.0
exit_fee_bps = 5.5
"""


def day_of_minutes(day: date, price: float, rng: random.Random) -> tuple[list[FlowBar], float]:
    start = midnight_ns(day)
    out = []
    for i in range(1440):
        before = price
        price = max(1.0, price + rng.choice((-0.03, -0.01, 0.01, 0.03)))
        o, c = Decimal(f"{before:.2f}"), Decimal(f"{price:.2f}")
        out.append(
            FlowBar(
                venue="bybit",
                instrument="BTCUSDT",
                start_ns=start + i * NS_PER_MINUTE,
                end_ns=start + (i + 1) * NS_PER_MINUTE,
                open=o,
                high=max(o, c) + Decimal("0.01"),
                low=min(o, c) - Decimal("0.01"),
                close=c,
                volume_base=Decimal("1.000"),
                volume_quote=Decimal("1.000") * c,
                taker_buy_base=Decimal("0.500"),
                taker_sell_base=Decimal("0.500"),
                trade_count=2,
                large_trade_base=None,
                late_trades=0,
                gap=GapState.NONE,
                source="archive:bybit",
            )
        )
    return out, price


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("archive")
    rng = random.Random(9)  # noqa: S311
    price = 100.0
    for k in range(7):
        day = FIRST + timedelta(days=k)
        bars, price = day_of_minutes(day, price, rng)
        source = (SourceFile("fixture", None),)
        write_flow_day(bars, root, day=day, price_scale=2, qty_scale=3, sources=source)
        start = midnight_ns(day)
        points = [
            DerivPoint(Series.FUNDING, start + h * HOUR, start + h * HOUR, Decimal("0.0001"))
            for h in (0, 8, 16)
        ]
        write_deriv_day(points, root, venue="bybit", instrument="BTCUSDT", day=day, sources=source)
    return root


def args_for(root: Path, tmp: Path, *extra: str, envelope: str = ENVELOPE) -> argparse.Namespace:
    path = tmp / "envelope.toml"
    path.write_text(envelope, encoding="utf-8")
    return build_parser().parse_args(
        [
            "arena",
            "--instruments",
            "BTCUSDT",
            "--from",
            "2025-07-05",
            "--to",
            "2025-07-05",
            "--venue",
            "bybit",
            "--data",
            str(root),
            "--envelope",
            str(path),
            "--out",
            str(tmp / "out"),
            *extra,
        ]
    )


def test_the_command_is_wired() -> None:
    assert ("arena", "arena") in HANDLERS
    assert build_parser().parse_args(["arena", "--instruments", "BTCUSDT"]).instruments == "BTCUSDT"


def test_undeclared_costs_are_named(archive: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="--spread-bps"):
        arena_report(args_for(archive, tmp_path))


def test_a_run_writes_its_record_and_says_when_the_sample_is_small(
    archive: Path, tmp_path: Path
) -> None:
    args = args_for(
        archive, tmp_path, "--spread-bps", "1", "--slippage-bps", "0", "--require-verdict"
    )
    payload, lines, code = arena_report(args)
    assert code == 1
    assert (Path(payload["written_to"]) / "arena.json").is_file()
    assert payload["config"]["costs"]["channel"] == "bybit/taker-taker"
    assert any("sample too small" in line for line in lines)


def test_arena_toml_supplies_the_settings_and_refuses_unknown_keys(
    archive: Path, tmp_path: Path
) -> None:
    envelope = tmp_path / "envelope.toml"
    envelope.write_text(ENVELOPE, encoding="utf-8")
    body = (
        "[arena]\n"
        'instruments = ["BTCUSDT"]\n'
        "from = 2025-07-05\n"
        "to = 2025-07-05\n"
        'venue = "bybit"\n'
        "spread_bps = 1\n"
        "slippage_bps = 0\n"
        f'data = "{archive.as_posix()}"\n'
        f'envelope = "{envelope.as_posix()}"\n'
        f'out = "{(tmp_path / "out").as_posix()}"\n'
    )
    good = tmp_path / "arena.toml"
    good.write_text(body, encoding="utf-8")
    _, _, code = arena_report(build_parser().parse_args(["arena", "--config", str(good)]))
    assert code == 0
    bad = tmp_path / "bad.toml"
    bad.write_text(body.replace("spread_bps", "spred_bps"), encoding="utf-8")
    with pytest.raises(ValueError, match="did you mean spread_bps"):
        arena_report(build_parser().parse_args(["arena", "--config", str(bad)]))


def test_a_venue_without_a_taker_channel_must_name_one(archive: Path, tmp_path: Path) -> None:
    makers = ENVELOPE.split("[[channel]]")[2]
    args = args_for(
        archive,
        tmp_path,
        "--spread-bps",
        "1",
        "--slippage-bps",
        "0",
        envelope="[[channel]]" + makers,
    )
    with pytest.raises(EnvelopeError, match="name the channel"):
        arena_report(args)
