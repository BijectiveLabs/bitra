"""The analysts that only read: what they are handed, and what they say when handed nothing."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.agents import macro_analyst, onchain_analyst, sentiment_analyst
from trading_research_agents.agents.briefs import BriefError, context_brief, reference_returns
from trading_research_agents.agents.sentiment_analyst.tools import sentiment_brief
from trading_research_agents.data.schema import Candle
from trading_research_agents.data.series import load_all, read_events
from trading_research_agents.data.sources import Fetched
from trading_research_agents.store.snapshot import Snapshot

NOW = 1_800_000_000.0
MINUTE_NS = 60 * 1_000_000_000
AS_OF_NS = int(NOW * 1_000_000_000)


def snapshot(candles: int = 6) -> Snapshot:
    snap = Snapshot(cycle="2026-09-27T10-00-00Z", instrument="BTCUSDT")
    start = AS_OF_NS - candles * MINUTE_NS
    rows = [
        Candle(
            start_ns=start + i * MINUTE_NS,
            open=Decimal(100 + i),
            high=Decimal(100 + i),
            low=Decimal(100 + i),
            close=Decimal(100 + i),
            volume=Decimal("5"),
            trades=3,
        )
        for i in range(candles)
    ]
    snap.put(
        Fetched(
            kind="ohlcv",
            instrument="BTCUSDT",
            value=rows,
            source="file:data",
            tier="free",
            fetched_at=NOW,
            as_of_ns=rows[-1].start_ns + MINUTE_NS,
            detail={"candles": len(rows)},
        )
    )
    return snap


def series_file(tmp_path: Path, name: str, values: list[tuple[str, str]], **extra: Any) -> Path:
    """A series the way an operator declares one: a CSV they own and a mapping they wrote."""
    csv = tmp_path / f"{name}.csv"
    csv.write_text(
        "date,value\n" + "".join(f"{when},{value}\n" for when, value in values), encoding="utf-8"
    )
    lines = [
        "[[series]]",
        f'name = "{name}"',
        f'file = "{name}.csv"',
        'ts = "date"',
        'ts_unit = "iso8601"',
        'value = "value"',
        'unit = "index"',
        'publish_lag = "0s"',
        'source = "operator export"',
    ]
    # The fixtures' dates sit months before the test clock; freshness has its own tests.
    extra.setdefault("max_age", '"365d"')
    for key, value in extra.items():
        lines.append(f"{key} = {value}")
    path = tmp_path / f"{name}.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_the_sentiment_brief_says_when_nothing_was_declared() -> None:
    empty = sentiment_brief((), at_ns=AS_OF_NS)
    assert empty["declared"] is False and empty["series"] == [] and empty["notes"] == []
    assert "nothing" in empty["basis"] and empty["as_of"].endswith("+00:00")


def test_the_sentiment_brief_carries_change_zscore_and_the_operator_threshold(
    tmp_path: Path,
) -> None:
    when = datetime.fromtimestamp(AS_OF_NS / 1e9, tz=UTC)
    days = [
        ((when.replace(hour=0, minute=0, second=0, microsecond=0)).replace(day=when.day), "70"),
    ]
    history = [
        (f"2026-09-{day:02d}", value)
        for day, value in ((10, "20"), (12, "35"), (14, "40"), (16, "55"), (18, "70"))
    ]
    path = series_file(
        tmp_path,
        "fear_greed",
        history,
        threshold="{ bullish_above = 60, bearish_below = 25 }",
    )
    del days
    series = load_all(path)
    brief = sentiment_brief(series, at_ns=AS_OF_NS)
    row = brief["series"][0]
    assert brief["declared"] is True and row["series"] == "fear_greed"
    assert row["value"] == "70" and row["state"] == "bullish"
    assert row["basis"] == "operator-declared threshold"
    assert row["zscore_vs_recent"]["n"] == 5
    assert row["points_known"] == 5

    notes = tmp_path / "notes.csv"
    notes.write_text(
        "when,what\n"
        f"{datetime.fromtimestamp((AS_OF_NS - MINUTE_NS) / 1e9, tz=UTC).isoformat()},a dated note\n"
        "2020-01-01T00:00:00+00:00,older than the window\n",
        encoding="utf-8",
    )
    with_notes = sentiment_brief(series, at_ns=AS_OF_NS, notes=read_events(notes))
    assert [n["what"] for n in with_notes["notes"]] == ["a dated note"]


def test_a_context_brief_is_built_for_each_reading_role(tmp_path: Path) -> None:
    path = series_file(tmp_path, "us10y", [("2026-09-20", "4.1"), ("2026-09-25", "4.4")])
    series = load_all(path)
    snap = snapshot()
    times, returns = reference_returns(snap)
    assert len(times) == 6 and len(returns) == 6 and returns[0] == 0.0

    for role in (macro_analyst.ROLE, onchain_analyst.ROLE, sentiment_analyst.ROLE):
        brief = context_brief(role, snap, series=series)
        assert brief["instrument"] == "BTCUSDT" and brief["declared_series"] == ["us10y"]
        assert brief["series"][0]["value"] == "4.4"
        # The correlation is measured on the operator's own closes, and carries its n.
        measured = brief["series"][0]["correlation_measured"]
        assert measured is None or "n" in measured

    with pytest.raises(BriefError, match="not a reading analyst"):
        context_brief("technical_analyst", snap)
    with pytest.raises(BriefError, match="snapshot is empty"):
        context_brief(macro_analyst.ROLE, Snapshot(cycle="c", instrument="BTCUSDT"))


def test_a_role_may_run_on_its_own_model(tmp_path: Path) -> None:
    from trading_research_agents.trading import load_strategy_config
    from trading_research_agents.trading.config import ConfigError

    examples = Path(__file__).resolve().parent.parent / "examples"
    text = (examples / "strategy.toml").read_text("utf-8")
    start, end = text.index("[strategy]"), text.index("[exit]")
    base = (
        text[:start]
        + '[strategy]\nagent = "technical_analyst"\n\n'
        + text[end:]
        + "\n[agents]\n"
        + 'provider = "groq"\nmodel = "strong-model"\nmechanism = "reversion after a burst"\n'
        + "token_budget = 100000\nmax_calls_per_hour = 100\nmin_interval_s = 0\n"
        + "max_output_tokens = 1024\n"
    )
    path = tmp_path / "strategy.toml"
    path.write_text(
        base + '\n[agents.roles.validator]\nmodel = "cheap-model"\nmax_output_tokens = 256\n',
        "utf-8",
    )
    cfg = load_strategy_config(path)
    assert cfg.agents is not None
    strong = cfg.agents.for_role("technical_analyst")
    cheap = cfg.agents.for_role("validator")
    assert strong.model == "strong-model" and strong.max_output_tokens == 1024
    assert cheap.model == "cheap-model" and cheap.max_output_tokens == 256
    # Anything a role does not override it inherits, provider included.
    assert cheap.provider == "groq" and cheap.call_timeout_s == strong.call_timeout_s

    path.write_text(base + '\n[agents.roles.nobody]\nmodel = "x"\n', "utf-8")
    with pytest.raises(ConfigError, match="is not a role"):
        load_strategy_config(path)
    path.write_text(base + "\n[agents.roles.executor]\ntemperature = 0.3\n", "utf-8")
    with pytest.raises(ConfigError, match="is not settable"):
        load_strategy_config(path)


def test_the_roster_can_be_named_in_the_config(tmp_path: Path) -> None:
    from trading_research_agents.trading import load_strategy_config
    from trading_research_agents.trading.config import ConfigError

    examples = Path(__file__).resolve().parent.parent / "examples"
    text = (examples / "strategy.toml").read_text("utf-8")
    start, end = text.index("[strategy]"), text.index("[exit]")
    base = (
        text[:start]
        + '[strategy]\nagent = "technical_analyst"\n\n'
        + text[end:]
        + "\n[agents]\n"
        + 'provider = "groq"\nmodel = "m"\nmechanism = "reversion after a burst of prints"\n'
        + "token_budget = 100000\nmax_calls_per_hour = 100\nmin_interval_s = 0\n"
    )
    path = tmp_path / "strategy.toml"
    path.write_text(
        base + 'analysts = ["technical_analyst", "sentiment_analyst"]\n',
        "utf-8",
    )
    cfg = load_strategy_config(path)
    assert cfg.agents is not None
    assert cfg.agents.analysts == ("technical_analyst", "sentiment_analyst")

    # Left out, the roster is decided by which sources are there, not by this field.
    path.write_text(base, "utf-8")
    assert load_strategy_config(path).agents.analysts == ()  # type: ignore[union-attr]

    for body, message in (
        ('analysts = ["macro_analyst"]\n', "it is the role that decides"),
        ('analysts = ["technical_analyst", "poet"]\n', "cannot be asked"),
        ('analysts = "technical_analyst"\n', "a list of role names"),
        ('analysts = ["technical_analyst", "technical_analyst"]\n', "names a role twice"),
    ):
        path.write_text(base + body, "utf-8")
        with pytest.raises(ConfigError, match=message):
            load_strategy_config(path)


def test_code_reads_the_declared_state_beside_whatever_the_model_says(tmp_path: Path) -> None:
    from trading_research_agents.agents.briefs import declared_state

    path = series_file(
        tmp_path,
        "venue_net_flow",
        [("2026-09-20", "1800"), ("2026-09-25", "2400")],
        threshold="{ bearish_above = 1500, bullish_below = 0 }",
    )
    snap = snapshot()
    brief = context_brief(onchain_analyst.ROLE, snap, series=load_all(path))
    read = declared_state(onchain_analyst.ROLE, brief)
    assert read["declared"] == "bearish" and read["as_role_says_it"] == "distribution"
    assert read["series_declared"] == 1

    # The same declaration, said in each role's own words.
    assert declared_state(macro_analyst.ROLE, brief)["as_role_says_it"] == "risk-off"
    assert declared_state(sentiment_analyst.ROLE, brief)["as_role_says_it"] == "negative"

    # A series with no threshold declares nothing, and that is its own answer.
    plain = context_brief(
        onchain_analyst.ROLE,
        snap,
        series=load_all(series_file(tmp_path, "supply", [("2026-09-20", "10")])),
    )
    assert declared_state(onchain_analyst.ROLE, plain)["declared"] == "undeclared"

    # Two declarations pointing different ways is `mixed`, never quietly one of them.
    both = {
        "series": [
            {"state": "bullish", "basis": "operator-declared threshold"},
            {"state": "bearish", "basis": "operator-declared threshold"},
        ]
    }
    assert declared_state(onchain_analyst.ROLE, both)["declared"] == "mixed"


def test_a_series_about_another_asset_counts_for_less_on_this_one(tmp_path: Path) -> None:
    from trading_research_agents.agents.briefs import base_of, declared_state

    path = series_file(
        tmp_path,
        "btc_flow",
        [("2026-09-20", "1800"), ("2026-09-25", "4000")],
        threshold="{ bearish_above = 1500, bullish_below = 0 }",
        asset='"btc"',
        elsewhere="0.25",
    )
    series = load_all(path)
    assert base_of("ZECUSDT") == "ZEC" and base_of("BTCUSD") == "BTC" and base_of("USDT") == "USDT"
    # Other venues' spellings of the same perpetual.
    assert base_of("BTC-USDT-SWAP") == "BTC" and base_of("BTC/USDC:USDC") == "BTC"
    assert base_of("BTC_USDT") == "BTC" and base_of("ZECUSDC") == "ZEC"

    own = context_brief(onchain_analyst.ROLE, snapshot(), series=series)
    assert own["series"][0]["asset"] == "BTC" and "counts" not in own["series"][0]

    other = snapshot()
    other.instrument = "ZECUSDT"
    away = context_brief(onchain_analyst.ROLE, other, series=series)
    assert away["series"][0]["counts"] == "0.25"
    here = Decimal(declared_state(onchain_analyst.ROLE, own)["score"])
    there = Decimal(declared_state(onchain_analyst.ROLE, away)["score"])
    assert here != 0 and there == (here * Decimal("0.25")).quantize(Decimal("0.001"))


def test_a_threshold_grades_how_far_past_its_line_a_value_sits() -> None:
    from trading_research_agents.data.series import Threshold

    yields = Threshold(bullish_below=Decimal("4.05"), bearish_above=Decimal("4.25"))
    assert yields.graded and yields.band == Decimal("0.20")
    assert yields.strength(Decimal("4.02")) == Decimal("0.150")
    assert yields.strength(Decimal("3.80")) == Decimal("1.000")
    assert yields.strength(Decimal("4.10")) == Decimal(0)
    assert yields.strength(Decimal("4.30")) == Decimal("-0.250")

    # One line only has no band to measure in, so it stays the plain word it always was.
    one = Threshold(bullish_above=Decimal(60))
    assert not one.graded and one.strength(Decimal(62)) == Decimal(1)
    assert one.strength(Decimal(50)) == Decimal(0)


def test_the_declared_state_carries_the_net_graded_score() -> None:
    from trading_research_agents.agents.briefs import declared_state

    rows = {
        "series": [
            {
                "state": "bullish",
                "strength": "0.150",
                "graded": True,
                "basis": "operator-declared threshold",
            },
            {
                "state": "bullish",
                "strength": "0.057",
                "graded": True,
                "basis": "operator-declared threshold",
            },
        ]
    }
    said = declared_state(macro_analyst.ROLE, rows)
    assert said["declared"] == "bullish" and said["score"] == "0.104" and said["graded"] is True

    # Opposite words stay `mixed` for the gate, and the net is what conviction weighs.
    both = {
        "series": [
            {"state": "bullish", "strength": "0.8", "basis": "operator-declared threshold"},
            {"state": "bearish", "strength": "-0.2", "basis": "operator-declared threshold"},
        ]
    }
    mixed = declared_state(onchain_analyst.ROLE, both)
    assert mixed["declared"] == "mixed" and mixed["score"] == "0.300"
