"""One cycle end to end: briefs from code, agents in parallel, the cost gate, one record."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.agents.briefs import (
    BriefError,
    cost_brief,
    data_brief,
    technical_brief,
)
from trading_research_agents.agents.cycle import CycleConfig, CycleError, run_cycle
from trading_research_agents.agents.parallel import FanOut, Task, run_tasks
from trading_research_agents.data.schema import Candle, Direction
from trading_research_agents.data.sources import Fetched, SourceChain, SourceError
from trading_research_agents.envelope import Envelope, load_envelope
from trading_research_agents.execution.pretrade import book_from_levels
from trading_research_agents.llm import BudgetLimits, FakeCompleter, MeteredCompleter, UsageLedger
from trading_research_agents.store.snapshot import Snapshot

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
NOW = 1_800_000_000.0
MINUTE_NS = 60 * 1_000_000_000


@pytest.fixture
def envelope() -> Envelope:
    return load_envelope(EXAMPLES / "envelope.toml")


def candles(closes: list[str], volume: str = "10") -> list[Candle]:
    start = int(NOW * 1_000_000_000) - len(closes) * MINUTE_NS
    out = []
    for i, close in enumerate(closes):
        price = Decimal(close)
        out.append(
            Candle(
                start_ns=start + i * MINUTE_NS,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal(volume),
                trades=5,
            )
        )
    return out


def snapshot_with(
    *, closes: list[str] | None = None, book: bool = True, funding: bool = True
) -> Snapshot:
    snap = Snapshot(cycle="2026-09-27T10-00-00Z", instrument="BTCUSDT")
    rows = candles(closes or ["100", "101", "99", "100", "102"])
    snap.put(
        Fetched(
            kind="ohlcv",
            instrument="BTCUSDT",
            value=rows,
            source="file:data",
            tier="free",
            fetched_at=NOW,
            as_of_ns=rows[-1].start_ns + MINUTE_NS,
            detail={"candles": len(rows), "last_close": str(rows[-1].close)},
        )
    )
    if book:
        snap.put(
            Fetched(
                kind="orderbook",
                instrument="BTCUSDT",
                value=book_from_levels(
                    bids=[("101.95", "5"), ("101.90", "50")],
                    asks=[("102.05", "5"), ("102.10", "50")],
                ),
                source="ccxt:bybit",
                tier="free",
                fetched_at=NOW,
                as_of_ns=int(NOW * 1_000_000_000),
                detail={"spread_bps": "9.80"},
            )
        )
    if funding:
        snap.put(
            Fetched(
                kind="funding",
                instrument="BTCUSDT",
                value=Decimal("0.0001"),
                source="ccxt:bybit",
                tier="free",
                fetched_at=NOW,
                as_of_ns=int(NOW * 1_000_000_000),
                detail={"rate": "0.0001"},
            )
        )
    return snap


class Stub:
    """A source that answers from a prepared snapshot, so a cycle runs without a network."""

    def __init__(self, kind: str, value: Any, *, fail: str = "") -> None:
        self.kind = kind
        self.value = value
        self.fail = fail

    @property
    def name(self) -> str:
        return f"stub:{self.kind}"

    @property
    def tier(self) -> str:
        return "free"

    def kinds(self) -> tuple[str, ...]:
        return (self.kind,)

    def fetch(self, kind: str, instrument: str, **options: Any) -> Fetched:
        if self.fail:
            raise SourceError(self.fail)
        prepared = snapshot_with().get(kind)
        return Fetched(
            kind=kind,
            instrument=instrument,
            value=prepared.value,
            source=self.name,
            tier="free",
            fetched_at=NOW,
            as_of_ns=int(NOW * 1_000_000_000),
            detail=dict(prepared.detail),
        )


def chains(*, without: str = "") -> dict[str, SourceChain]:
    out = {}
    for kind in ("ohlcv", "orderbook", "funding"):
        if kind == without:
            continue
        out[kind] = SourceChain(
            kind=kind, sources=(Stub(kind, None),), ttl_s=600, clock=lambda: NOW
        )
    return out


def analyst_answer(target: str = "long", confidence: float = 0.9, horizon: int = 30) -> str:
    return json.dumps(
        {
            "target": target,
            "reason": "the close sits two standard deviations below its window",
            "confidence": confidence,
            "horizon_bars": horizon,
        }
    )


class Scripted:
    """A scripted model per role, because the analysts are asked at the same time.

    One shared queue cannot say which answer belongs to which role once the calls go out in
    parallel, and that is exactly what the cycle does. So each role gets its own script, the way
    each role can get its own model in a real configuration.
    """

    def __init__(self, answers: list[Any] | dict[str, Any]) -> None:
        self.shared = FakeCompleter(answers) if isinstance(answers, list) else None
        self.per_role = (
            {role: FakeCompleter([answer]) for role, answer in answers.items()}
            if isinstance(answers, dict)
            else {}
        )

    def for_role(self, role: str) -> FakeCompleter:
        if self.shared is not None:
            return self.shared
        if role not in self.per_role:
            raise AssertionError(f"the test scripted no answer for {role}")
        return self.per_role[role]

    @property
    def calls(self) -> list[Any]:
        if self.shared is not None:
            return list(self.shared.calls)
        return [call for fake in self.per_role.values() for call in fake.calls]


def cycle_config(
    tmp_path: Path,
    envelope: Envelope,
    answers: list[Any] | dict[str, Any],
    **overrides: Any,
) -> tuple[CycleConfig, Scripted, UsageLedger]:
    fake = Scripted(answers)
    usage = UsageLedger(tmp_path / "usage.jsonl")
    limits = BudgetLimits(token_budget=500_000, max_calls_per_hour=1000, min_interval_s=0)
    metered: dict[str, MeteredCompleter] = {}

    def completer_for(role: str) -> MeteredCompleter:
        if role not in metered:
            metered[role] = MeteredCompleter(
                fake.for_role(role),
                role=role,
                provider="fake",
                limits=limits,
                ledger=usage,
                clock=lambda: 0.0,
            )
        return metered[role]

    settings: dict[str, Any] = {
        "instrument": "BTCUSDT",
        "envelope": envelope,
        "venue_key": envelope.cheapest(None).key,
        "chains": chains(),
        "completer_for": completer_for,
        "usage": usage,
        "root": tmp_path / "cycles",
        "mechanism": "short-horizon reversion after a burst of prints",
        "size": Decimal("5"),
        "magnitude_bps": Decimal("40"),
        "clock": lambda: datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
        "lookback_bars": 5,
    }
    settings.update(overrides)
    return CycleConfig(**settings), fake, usage


def test_the_briefs_are_computed_by_code_and_name_their_sources(envelope: Envelope) -> None:
    snap = snapshot_with()
    data = data_brief(snap, now=NOW)
    assert set(data.values) == {"ohlcv", "orderbook", "funding"}
    assert data.values["ohlcv"]["source"] == "file:data" and data.refused == {}

    brief = technical_brief(snap, mechanism="reversion", interval_s=60, lookback_bars=5, now=NOW)
    record = brief.record()
    assert record["bars"] == 5 and record["mechanism"] == "reversion"
    assert record["spread_bps"] == "9.80" and record["funding_rate"] == "0.0001"
    assert "zscore" in record and "ret_1_bps" in record and record["age_s"] == 0.0

    costs = cost_brief(
        snap,
        channel=envelope.cheapest(None),
        size=Decimal("5"),
        direction=Direction.LONG,
        horizon_bars=30,
        bar_interval_s=60,
    )
    assert costs.record()["direction"] == "long"
    assert Decimal(costs.record()["round_trip_bps"]) > 0
    assert [c["name"] for c in costs.record()["components"]][:2] == [
        "fee_round_trip",
        "spread_cost",
    ]

    thin = snapshot_with(closes=["100"])
    with pytest.raises(BriefError, match="fewer than two candles"):
        technical_brief(thin, mechanism="m", lookback_bars=5)
    with pytest.raises(BriefError, match="at least 2"):
        technical_brief(snap, mechanism="m", lookback_bars=1)
    with pytest.raises(BriefError, match="no book in the snapshot"):
        cost_brief(
            snapshot_with(book=False),
            channel=envelope.cheapest(None),
            size=Decimal("5"),
            direction=Direction.LONG,
            horizon_bars=30,
        )


def test_tasks_run_together_keep_their_order_and_survive_one_failure(tmp_path: Path) -> None:
    def ok(name: str) -> Any:
        return lambda: {"who": name}

    def boom() -> Any:
        raise SourceError("RateLimitExceeded")

    fan = run_tasks(
        [
            Task("zulu", ok("zulu"), tmp_path / "zulu.json"),
            Task("alpha", ok("alpha"), tmp_path / "alpha.json"),
            Task("mike", boom, tmp_path / "mike.json"),
        ]
    )
    assert [o.name for o in fan.outcomes] == ["alpha", "mike", "zulu"]
    assert set(fan.answers) == {"alpha", "zulu"} and "mike" in fan.failed
    assert "RateLimitExceeded" in fan.failed["mike"] and fan.seconds >= 0
    assert json.loads((tmp_path / "alpha.json").read_text("utf-8")) == {"who": "alpha"}
    assert not (tmp_path / "mike.json").exists()

    # With resume, the answer already on disk is reused and the work is not run again.
    called: list[str] = []

    def again() -> Any:
        called.append("yes")
        return {"who": "fresh"}

    reused = run_tasks([Task("alpha", again, tmp_path / "alpha.json")], resume=True)
    assert called == [] and reused.answers["alpha"] == {"who": "alpha"}
    assert reused.outcomes[0].reused is True

    with pytest.raises(ValueError, match="same agent name"):
        run_tasks([Task("a", ok("a")), Task("a", ok("a"))])
    with pytest.raises(ValueError, match="names its agent"):
        Task("  ", ok("x"))
    assert run_tasks([]).outcomes == [] and FanOut().seconds == 0.0


def test_a_cycle_writes_everything_it_touched_and_admits_a_claim_that_clears(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, fake, usage = cycle_config(tmp_path, envelope, [analyst_answer()])
    result = run_cycle(cfg)
    assert result.stopped_by is None and result.acted
    assert result.analyst is not None and result.analyst.target == "long"
    assert result.verdict is not None and result.verdict.admitted
    assert result.intent is not None and result.intent["size"] == "5"
    assert len(fake.calls) == 1 and usage.tokens > 0

    where = Path(str(result.path))
    assert json.loads((where / "snapshot.json").read_text("utf-8"))["instrument"] == "BTCUSDT"
    brief = json.loads((where / "briefs" / "technical.json").read_text("utf-8"))
    assert brief["bars"] == 5 and brief["mechanism"].startswith("short-horizon")
    assert (where / "briefs" / "cost.json").is_file()
    assert (where / "answers" / "technical.json").is_file()
    intents = (where / "intents.jsonl").read_text("utf-8").strip().splitlines()
    assert len(intents) == 1 and json.loads(intents[0])["target"] == "long"
    summary = json.loads((where / "cycle.json").read_text("utf-8"))
    assert summary["cost"]["admitted"] is True and summary["mode"] == "sandbox"
    assert summary["agents"]["agents"][0]["agent"] == "technical"
    assert summary["tokens"] == usage.tokens


def test_a_cycle_refuses_when_the_claim_does_not_clear_the_cost(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer()], magnitude_bps=Decimal("1"))
    result = run_cycle(cfg)
    assert not result.acted and result.verdict is not None and not result.verdict.admitted
    assert any("cost gate" in r for r in result.refusals)
    # A refused claim is not a stopped cycle: the cycle did its job by refusing.
    assert result.stopped_by is None
    summary = json.loads((Path(str(result.path)) / "cycle.json").read_text("utf-8"))
    assert summary["intent"] is None and "below" in summary["cost"]["reason"]


def test_flat_and_low_confidence_answers_never_reach_the_cost_gate(
    tmp_path: Path, envelope: Envelope
) -> None:
    flat, _, _ = cycle_config(tmp_path / "flat", envelope, [analyst_answer(target="flat")])
    result = run_cycle(flat)
    assert not result.acted and result.verdict is None
    assert any("flat" in r for r in result.refusals)

    unsure, _, _ = cycle_config(
        tmp_path / "unsure", envelope, [analyst_answer(confidence=0.2)], min_confidence=0.6
    )
    result = run_cycle(unsure)
    assert not result.acted and result.verdict is None
    assert any("confidence 0.2 below" in r for r in result.refusals)


def test_a_cycle_without_a_book_prices_nothing_and_says_so(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path, envelope, [analyst_answer()], chains=chains(without="orderbook")
    )
    result = run_cycle(cfg)
    assert not result.acted and result.stopped_by is not None
    assert "no book in the snapshot" in result.stopped_by


def test_a_cycle_with_no_data_stops_before_it_asks_a_model(
    tmp_path: Path, envelope: Envelope
) -> None:
    empty = {
        "ohlcv": SourceChain(
            kind="ohlcv",
            sources=(Stub("ohlcv", None, fail="RateLimitExceeded"),),
            ttl_s=600,
            clock=lambda: NOW,
        )
    }
    cfg, fake, _ = cycle_config(tmp_path, envelope, [analyst_answer()], chains=empty)
    result = run_cycle(cfg)
    assert fake.calls == [] and result.stopped_by is not None
    assert "no technical brief" in result.stopped_by
    assert any("data ohlcv" in r for r in result.refusals)


def test_a_cycle_refuses_to_be_configured_wrong(tmp_path: Path, envelope: Envelope) -> None:
    for overrides, message in (
        ({"instrument": " "}, "names its instrument"),
        ({"mode": "venue"}, "mode is one of"),
        ({"size": Decimal(0)}, "size is a positive"),
        ({"magnitude_bps": Decimal(0)}, "edge the operator claims"),
        ({"venue_key": "nowhere/taker"}, "is not a channel"),
    ):
        with pytest.raises(CycleError, match=message):
            cycle_config(tmp_path, envelope, [analyst_answer()], **overrides)


def test_the_cli_runs_one_cycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("pyarrow")
    from trading_research_agents.cli.main import main
    from trading_research_agents.data import Side, Trade
    from trading_research_agents.data.parquet import write_trades
    from trading_research_agents.llm.providers import Provider, register

    project = tmp_path / "desk"
    assert main(["init", str(project)]) == 0
    monkeypatch.chdir(project)

    # A model as the core, answering from a script, and data from the project's own directory.
    register(
        Provider(
            "scripted-cycle",
            None,
            None,
            True,
            lambda model, base_url: FakeCompleter([analyst_answer()]),
        )
    )
    config = project / "strategy.toml"
    text = config.read_text("utf-8")
    start, end = text.index("[strategy]"), text.index("[exit]")
    text = (
        text[:start]
        + '[strategy]\nagent = "technical_analyst"\n\n'
        + text[end:]
        + '\n[agents]\nprovider = "scripted-cycle"\nmodel = "scripted"\n'
        + 'mechanism = "reversion after a burst"\ntoken_budget = 100000\n'
        + "max_calls_per_hour = 100\nmin_interval_s = 0\nlookback_bars = 5\n"
        + "min_confidence = 0.5\nmagnitude_bps = 40\n"
    )
    config.write_text(text, "utf-8")
    # The offline source serves candles, never a book, so this cycle must stop at the cost
    # brief and say why. A chain naming a source that cannot serve a kind is refused earlier
    # still, by the config itself, which is the other half of the same rule.
    ohlcv_only = "\n".join(
        (
            "[data.ohlcv]",
            'chain = ["file:data"]',
            'ttl = "3650d"',
            "interval_s = 60",
            "limit = 10",
            "",
        )
    )
    (project / "data.toml").write_text(ohlcv_only, "utf-8")
    rows = [
        Trade(
            1_700_000_000_000_000_000 + i * MINUTE_NS,
            "bybit",
            "BTCUSDT",
            Decimal("93000") + Decimal(i),
            Decimal("0.01"),
            Side.BUY,
            str(i),
        )
        for i in range(10)
    ]
    write_trades(rows, project / "data" / "BTCUSDT-2023-11-14.parquet", price_scale=2, qty_scale=3)
    capsys.readouterr()
    code = main(["run", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1 and payload["instrument"] == "BTCUSDT"
    assert payload["stopped_by"] is not None and "no book" in str(payload["stopped_by"])
    assert payload["analyst"]["target"] == "long" and payload["cost"] is None
    assert payload["data"]["values"]["ohlcv"]["source"].startswith("file:")
    assert payload["tokens"] > 0 and Path(payload["out"]).is_dir()

    book_chain = "\n".join(("[data.orderbook]", 'chain = ["file:data"]', 'ttl = "10s"', ""))
    (project / "data.toml").write_text(book_chain, "utf-8")
    assert main(["run"]) == 1
    assert "does not serve orderbook" in capsys.readouterr().err


def reading_answer(role: str) -> str:
    """What each reading analyst answers, in its own schema."""
    if role == "macro_analyst":
        return json.dumps(
            {
                "regime": "risk-off",
                "events": [],
                "reason": "us10y at 4.4 is above the operator's declared 4.25",
                "data_as_of": "2026-09-27T10:00:00+00:00",
            }
        )
    if role == "onchain_analyst":
        return json.dumps(
            {
                "signal": "distribution",
                "reason": "net flow to venues over the window is positive",
                "data_as_of": "2026-09-27T10:00:00+00:00",
            }
        )
    return json.dumps(
        {
            "state": "not declared",
            "reason": "the brief carried no series and no notes",
            "figures_used": [],
            "data_as_of": "2026-09-27T10:00:00+00:00",
        }
    )


def series_for(tmp_path: Path, name: str, value: str, threshold: str = "") -> Path:
    csv = tmp_path / f"{name}.csv"
    csv.write_text(f"date,value\n2026-09-20,{value}\n2026-09-25,{value}\n", encoding="utf-8")
    body = [
        "[[series]]",
        f'name = "{name}"',
        f'file = "{name}.csv"',
        'ts = "date"',
        'ts_unit = "iso8601"',
        'value = "value"',
        'unit = "percent"',
        'publish_lag = "0s"',
        'source = "operator export"',
    ]
    if threshold:
        body.append(f"threshold = {threshold}")
    path = tmp_path / f"{name}.toml"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def test_four_analysts_run_together_and_every_answer_is_recorded(
    tmp_path: Path, envelope: Envelope
) -> None:
    macro = series_for(tmp_path, "us10y", "4.4", "{ bearish_above = 4.25 }")
    onchain = series_for(tmp_path, "venue_net_flow", "120")
    answers = {
        "technical_analyst": analyst_answer(),
        "macro_analyst": reading_answer("macro_analyst"),
        "onchain_analyst": reading_answer("onchain_analyst"),
        "sentiment_analyst": reading_answer("sentiment_analyst"),
    }
    cfg, fake, usage = cycle_config(
        tmp_path,
        envelope,
        answers,
        series={"macro_analyst": macro, "onchain_analyst": onchain},
        analysts=(
            "technical_analyst",
            "macro_analyst",
            "onchain_analyst",
            "sentiment_analyst",
        ),
    )
    result = run_cycle(cfg)
    assert result.stopped_by is None and result.acted
    # Four agents asked, in a fixed order in the record whoever answered first.
    assert [o.name for o in result.fan.outcomes] == [
        "macro_analyst",
        "onchain_analyst",
        "sentiment_analyst",
        "technical",
    ]
    assert len(fake.calls) == 4 and usage.tokens > 0
    assert result.context["macro_analyst"]["regime"] == "risk-off"
    assert result.context["onchain_analyst"]["signal"] == "distribution"
    # A sentiment analyst with no declared source says so, and is never recorded as neutral.
    assert result.context["sentiment_analyst"]["state"] == "not declared"

    where = Path(str(result.path))
    for role in ("macro_analyst", "onchain_analyst", "sentiment_analyst"):
        assert (where / "briefs" / f"{role}.json").is_file()
        assert (where / "answers" / f"{role}.json").is_file()
    summary = json.loads((where / "cycle.json").read_text("utf-8"))
    assert set(summary["context"]) == {"macro_analyst", "onchain_analyst", "sentiment_analyst"}
    macro_brief_read = json.loads((where / "briefs" / "macro_analyst.json").read_text("utf-8"))
    assert macro_brief_read["declared_series"] == ["us10y"]
    assert macro_brief_read["series"][0]["state"] == "bearish"


def test_a_reading_analyst_whose_series_cannot_be_read_is_refused_not_guessed(
    tmp_path: Path, envelope: Envelope
) -> None:
    missing = tmp_path / "nowhere.toml"
    missing.write_text(
        '[[series]]\nname = "x"\nfile = "absent.csv"\nts = "date"\nvalue = "value"\n', "utf-8"
    )
    cfg, fake, _ = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer()],
        series={"onchain_analyst": missing},
        analysts=("technical_analyst", "onchain_analyst"),
    )
    result = run_cycle(cfg)
    assert result.acted and result.context == {}
    assert any("onchain_analyst" in r and "does not exist" in r for r in result.refusals)
    assert len(fake.calls) == 1


def test_a_cycle_refuses_a_roster_it_cannot_run(tmp_path: Path, envelope: Envelope) -> None:
    with pytest.raises(CycleError, match="it is the role that decides"):
        cycle_config(tmp_path, envelope, [], analysts=("macro_analyst",))
    with pytest.raises(CycleError, match="is not an analyst"):
        cycle_config(tmp_path, envelope, [], analysts=("technical_analyst", "astrologer"))
    with pytest.raises(CycleError, match="read nothing"):
        cycle_config(tmp_path, envelope, [], series={"executor": tmp_path / "x.toml"})
