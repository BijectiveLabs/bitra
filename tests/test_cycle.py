"""One cycle end to end: briefs from code, agents in parallel, the cost gate, one record."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.agents.briefs import (
    BriefError,
    cost_brief,
    data_brief,
    structure_brief,
)
from trading_research_agents.agents.cycle import CycleConfig, CycleError, CycleResult, run_cycle
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
    *,
    closes: list[str] | None = None,
    book: bool = True,
    funding: bool = True,
    funding_detail: dict[str, str] | None = None,
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
                detail=funding_detail or {"rate": "0.0001"},
            )
        )
    return snap


class Stub:
    """A source that answers from a prepared snapshot, so a cycle runs without a network."""

    def __init__(
        self, kind: str, value: Any, *, fail: str = "", closes: list[str] | None = None
    ) -> None:
        self.kind = kind
        self.closes = closes
        self.value = value
        self.fail = fail
        self.seen: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return f"stub:{self.kind}"

    @property
    def tier(self) -> str:
        return "free"

    def kinds(self) -> tuple[str, ...]:
        return (self.kind,)

    def fetch(self, kind: str, instrument: str, **options: Any) -> Fetched:
        self.seen.append(dict(options))
        if self.fail:
            raise SourceError(self.fail)
        prepared = snapshot_with(closes=self.closes).get(kind)
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


def chains(*, without: str = "", closes: list[str] | None = None) -> dict[str, SourceChain]:
    out = {}
    for kind in ("ohlcv", "orderbook", "funding"):
        if kind == without:
            continue
        out[kind] = SourceChain(
            kind=kind, sources=(Stub(kind, None, closes=closes),), ttl_s=600, clock=lambda: NOW
        )
    return out


def analyst_answer(
    target: str = "long",
    confidence: float = 0.9,
    horizon: int = 30,
    setup: str = "",
    cited: list[dict[str, str]] | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
) -> str:
    # The fixture's closes end at the top of their range, stretched up: a long that fits them is a
    # breakout and a short that fits them is a fade.
    setup = setup or ("range_breakout" if target == "long" else "range_reversion")
    return json.dumps(
        {
            "target": target,
            "setup": "no_setup" if target == "flat" else setup,
            "reason": "the close sits two standard deviations below its window",
            # interval_s is in this role's brief on every cycle, so a fixture can cite it and the
            # check has something real to check.
            "figures_used": [{"figure": "interval_s", "value": "60"}] if cited is None else cited,
            "confidence": confidence,
            "horizon_bars": horizon,
            "stop_price": stop_price,
            "target_price": target_price,
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


class ByStance:
    """One role asked three times: the answer depends on the stance in the brief, not on the order.

    A queue cannot do this. The three reviewers go out on threads, and which one reaches the
    completer first is not ours to decide, so the fake reads the question like a model would.
    """

    model = "fake"

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[Any] = []

    def complete(self, *, system: str, user: str, schema: Any) -> Any:
        from trading_research_agents.llm.completer import Call, Completion

        self.calls.append(Call(system=system, user=user, schema=schema))
        stance = str(json.loads(user).get("stance", ""))
        text = self.answers[stance]
        return Completion(
            text=text, model=self.model, input_tokens=len(user), output_tokens=len(text)
        )


def reviewer_answer(stance: str, verdict: str, *, figure: str = "zscore", value: str = "") -> str:
    return json.dumps(
        {
            "stance": stance,
            "claims": [
                {
                    "figure": figure,
                    "value": value or "1.403",
                    "says": "the close sits at the top of its window",
                }
            ],
            "verdict": verdict,
            "reason": f"the {stance} case rests on one figure",
        }
    )


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

    brief = structure_brief(snap, mechanism="reversion", interval_s=60, lookback_bars=5, now=NOW)
    record = brief.record()
    assert record["bars"] == 5 and record["mechanism"] == "reversion"
    assert record["spread_bps"] == "9.80" and record["funding_rate"] == "0.0001"
    assert "zscore" in record and "ret_1_bps" in record and record["age_s"] == 0.0
    # Price levels, so an analyst naming a stop or a target names it against figures it was given.
    assert record["range_high"] == "102" and record["range_low"] == "99"
    assert record["atr"] is not None and record["atr_bps"] is not None

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
        structure_brief(thin, mechanism="m", lookback_bars=5)
    with pytest.raises(BriefError, match="at least 2"):
        structure_brief(snap, mechanism="m", lookback_bars=1)
    with pytest.raises(BriefError, match="no book in the snapshot"):
        cost_brief(
            snapshot_with(book=False),
            channel=envelope.cheapest(None),
            size=Decimal("5"),
            direction=Direction.LONG,
            horizon_bars=30,
        )


def test_the_cost_counts_funding_at_the_venues_own_interval(envelope: Envelope) -> None:
    def funding_of(detail: dict[str, str] | None) -> dict[str, str]:
        costs = cost_brief(
            snapshot_with(funding_detail=detail),
            channel=envelope.cheapest(None),
            size=Decimal("5"),
            direction=Direction.LONG,
            horizon_bars=360,
            bar_interval_s=60,
        )
        return next(c for c in costs.record()["components"] if c["name"] == "funding_accrual")

    # Six hours held at one basis point a settlement: once at the declared eight hours, six times
    # where the venue settles every hour.
    declared = funding_of(None)
    assert declared["bps"] == "1.0000" and "[run].settlement_interval_s" in declared["source"]
    hourly = funding_of({"rate": "0.0001", "interval": "1h"})
    assert hourly["bps"] == "6.0000" and "the venue's interval" in hourly["source"]


def test_a_contract_with_no_funding_rate_is_refused_not_priced_cheap(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path, envelope, [analyst_answer()], chains=chains(without="funding")
    )
    result = run_cycle(cfg)
    assert not result.acted and result.stopped_by == "cost: no funding rate"
    assert any("not priced without it" in r for r in result.refusals)


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
    brief = json.loads((where / "briefs" / "structure_lead.json").read_text("utf-8"))
    assert brief["bars"] == 5 and brief["mechanism"].startswith("short-horizon")
    # The figures the analyst names its stop and target from reach it, rather than being filtered.
    assert {"range_high", "range_low", "atr", "atr_bps"} <= set(brief)
    assert not any("not this role's to read" in line for line in result.refusals)
    assert (where / "briefs" / "cost.json").is_file()
    assert (where / "answers" / "structure_lead.json").is_file()
    intents = (where / "intents.jsonl").read_text("utf-8").strip().splitlines()
    assert len(intents) == 1 and json.loads(intents[0])["target"] == "long"
    summary = json.loads((where / "cycle.json").read_text("utf-8"))
    assert summary["cost"]["admitted"] is True and summary["mode"] == "sandbox"
    assert summary["agents"]["agents"][0]["agent"] == "structure_lead"
    assert summary["tokens"] == usage.tokens


def test_the_cycle_hands_each_source_what_the_operator_declared(
    tmp_path: Path, envelope: Envelope
) -> None:
    built = chains()
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer()],
        chains=built,
        data_options={
            "orderbook": {"levels": 5},
            "ohlcv": {"limit": 2, "params": {"price": "mark"}},
        },
    )
    run_cycle(cfg)

    book = built["orderbook"].sources[0]
    assert isinstance(book, Stub) and book.seen == [{"levels": 5}]
    bars = built["ohlcv"].sources[0]
    assert isinstance(bars, Stub)
    # The window the analyst reads is the cycle's to decide; everything else the operator declared
    # for that kind survives, venue quirks included.
    assert bars.seen == [{"limit": 5, "interval_s": 60, "params": {"price": "mark"}}]


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
    assert "no structure brief" in result.stopped_by
    assert any("data ohlcv" in r for r in result.refusals)


def test_a_cycle_refuses_to_be_configured_wrong(tmp_path: Path, envelope: Envelope) -> None:
    for overrides, message in (
        ({"instrument": " "}, "names its instrument"),
        ({"mode": "nowhere"}, "mode is one of"),
        # Venue mode is refused unless the caller hands the cycle something to send with.
        ({"mode": "venue"}, "needs something to send with"),
        ({"size": Decimal(0)}, "exactly one of them"),
        ({"size": Decimal("-1")}, "positive when declared"),
        ({"size_quote": Decimal(100)}, "exactly one of them"),
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
            # The fixture's prints climb one tick a minute, which measures as a trend; a range
            # setup on it would be refused by the regime rule before the cost brief is reached.
            lambda model, base_url: FakeCompleter([analyst_answer(setup="trend_continuation")]),
        )
    )
    config = project / "strategy.toml"
    text = config.read_text("utf-8")
    start, end = text.index("[strategy]"), text.index("[exit]")
    text = (
        text[:start]
        + '[strategy]\nagent = "structure_lead"\n\n'
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
        # The fixture's dates sit months before the test clock; freshness has its own tests.
        'max_age = "365d"',
    ]
    if threshold:
        body.append(f"threshold = {threshold}")
    path = tmp_path / f"{name}.toml"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def test_the_readings_are_read_by_code_and_ask_no_model(tmp_path: Path, envelope: Envelope) -> None:
    macro = series_for(tmp_path, "us10y", "4.4", "{ bearish_above = 4.25 }")
    onchain = series_for(tmp_path, "venue_net_flow", "120")
    steps: list[tuple[str, str, str]] = []
    cfg, fake, usage = cycle_config(
        tmp_path,
        envelope,
        {"structure_lead": analyst_answer()},
        watch=lambda step: steps.append((step.name, step.state, step.detail)),
        series={"macro": (macro,), "onchain": (onchain,)},
        readings=("macro", "onchain", "sentiment"),
    )
    result = run_cycle(cfg)
    assert result.stopped_by is None and result.acted
    # One model is asked, the one that proposes; the readings cost nothing.
    assert [o.name for o in result.fan.outcomes] == ["structure_lead"]
    assert len(fake.calls) == 1 and usage.tokens > 0
    assert result.context["macro"]["declared_state"]["declared"] == "bearish"
    assert result.context["onchain"]["declared_state"]["declared"] == "undeclared"
    assert {said["read_by"] for said in result.context.values()} == {"code"}
    assert ("macro", "done", "read by code: risk-off") in steps
    from trading_research_agents.cli.cycle_report import _cycle_report

    _, lines = _cycle_report(result, usage, "sandbox")
    assert "macro      read by code: bearish (risk-off)" in lines
    assert not any("None" in line for line in lines if line.startswith("macro"))

    where = Path(str(result.path))
    for name in ("macro", "onchain", "sentiment"):
        assert (where / "briefs" / f"{name}.json").is_file()
        assert not (where / "answers" / f"{name}.json").exists()
    summary = json.loads((where / "cycle.json").read_text("utf-8"))
    assert set(summary["context"]) == {"macro", "onchain", "sentiment"}
    macro_brief_read = json.loads((where / "briefs" / "macro.json").read_text("utf-8"))
    assert macro_brief_read["declared_series"] == ["us10y"]
    assert macro_brief_read["series"][0]["state"] == "bearish"


def test_a_reading_whose_series_cannot_be_read_is_refused_not_guessed(
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
        series={"onchain": (missing,)},
        readings=("onchain",),
    )
    result = run_cycle(cfg)
    assert result.acted and result.context == {}
    assert any(r.startswith("onchain") and "does not exist" in r for r in result.refusals)
    assert len(fake.calls) == 1


def test_a_cycle_refuses_a_roster_it_cannot_run(tmp_path: Path, envelope: Envelope) -> None:
    with pytest.raises(CycleError, match="is not a reading"):
        cycle_config(tmp_path, envelope, [], readings=("astrologer",))
    with pytest.raises(CycleError, match="is not a reading"):
        cycle_config(tmp_path, envelope, [], series={"executor": (tmp_path / "x.toml",)})


def test_the_matrix_refuses_a_direction_the_declarations_do_not_support(
    tmp_path: Path, envelope: Envelope
) -> None:
    from trading_research_agents.execution.confluence import Matrix

    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {"structure_lead": analyst_answer()},
        readings=("macro",),
        matrix=Matrix(required=("macro",)),
    )
    result = run_cycle(cfg)

    # Nothing was declared for the macro reading in this fixture, so it produced no state at all.
    assert not result.acted and result.confluence is not None
    assert not result.confluence.admitted
    assert any("confluence: macro declares undeclared" in r for r in result.refusals)
    # The cost was priced before the matrix read, so the record still shows what the trade cost.
    assert result.briefs.get("cost") is not None
    summary = json.loads((Path(str(result.path)) / "cycle.json").read_text("utf-8"))
    assert summary["confluence"]["states"] == {"macro": "undeclared"}
    assert summary["intent"] is None


def test_the_targets_refuse_before_there_is_an_intent(tmp_path: Path, envelope: Envelope) -> None:
    from trading_research_agents.execution.targets import Targets

    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer()],
        targets=Targets(min_net_bps=Decimal("1000")),
    )
    result = run_cycle(cfg)

    assert not result.acted and result.targets is not None
    assert not result.targets.admitted
    assert any("targets: net " in r for r in result.refusals)
    # The cost gate admitted it; the desk's own target is what refused.
    assert result.verdict is not None and result.verdict.admitted


def test_the_round_trip_count_comes_from_the_intents_already_written(
    tmp_path: Path, envelope: Envelope
) -> None:
    from trading_research_agents.execution.targets import Targets

    first, _, _ = cycle_config(tmp_path, envelope, [analyst_answer()])
    assert run_cycle(first).acted

    again, _, _ = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer()],
        targets=Targets(max_round_trips_per_day=1),
        clock=lambda: datetime(2026, 9, 27, 11, 0, tzinfo=UTC),
    )
    result = run_cycle(again)

    assert not result.acted
    assert any("1 intent(s) already written today" in r for r in result.refusals)


def test_the_reviewers_argue_and_only_cited_claims_reach_the_matrix(
    tmp_path: Path, envelope: Envelope
) -> None:
    from trading_research_agents.execution.confluence import Matrix

    stances = ByStance(
        {
            "pro": reviewer_answer("pro", "support"),
            "contra": reviewer_answer("contra", "oppose", figure="made_up", value="1"),
        }
    )
    fake = Scripted({"structure_lead": analyst_answer()})
    usage = UsageLedger(tmp_path / "usage.jsonl")
    limits = BudgetLimits(token_budget=500_000, max_calls_per_hour=1000, min_interval_s=0)
    metered: dict[str, MeteredCompleter] = {}

    def completer_for(role: str) -> MeteredCompleter:
        if role not in metered:
            inner = stances if role == "reviewer" else fake.for_role(role)
            metered[role] = MeteredCompleter(
                inner,
                role=role,
                provider="fake",
                limits=limits,
                ledger=usage,
                clock=lambda: 0.0,
            )
        return metered[role]

    cfg = CycleConfig(
        instrument="BTCUSDT",
        envelope=envelope,
        venue_key=envelope.cheapest(None).key,
        chains=chains(),
        completer_for=completer_for,
        usage=usage,
        root=tmp_path / "cycles",
        mechanism="short-horizon reversion after a burst of prints",
        size=Decimal("5"),
        magnitude_bps=Decimal("40"),
        clock=lambda: datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
        lookback_bars=5,
        reviewers=("pro", "contra"),
        matrix=Matrix(min_support=1, max_opposition=0),
    )
    result = run_cycle(cfg)

    # The contra stance cited a figure that was not in the brief, so its claim and its vote went.
    assert result.context["reviewer_pro"]["verdict"] == "support"
    assert result.context["reviewer_contra"]["verdict"] == "abstain"
    assert any("discarded a claim, 'made_up' is not a figure" in r for r in result.refusals)
    assert result.confluence is not None and result.confluence.admitted
    assert result.confluence.support == 1 and result.confluence.opposition == 0
    assert result.acted

    where = Path(str(result.path))
    assert (where / "briefs" / "reviewer_pro.json").is_file()
    assert (where / "answers" / "reviewer_contra.json").is_file()
    summary = json.loads((where / "cycle.json").read_text("utf-8"))
    assert summary["reviewers"]["agents"][0]["agent"].startswith("reviewer_")


def test_a_reviewer_that_opposes_can_stop_a_cycle_the_cost_gate_would_have_passed(
    tmp_path: Path, envelope: Envelope
) -> None:
    from trading_research_agents.execution.confluence import Matrix

    stances = ByStance({"contra": reviewer_answer("contra", "oppose")})
    fake = Scripted({"structure_lead": analyst_answer()})
    usage = UsageLedger(tmp_path / "usage.jsonl")
    limits = BudgetLimits(token_budget=500_000, max_calls_per_hour=1000, min_interval_s=0)
    metered: dict[str, MeteredCompleter] = {}

    def completer_for(role: str) -> MeteredCompleter:
        if role not in metered:
            inner = stances if role == "reviewer" else fake.for_role(role)
            metered[role] = MeteredCompleter(
                inner, role=role, provider="fake", limits=limits, ledger=usage, clock=lambda: 0.0
            )
        return metered[role]

    cfg = CycleConfig(
        instrument="BTCUSDT",
        envelope=envelope,
        venue_key=envelope.cheapest(None).key,
        chains=chains(),
        completer_for=completer_for,
        usage=usage,
        root=tmp_path / "cycles",
        mechanism="short-horizon reversion after a burst of prints",
        size=Decimal("5"),
        magnitude_bps=Decimal("40"),
        clock=lambda: datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
        lookback_bars=5,
        reviewers=("contra",),
        matrix=Matrix(max_opposition=0),
    )
    result = run_cycle(cfg)

    assert not result.acted
    assert any("1 reviewer(s) oppose it" in r for r in result.refusals)


def test_a_stance_asked_twice_is_refused_when_the_cycle_is_configured(
    tmp_path: Path, envelope: Envelope
) -> None:
    with pytest.raises(CycleError, match="each stance is asked once"):
        cycle_config(tmp_path, envelope, [analyst_answer()], reviewers=("pro", "pro"))
    with pytest.raises(CycleError, match="is not a stance"):
        cycle_config(tmp_path, envelope, [analyst_answer()], reviewers=("bullish",))


def conviction_rule(**overrides: Any) -> Any:
    from trading_research_agents.execution.conviction import Conviction, Tier

    settings: dict[str, Any] = {
        "weights": {"structure_lead": Decimal(1), "macro": Decimal(1)},
        "min_score": Decimal("0.4"),
        "ladder": (
            Tier(Decimal("0.8"), Decimal(1)),
            Tier(Decimal("0.5"), Decimal("0.5")),
            Tier(Decimal("0.4"), Decimal("0.25")),
        ),
    }
    settings.update(overrides)
    return Conviction(**settings)


def test_conviction_sizes_the_trade_and_the_cost_is_priced_at_that_size(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer(confidence=0.6)],
        conviction=conviction_rule(),
        size_step=Decimal("0.1"),
    )
    result = run_cycle(cfg)

    # The macro reading was not read, so it is silence: the score is the deciding confidence.
    assert result.stake is not None and result.stake.score.score == Decimal("0.600")
    assert result.stake.multiple == Decimal("0.5") and result.stake.size == Decimal("2.5")
    assert result.acted and result.intent is not None
    assert result.intent["size"] == "2.5" and result.intent["base_size"] == "5"
    # The gate priced the trade it admitted, not the base the operator declared.
    assert result.briefs["cost"]["size"] == "2.5"
    summary = json.loads((Path(str(result.path)) / "cycle.json").read_text("utf-8"))
    assert summary["conviction"]["score"]["contributions"]["macro"] == "0"


def test_conviction_below_the_floor_refuses_after_everything_else_passed(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer(confidence=0.7)],
        conviction=conviction_rule(min_score=Decimal("0.75")),
    )
    result = run_cycle(cfg)

    assert not result.acted and result.stake is not None and not result.stake.admitted
    assert any("below the declared floor" in r for r in result.refusals)
    # Everything before it was fine; the desk simply does not trade this one.
    assert result.confluence is None and result.stopped_by is None


def test_a_context_that_disagrees_costs_size_rather_than_being_averaged_away(
    tmp_path: Path, envelope: Envelope
) -> None:
    agreeing, _, _ = cycle_config(
        tmp_path / "agrees",
        envelope,
        {"structure_lead": analyst_answer(confidence=1.0)},
        readings=("macro",),
        conviction=conviction_rule(),
    )
    # Nothing is declared for the macro reading in this fixture, so it stays silent either way; what
    # this pins is that silence neither lifts nor dilutes the deciding analyst's confidence.
    result = run_cycle(agreeing)
    assert result.stake is not None and result.stake.score.score == Decimal("1.000")
    assert result.stake.score.silent == ("macro",)


def test_venue_mode_sends_the_intent_and_keeps_what_came_back(
    tmp_path: Path, envelope: Envelope
) -> None:
    sent: list[dict[str, Any]] = []

    def hand(intent: Any) -> dict[str, Any]:
        sent.append(dict(intent))
        return {"sent": True, "order_id": "order-1", "status": "closed"}

    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer()], mode="venue", place=hand)
    result = run_cycle(cfg)

    assert result.acted and result.placed == {
        "sent": True,
        "order_id": "order-1",
        "status": "closed",
    }
    assert sent and sent[0]["target"] == "long"
    where = Path(str(result.path))
    assert json.loads((where / "placed.json").read_text("utf-8"))["order_id"] == "order-1"
    summary = json.loads((where / "cycle.json").read_text("utf-8"))
    assert summary["placed"]["sent"] is True and summary["mode"] == "venue"


def test_a_venue_that_refuses_leaves_the_decision_on_disk(
    tmp_path: Path, envelope: Envelope
) -> None:
    def hand(intent: Any) -> dict[str, Any]:
        raise RuntimeError("insufficient margin")

    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer()], mode="venue", place=hand)
    result = run_cycle(cfg)

    # The intent was written before anything was sent, and it stays written.
    assert result.acted and result.placed is not None and result.placed["sent"] is False
    assert any("venue: RuntimeError: insufficient margin" in r for r in result.refusals)
    where = Path(str(result.path))
    assert (where / "intents.jsonl").read_text("utf-8").strip()
    assert json.loads((where / "placed.json").read_text("utf-8"))["sent"] is False


def test_the_intent_sent_carries_the_risk_gates_approval(
    tmp_path: Path, envelope: Envelope
) -> None:
    from trading_research_agents.risk import approval_problem

    sent: list[dict[str, Any]] = []

    def hand(intent: Any) -> dict[str, Any]:
        sent.append(dict(intent))
        return {"sent": True, "order_id": "order-1", "status": "closed"}

    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer()], mode="venue", place=hand)
    result = run_cycle(cfg)

    assert approval_problem(sent[0], now=cfg.clock().timestamp()) == ""
    summary = json.loads((Path(str(result.path)) / "cycle.json").read_text("utf-8"))
    assert (
        summary["risk_gate"]["approved"] is True
        and "risk per trade" in summary["risk_gate"]["checks"]
    )


def test_an_armed_switch_stops_the_cycle_at_the_risk_gate(
    tmp_path: Path, envelope: Envelope
) -> None:
    switch = tmp_path / "KILL"
    switch.write_text("stop", encoding="utf-8")
    sent: list[Any] = []
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer()],
        mode="venue",
        place=sent.append,
        switches=(switch,),
    )
    result = run_cycle(cfg)

    assert result.intent is None and result.placed is None and sent == []
    assert any(r.startswith("risk gate: held by the switch at") for r in result.refusals)
    assert not (Path(str(result.path)) / "intents.jsonl").exists()


def test_an_unknown_send_is_written_down_and_holds_its_instrument(
    tmp_path: Path, envelope: Envelope
) -> None:
    from trading_research_agents.execution.unknown import blocking

    def hand(intent: Any) -> dict[str, Any]:
        return {"sent": None, "status": "UNKNOWN", "client_order_id": "tra9", "unknown": "lost"}

    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer()], mode="venue", place=hand)
    result = run_cycle(cfg)

    assert result.placed is not None and result.placed["status"] == "UNKNOWN"
    assert any(r.startswith("venue: order state UNKNOWN") for r in result.refusals)
    where = Path(str(result.path))
    assert json.loads((where / "placed.json").read_text("utf-8"))["client_order_id"] == "tra9"
    assert "tra9" in blocking(cfg.root, cfg.instrument)


class Stopped:
    """A cycle result as the schedule sees it: only what stopped it, if anything."""

    def __init__(self, stopped_by: str | None = None) -> None:
        self.stopped_by = stopped_by


def test_the_schedule_runs_once_unless_an_interval_is_asked_for() -> None:
    from trading_research_agents.cli.schedule import Schedule

    once = Schedule()
    assert not once.looping and once.stop_reason(Stopped(), 1) == "one cycle"
    assert once.wait() == 0.0


def test_the_schedule_lands_on_the_next_boundary(tmp_path: Path) -> None:
    from trading_research_agents.cli.schedule import Schedule

    slept: list[float] = []
    # Twelve seconds past a five-minute boundary: the next cycle is 288 seconds away, not 300.
    plan = Schedule(every_s=300.0, clock=lambda: 1_800_000_012.0, rest=lambda s: slept.append(s))
    assert plan.wait() == 288.0 and slept == [288.0]


def test_the_schedule_says_when_the_wait_ends_on_the_clock() -> None:
    from trading_research_agents.cli.schedule import Schedule

    # 1_800_000_000 is 2027-01-15 08:00:00 UTC; ten minutes into a half hour leaves twenty.
    plan = Schedule(every_s=1800.0, clock=lambda: 1_800_000_600.0)
    said = plan.announce(plan.left())
    assert "08:30:00 UTC" in said and "in 20m 00s" in said and "every 30m" in said
    assert Schedule(every_s=90.0).every_label == "90s"


def test_a_loop_ends_on_the_budget_the_kill_switch_or_the_count(tmp_path: Path) -> None:
    from trading_research_agents.cli.schedule import Schedule

    kill = tmp_path / "KILL"
    plan = Schedule(every_s=60.0, cycles=3, kill_switch=kill, rest=lambda s: None)

    assert plan.stop_reason(Stopped(), 1) == ""
    assert "budget" in plan.stop_reason(Stopped("structure_lead: BudgetSpentError: spent"), 1)
    # A stop that is not the budget does not end the loop: the next cycle decides again.
    assert plan.stop_reason(Stopped("no structure brief: ohlcv is not in the snapshot"), 1) == ""

    assert plan.stop_reason(Stopped(), 3) == "the 3 pass(es) asked for"

    kill.write_text("stop", encoding="utf-8")
    assert "kill switch" in plan.stop_reason(Stopped(), 1)


def test_an_interval_below_a_trading_cadence_is_refused() -> None:
    import argparse

    from trading_research_agents.cli.arguments import parse_duration_s

    assert parse_duration_s("5m") == 300.0
    with pytest.raises(argparse.ArgumentTypeError, match="five seconds"):
        parse_duration_s("1s")


def test_the_intent_carries_the_exits_code_computed(tmp_path: Path, envelope: Envelope) -> None:
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer()],
        stop_frac=Decimal("0.01"),
        take_frac=Decimal("0.02"),
    )
    result = run_cycle(cfg)

    assert result.intent is not None
    mid = Decimal(result.intent["mid"])
    # A long stops below and takes above, both from the operator's own fractions.
    assert Decimal(result.intent["stop_price"]) == (mid * Decimal("0.99")).quantize(mid)
    assert Decimal(result.intent["take_price"]) == (mid * Decimal("1.02")).quantize(mid)


def test_an_intent_without_declared_exits_carries_none(tmp_path: Path, envelope: Envelope) -> None:
    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer()])
    result = run_cycle(cfg)

    assert result.intent is not None
    assert "stop_price" not in result.intent and "take_price" not in result.intent


def test_a_quote_size_is_converted_at_the_cycles_own_mid(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer()],
        size=Decimal(0),
        size_quote=Decimal("510"),
        size_step=Decimal("0.1"),
    )
    result = run_cycle(cfg)

    # The book in the fixture is 101.95 / 102.05, so 510 of quote is 5.0 of base.
    assert result.intent is not None and Decimal(result.intent["base_size"]) == Decimal("5.0")
    summary = json.loads((Path(str(result.path)) / "cycle.json").read_text("utf-8"))
    said = summary["size"]
    assert (said["mid"], said["size_quote"], said["step"]) == ("102.00", "510", "0.1")
    assert Decimal(said["size"]) == Decimal(5)


def test_a_quote_size_below_one_step_stops_before_anyone_is_asked(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, usage = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer()],
        size=Decimal(0),
        size_quote=Decimal("50"),
        size_step=Decimal("1"),
    )
    result = run_cycle(cfg)
    assert not result.acted and result.stopped_by is not None
    assert "less than one step" in result.stopped_by and usage.tokens == 0


def test_an_interrupt_at_the_venue_is_written_down_before_it_stops_the_run(
    tmp_path: Path, envelope: Envelope
) -> None:
    def hand(intent: Any) -> dict[str, Any]:
        return {
            "sent": True,
            "order_id": "order-1",
            "interrupted": True,
            "cancelled": True,
            "note": "interrupted while the order rested; it was cancelled",
        }

    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer()], mode="venue", place=hand)
    with pytest.raises(KeyboardInterrupt, match="interrupted at the venue"):
        run_cycle(cfg)
    [where] = [p for p in (tmp_path / "cycles").iterdir() if (p / "cycle.json").is_file()]
    assert json.loads((where / "placed.json").read_text("utf-8"))["cancelled"] is True
    summary = json.loads((where / "cycle.json").read_text("utf-8"))
    assert summary["stopped_by"].startswith("interrupted at the venue")


def test_a_reading_leaves_its_declared_state_without_a_model(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        {"structure_lead": analyst_answer()},
        readings=("macro",),
    )
    result = run_cycle(cfg)
    said = result.context["macro"]
    # Nothing is asked, so nothing can fail: the code over the operator's thresholds stands.
    assert said == {"declared_state": said["declared_state"], "read_by": "code"}
    assert said["declared_state"]["declared"] == "undeclared"


def test_an_instrument_already_open_at_the_venue_waits_without_asking_anyone(
    tmp_path: Path, envelope: Envelope
) -> None:
    from trading_research_agents.execution.desk import Open

    cfg, _, usage = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer()],
        held=lambda: ((Open(instrument="BTCUSDT", side="long"),), "the venue"),
        wait_while_open=True,
    )
    result = run_cycle(cfg)
    assert result.held and not result.acted and usage.tokens == 0
    assert any("waits for its exits" in r for r in result.refusals)


def test_the_account_sets_the_equity_and_a_day_of_losses_stops_new_entries(
    tmp_path: Path, envelope: Envelope
) -> None:
    from trading_research_agents.trading.capital import Account

    readings = iter([Decimal(10000), Decimal(9600)])

    def account() -> Account:
        return Account("USDT", next(readings), Decimal(9000), Decimal(0))

    first, _, _ = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer()],
        equity=Decimal(50000),
        account=account,
        daily_loss_limit_bps=Decimal(300),
    )
    done = run_cycle(first)
    # Sizing uses the smaller of what is declared and what is there.
    assert done.briefs["equity"]["used"] == "10000"

    later, _, usage = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer()],
        equity=Decimal(50000),
        account=account,
        daily_loss_limit_bps=Decimal(300),
    )
    stopped = run_cycle(later)
    assert stopped.stopped_by is not None and "down 400.00 bps" in stopped.stopped_by
    assert not stopped.acted and usage.tokens == 0


def test_a_declared_reviewer_that_never_answers_refuses_the_trade_by_default(
    tmp_path: Path, envelope: Envelope
) -> None:
    # The reviewer role is scripted once, so one of the two stances gets no answer at all.
    answers = {
        "structure_lead": analyst_answer(),
        "reviewer": reviewer_answer("pro", "support"),
    }
    cfg, _, _ = cycle_config(tmp_path, envelope, answers, reviewers=("pro", "contra"))
    refused = run_cycle(cfg)
    assert not refused.acted and refused.stopped_by is not None
    assert "did not answer" in refused.stopped_by and "on_missing_reviewer" in refused.stopped_by

    cfg, _, _ = cycle_config(
        tmp_path / "continue",
        envelope,
        dict(answers),
        reviewers=("pro", "contra"),
        on_missing_reviewer="continue",
    )
    carried_on = run_cycle(cfg)
    assert carried_on.acted


def test_the_desk_analyst_is_told_the_regime_picks_the_setup(
    tmp_path: Path, envelope: Envelope
) -> None:
    from trading_research_agents.agents.roles import prompt

    cfg, fake, _ = cycle_config(tmp_path, envelope, [analyst_answer()])
    run_cycle(cfg)
    told = fake.calls[0].system
    # The desk's prompt, not the research core's sealed pre-registration.
    assert told == prompt("structure_lead_live")
    assert "it is not the only setup you may name" in told and "pre-registration" not in told


def test_a_short_on_a_spot_market_is_refused_before_it_is_priced(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer("short")], spot=True)
    result = run_cycle(cfg)
    assert result.intent is None and result.verdict is None
    assert any("is a spot market; a short would sell coins" in r for r in result.refusals)
    assert not (Path(str(result.path)) / "briefs" / "cost.json").exists()
    # The same market, a long: everything else applies as on a perpetual.
    long_cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer("long")], spot=True)
    spot_long = run_cycle(long_cfg)
    assert spot_long.intent is not None
    # Funding belongs to contracts: on spot it is not asked for, and so not refused either.
    assert "funding" not in spot_long.snapshot.values
    assert not any(r.startswith("data funding") for r in spot_long.refusals)


def test_a_spot_cycle_is_priced_at_the_channels_spot_fees(
    tmp_path: Path, envelope: Envelope
) -> None:
    from dataclasses import replace

    channel = envelope.cheapest(None)
    spot_priced = replace(channel, market_fees=(("spot", Decimal(10), Decimal(10)),))
    priced = Envelope(
        channels=tuple(spot_priced if c is channel else c for c in envelope.channels),
        order_latency_ms=envelope.order_latency_ms,
    )
    cfg, _, _ = cycle_config(tmp_path, priced, [analyst_answer("long")], spot=True)
    result = run_cycle(cfg)
    fee = next(c for c in result.briefs["cost"]["components"] if c["name"] == "fee_round_trip")
    assert Decimal(fee["bps"]) == Decimal(20) and fee["source"].endswith("its spot fees")


def test_the_analyst_is_shown_its_own_last_reading_and_never_what_became_of_it(
    tmp_path: Path, envelope: Envelope
) -> None:
    first, _, _ = cycle_config(tmp_path, envelope, [analyst_answer(confidence=0.7)])
    assert run_cycle(first).intent is not None
    later = datetime(2026, 9, 27, 10, 3, tzinfo=UTC)
    second, fake, _ = cycle_config(
        tmp_path, envelope, [analyst_answer(target="flat")], clock=lambda: later
    )
    run_cycle(second)
    shown = json.loads(fake.calls[0].user)["previous"]
    assert shown["target"] == "long" and shown["confidence"] == 0.7
    assert shown["minutes_ago"] == 3.0 and shown["move_since_bps"] == "0.00"
    # The reading and the market since; nothing about whether it was traded.
    assert set(shown) == {
        "minutes_ago",
        "target",
        "setup",
        "confidence",
        "stop_price",
        "target_price",
        "close_then",
        "move_since_bps",
    }
    # Past the window the analyst reads (five bars of a minute), there is nothing to carry over.
    far = datetime(2026, 9, 27, 10, 30, tzinfo=UTC)
    third, fake, _ = cycle_config(tmp_path, envelope, [analyst_answer()], clock=lambda: far)
    run_cycle(third)
    assert "previous" not in json.loads(fake.calls[0].user)


def test_a_malformed_answer_is_asked_again_with_the_reason(
    tmp_path: Path, envelope: Envelope
) -> None:
    cfg, fake, _ = cycle_config(tmp_path, envelope, ['{"target": "long"}', analyst_answer()])
    result = run_cycle(cfg)
    assert result.analyst is not None and len(fake.calls) == 2
    again = fake.calls[1].user
    assert "refused before anyone read it: response lacks" in again
    # The brief comes first, unchanged, so a provider's cached prefix holds for the second call.
    assert again.startswith(fake.calls[0].user)


def test_each_reading_sees_its_own_series_and_the_analyst_sees_none_of_them(
    tmp_path: Path, envelope: Envelope
) -> None:
    macro = series_for(tmp_path, "us10y", "4.4", "{ bearish_above = 4.25 }")
    onchain = series_for(tmp_path, "venue_net_flow", "120", "{ bearish_above = 100 }")
    answers = {
        "structure_lead": analyst_answer(),
    }
    cfg, _fake, _ = cycle_config(
        tmp_path,
        envelope,
        answers,
        series={"macro": (macro,), "onchain": (onchain,)},
        readings=("macro", "onchain"),
    )
    result = run_cycle(cfg)
    where = Path(str(result.path)) / "briefs"
    seen = {
        name: (where / f"{name}.json").read_text("utf-8") for name in ("structure_lead", "macro")
    }
    seen["onchain"] = (where / "onchain.json").read_text("utf-8")
    others = {
        "structure_lead": ("us10y", "venue_net_flow", "declared_series"),
        "macro": ("venue_net_flow", "market_structure"),
        "onchain": ("us10y", "market_structure"),
    }
    for name, words in others.items():
        assert [w for w in words if w in seen[name]] == [], name
    # The readings are weighed by code alone.
    assert result.context["macro"]["declared_state"]["declared"] == "bearish"


def judged_cycle(
    tmp_path: Path, envelope: Envelope, answers: dict[str, str], judge: Any = None
) -> tuple[CycleResult, ByStance]:
    from trading_research_agents.execution.confluence import Matrix

    stances = ByStance(answers)
    fake = Scripted({"structure_lead": analyst_answer()})
    usage = UsageLedger(tmp_path / "usage.jsonl")
    limits = BudgetLimits(token_budget=500_000, max_calls_per_hour=1000, min_interval_s=0)
    metered: dict[str, MeteredCompleter] = {}

    def completer_for(role: str) -> MeteredCompleter:
        if role not in metered:
            inner = stances if role == "reviewer" else fake.for_role(role)
            metered[role] = MeteredCompleter(
                inner, role=role, provider="fake", limits=limits, ledger=usage, clock=lambda: 0.0
            )
        return metered[role]

    cfg = CycleConfig(
        instrument="BTCUSDT",
        envelope=envelope,
        venue_key=envelope.cheapest(None).key,
        chains=chains(),
        completer_for=completer_for,
        usage=usage,
        root=tmp_path / "cycles",
        mechanism="short-horizon reversion after a burst of prints",
        size=Decimal("5"),
        magnitude_bps=Decimal("40"),
        clock=lambda: datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
        lookback_bars=5,
        reviewers=("pro", "contra", "neutral"),
        matrix=Matrix(min_support=1),
        judge=judge,
    )
    return run_cycle(cfg), stances


def test_neutral_judges_both_cases_and_only_its_verdict_counts(
    tmp_path: Path, envelope: Envelope
) -> None:
    pro = json.loads(reviewer_answer("pro", "support"))
    pro["claims"].append({"figure": "made_up", "value": "1", "says": "an invented figure"})
    result, stances = judged_cycle(
        tmp_path,
        envelope,
        {
            "pro": json.dumps(pro),
            "contra": reviewer_answer("contra", "oppose"),
            "neutral": reviewer_answer("neutral", "oppose"),
        },
    )
    # The advocates first, apart; the judge last, with both cases as code checked them.
    first, judge = stances.calls[:2], json.loads(stances.calls[2].user)
    assert {json.loads(c.user)["stance"] for c in first} == {"pro", "contra"}
    assert not any("case_for" in json.loads(c.user) for c in first)
    assert [c["figure"] for c in judge["case_for"]] == ["zscore"]
    assert [c["figure"] for c in judge["case_against"]] == ["zscore"]
    # It weighs the evidence, not the advocates' votes.
    assert "verdict" not in json.dumps(judge["case_for"]) and "reason" not in judge["case_for"][0]
    assert stances.calls[2].system != stances.calls[0].system
    assert result.context["reviewer_neutral"]["judged"] == "neutral"
    # Pro supported, but the judge opposed, and only the judge counts: min_support 1 is not met.
    assert result.confluence is not None and result.confluence.support == 0
    assert not result.acted


def test_a_case_nobody_answers_is_decided_by_code_without_asking_the_judge(
    tmp_path: Path, envelope: Envelope
) -> None:
    result, stances = judged_cycle(
        tmp_path,
        envelope,
        {
            "pro": reviewer_answer("pro", "support"),
            "contra": reviewer_answer("contra", "oppose", figure="made_up", value="1"),
        },
    )
    assert len(stances.calls) == 2
    judged = result.context["reviewer_neutral"]
    assert judged["verdict"] == "support" and judged["judged"] == "code"
    assert "neutral was not asked" in judged["reason"]
    assert result.confluence is not None and result.confluence.support == 1
    assert result.acted


def test_with_a_judge_declared_the_judge_weighs_the_two_cases_with_probabilities(
    tmp_path: Path, envelope: Envelope
) -> None:
    from trading_research_agents.llm.judge import Checked, FakeJudge, Picked

    weighed = Picked("oppose", 0.4, {"support": 0.2, "oppose": 0.7, "abstain": 0.1})
    judge = FakeJudge({"verdict": weighed, "describes": Checked(0.9)}, model="jev-1.14.0")
    answers = {
        "pro": reviewer_answer("pro", "support"),
        "contra": reviewer_answer("contra", "oppose"),
    }
    result, stances = judged_cycle(tmp_path, envelope, answers, judge=judge)
    # Two advocates asked of a model; the neutral verdict asked of the judge, never of a model.
    assert len(stances.calls) == 2
    neutral = result.context["reviewer_neutral"]
    assert neutral["verdict"] == "oppose" and neutral["judged"] == "judge"
    assert neutral["model"] == "jev-1.14.0" and neutral["probabilities"]["oppose"] == 0.7
    state = next(s for s, q in judge.asked if "verdict" in q)
    assert isinstance(state, dict)
    assert "zscore = 1.403" in state["case_for"][0] and state["case_against"]
    assert result.confluence is not None and result.confluence.support == 0


def test_a_judge_unsure_of_its_verdict_abstains(tmp_path: Path, envelope: Envelope) -> None:
    from trading_research_agents.llm.judge import Checked, FakeJudge, Picked

    unsure = Picked("support", 0.1, {"support": 0.45, "oppose": 0.35, "abstain": 0.2})
    judge = FakeJudge({"verdict": unsure, "describes": Checked(0.9)}, model="jev-1.14.0")
    answers = {
        "pro": reviewer_answer("pro", "support"),
        "contra": reviewer_answer("contra", "oppose"),
    }
    result, _ = judged_cycle(tmp_path, envelope, answers, judge=judge)
    assert result.context["reviewer_neutral"]["verdict"] == "abstain"


RANGE_THEN_BREAK = [str(100 + (i % 4) * 0.5) for i in range(56)] + ["101", "101.5", "102", "102"]


def test_the_leads_levels_are_read_against_the_instruments_own_paths(
    tmp_path: Path, envelope: Envelope
) -> None:
    levels = {"stop_frac": Decimal("0.01"), "take_frac": Decimal("0.02")}
    cfg, _, _ = cycle_config(
        tmp_path, envelope, [analyst_answer()], chains=chains(closes=RANGE_THEN_BREAK), **levels
    )
    result = run_cycle(cfg)
    summary = json.loads((Path(str(result.path)) / "cycle.json").read_text("utf-8"))
    paths = summary["paths"]
    assert paths["side"] == "long" and paths["paths"] == 1000 and paths["context_bars"] == 59
    assert paths["take_bps"] is not None and paths["stop_bps"] is not None
    assert math.isclose(paths["take_first"] + paths["stop_first"] + paths["neither"], 1.0)
    assert result.acted
    # Seeded by the cycle: the same cycle replays to the same figures.
    again, _, _ = cycle_config(
        tmp_path / "again",
        envelope,
        [analyst_answer()],
        chains=chains(closes=RANGE_THEN_BREAK),
        **levels,
    )
    assert run_cycle(again).briefs["paths"]["take_first"] == paths["take_first"]
    from trading_research_agents.cli.cycle_report import _path_lines

    (line,) = _path_lines(paths)
    assert line.startswith("paths:     take first") and "bps net of the round trip" in line


def test_too_few_candles_leave_the_paths_unread_and_refuse_only_a_declared_floor(
    tmp_path: Path, envelope: Envelope
) -> None:
    from trading_research_agents.execution.targets import Targets

    cfg, _, _ = cycle_config(tmp_path, envelope, [analyst_answer()])
    result = run_cycle(cfg)
    assert result.acted and "at least 30" in result.briefs["paths"]["absent"]
    floor = Targets(min_path_ev_bps=Decimal(-1000))
    held, _, _ = cycle_config(tmp_path / "held", envelope, [analyst_answer()], targets=floor)
    refused = run_cycle(held)
    assert refused.intent is None
    assert any(
        r.startswith("paths: [targets].min_path_ev_bps is declared") for r in refused.refusals
    )


def test_a_declared_path_floor_refuses_a_trade_the_paths_do_not_pay(
    tmp_path: Path, envelope: Envelope
) -> None:
    from trading_research_agents.execution.targets import Targets

    cfg, _, _ = cycle_config(
        tmp_path,
        envelope,
        [analyst_answer()],
        chains=chains(closes=RANGE_THEN_BREAK),
        targets=Targets(min_path_ev_bps=Decimal(5000)),
    )
    result = run_cycle(cfg)
    assert result.intent is None
    assert any(r.startswith("paths:") and "min_path_ev_bps 5000" in r for r in result.refusals)


def test_check_names_a_declared_path_floor() -> None:
    from test_config import agent_raw
    from trading_research_agents.cli.check_gates import _target_notes
    from trading_research_agents.trading.config import parse_strategy_config

    cfg = parse_strategy_config(agent_raw(targets={"min_path_ev_bps": Decimal(2)}))
    assert cfg.targets.min_path_ev_bps == Decimal(2)
    assert any(
        line.startswith("paths: a trade worth less than 2 bps") for line in _target_notes(cfg, [])
    )


def test_a_candle_nobody_traded_in_is_an_empty_bar_and_the_brief_goes_on() -> None:
    from dataclasses import replace as replaced

    from trading_research_agents.agents.briefs import bars_of

    rows = candles(["100", "101", "99", "100", "102", "101", "103"])
    rows[3] = replaced(rows[3], volume=Decimal(0), trades=0)
    bars = bars_of(rows, 60)
    assert [b.empty for b in bars] == [False, False, False, True, False, False, False]
    snap = snapshot_with(closes=["100"])
    snap.values["ohlcv"] = replaced(snap.values["ohlcv"], value=rows)
    brief = structure_brief(snap, mechanism="reversion", interval_s=60, lookback_bars=5, now=NOW)
    assert brief.record()["close"] == "103"
