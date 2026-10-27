"""The tools each role holds: computed by code, carrying n, never a constant about the market."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research_agents.agents.auditor.tools import (
    audit_brief,
    evidence_check,
    run_vs_replay,
    usage_summary,
)
from trading_research_agents.agents.executor.tools import export_signals, rate_budget, size_for
from trading_research_agents.agents.market_screener.tools import screen_figures
from trading_research_agents.agents.risk_manager.tools import (
    alpha_decay,
    drawdown,
    measured_latency,
    refusal_pattern,
    risk_brief,
)
from trading_research_agents.agents.session import ScreenSettings, SessionConfig
from trading_research_agents.agents.session_run import run_session
from trading_research_agents.agents.structure_lead.tools import features
from trading_research_agents.cli.main import main
from trading_research_agents.data import Side, Trade
from trading_research_agents.data.series import Point, Series, SeriesSpec, Threshold
from trading_research_agents.envelope import Envelope
from trading_research_agents.llm import FakeCompleter
from trading_research_agents.llm.usage import BudgetLimits, MeteredCompleter, UsageLedger
from trading_research_agents.readings.macro import macro_brief, returns_from_closes
from trading_research_agents.readings.onchain import onchain_brief
from trading_research_agents.strategy import Bar
from trading_research_agents.trading import Ladder, load_strategy_config

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
MINUTE = 60 * 1_000_000_000
NS = 1_000_000_000


def trade(i: int, price: str, qty: str = "0.01", step_s: int = 30) -> Trade:
    return Trade(
        i * step_s * NS + 1, "x", "AAAUSDT", Decimal(price), Decimal(qty), Side.BUY, f"t{i}"
    )


def bar(i: int, o: str, h: str, lo: str, c: str, v: str = "1") -> Bar:
    return Bar(
        i * MINUTE, (i + 1) * MINUTE, Decimal(o), Decimal(h), Decimal(lo), Decimal(c), Decimal(v), 3
    )


# screener


def test_screen_figures_propose_a_regime_from_a_written_rule() -> None:
    trending = [trade(i, str(100 + i * 0.5)) for i in range(600)]
    f = screen_figures("AAAUSDT", trending, min_prints_per_minute=0.5, volatile_above_bps=500)
    assert f.rows == 600 and f.prints_per_minute == pytest.approx(2.0, rel=0.01)
    assert f.variance_ratio_1_10 is not None and f.variance_ratio_1_10 > 1
    assert f.proposed_regime == "trending" and "variance ratio" in f.regime_reason
    ranging = [trade(i, "100" if i % 2 == 0 else "101", step_s=60) for i in range(600)]
    r = screen_figures("AAAUSDT", ranging, min_prints_per_minute=0.5, volatile_above_bps=5000)
    assert r.proposed_regime == "ranging" and r.autocorr_lag1 is not None and r.autocorr_lag1 < 0
    thin = screen_figures(
        "AAAUSDT",
        [trade(i, "100", step_s=600) for i in range(20)],
        min_prints_per_minute=1.0,
        volatile_above_bps=50,
    )
    assert thin.proposed_regime == "illiquid" and "prints/min" in thin.regime_reason
    empty = screen_figures("AAAUSDT", [], min_prints_per_minute=1.0, volatile_above_bps=50)
    assert empty.rows == 0 and empty.proposed_regime == "illiquid"
    record = f.record()
    assert record["largest_gap_s"] == 30.0 and record["turnover"] == "6.00"


# structure lead


def test_features_come_from_closed_bars_only_and_carry_the_window() -> None:
    window = [bar(i, "100", str(101 + i), "99", str(100 + i)) for i in range(25)]
    f = features(window)
    assert f is not None and f.bars == 25 and f.close == Decimal("124")
    assert f.returns_bps[1] == pytest.approx((124 / 123 - 1) * 1e4)
    assert f.returns_bps[20] == pytest.approx((124 / 104 - 1) * 1e4)
    assert f.zscore is not None and f.zscore > 1.5
    assert f.position_in_range is not None and f.position_in_range > 0.9
    assert f.vwap_gap_bps is not None and f.vwap_gap_bps > 0
    assert f.volume_ratio == pytest.approx(1.0) and f.range_ratio is not None and f.range_ratio > 1
    row = f.record()
    assert set(row) >= {"ret_1_bps", "ret_5_bps", "ret_20_bps", "zscore", "vwap_gap_bps"}
    assert features([bar(0, "1", "1", "1", "1")]) is None
    short = features(window[:3])
    assert short is not None and short.returns_bps[5] is None and short.returns_bps[20] is None


# macro and on-chain briefs


def series(name: str, values: list[float], *, lag_s: int = 0, **threshold: float) -> Series:
    spec = SeriesSpec(
        name=name,
        file=f"{name}.csv",
        ts="t",
        ts_unit="s",
        value="v",
        unit="u",
        publish_lag=timedelta(seconds=lag_s),
        source="test-vendor",
        threshold=Threshold(**{k: Decimal(str(v)) for k, v in threshold.items()}),
    )
    points = tuple(
        Point(i * MINUTE, i * MINUTE + lag_s * NS, Decimal(str(v))) for i, v in enumerate(values)
    )
    return Series(spec, points)


def test_macro_brief_labels_measured_and_declared_and_hides_unpublished_values() -> None:
    times = [i * MINUTE for i in range(1, 31)]
    closes = [100.0 + i for i in range(30)]
    returns = returns_from_closes(times, closes)
    up = series("yield", [float(i + i % 3) for i in range(30)], bullish_below=5, bearish_above=20)
    lagged = series("late", [1.0] * 30, lag_s=3600 * 24)  # published a day later: unknown
    brief = macro_brief([up, lagged], at_ns=times[-1] + 1, times_ns=times, returns=returns)
    rows = {r["series"]: r for r in brief["series"]}
    assert rows["yield"]["basis"] == "operator-declared threshold"
    assert rows["yield"]["state"] == "bearish" and rows["yield"]["value"] == "31.0"
    corr = rows["yield"]["correlation_measured"]
    assert corr["n"] >= 25 and corr["window"].endswith("30 steps")
    assert (
        rows["yield"]["strongest_lead_lag"] is not None
        and "n" in rows["yield"]["strongest_lead_lag"]
    )
    assert rows["late"]["state"] == "unknown" and rows["late"]["points_known"] == 0
    assert "measured" in brief["basis"] and "declared" in brief["basis"]
    assert brief["events"] == []


def test_onchain_brief_carries_net_flow_and_zscore() -> None:
    times = [i * MINUTE for i in range(1, 31)]
    closes = [100.0] * 30
    flow = series("netflow", [1.0] * 29 + [30.0])
    brief = onchain_brief(
        [flow],
        at_ns=times[-1] + 1,
        times_ns=times,
        returns=returns_from_closes(times, closes),
        flow_window=timedelta(minutes=5),
    )
    row = brief["series"][0]
    assert row["net_over_window"]["value"] == 33.0 and row["net_over_window"]["n"] == 4
    assert row["zscore_vs_recent"]["value"] is not None and row["zscore_vs_recent"]["value"] > 2
    assert row["state"] == "undeclared" and row["correlation_measured"]["value"] is None


# risk manager


def run_dir(tmp_path: Path, nets: list[str], *, latency_ms: list[int], refusals: list[str]) -> Path:
    out = tmp_path / "run"
    out.mkdir(parents=True, exist_ok=True)
    intents, fills, trades = [], [], []
    t = 0
    for net, lat in zip(nets, latency_ms, strict=True):
        intents.append(
            {
                "ts_ns": t,
                "target": "long",
                "size": "1",
                "reason": "test",
                "risk_bps": "1",
                "admitted": True,
                "decision": "within limits",
            }
        )
        fills.append(
            {
                "ts_ns": t + lat * 1_000_000,
                "price": "100",
                "size": "1",
                "direction": "long",
                "closes": False,
            }
        )
        trades.append(
            {"entry_ts_ns": t, "exit_ts_ns": t + MINUTE, "net_bps": net, "instrument": "X"}
        )
        t += 2 * MINUTE
    for r in refusals:
        intents.append({"ts_ns": t, "target": "long", "admitted": False, "decision": r})
    (out / "intents.jsonl").write_text("".join(json.dumps(x) + "\n" for x in intents), "utf-8")
    (out / "fills.jsonl").write_text("".join(json.dumps(x) + "\n" for x in fills), "utf-8")
    (out / "trades.jsonl").write_text("".join(json.dumps(x) + "\n" for x in trades), "utf-8")
    return out


def test_risk_tools_measure_latency_decay_drawdown_and_refusals(tmp_path: Path) -> None:
    out = run_dir(
        tmp_path,
        ["10", "-30", "5", "-40"],
        latency_ms=[1200, 900, 1500, 1100],
        refusals=[
            "kill switch armed",
            "risk 60 bps exceeds max 50 bps",
            "risk 70 bps exceeds max 50 bps",
        ],
    )
    lat = measured_latency(out, declared_ms=Decimal("6"))
    assert lat.n == 4 and lat.median_ms == 1200.0 and lat.max_ms == 1500.0
    assert lat.ratio_to_declared == pytest.approx(200.0)
    decay = alpha_decay(out, claimed_bps=Decimal("20"), window=2)
    assert (
        decay.round_trips == 4 and decay.rolling_mean_bps == Decimal("-17.5") and decay.below_claim
    )
    dd = drawdown(out)
    assert dd["max_drawdown_bps"] == "-65" and dd["final_bps"] == "-55"
    pattern = refusal_pattern(out)
    assert (
        pattern[0] == {"control": "risk 60 bps exceeds max 50 bps", "refusals": 1}
        or pattern[0]["refusals"] >= 1
    )
    brief = risk_brief([("X", out)], declared_ms=Decimal("6"), claimed_bps=Decimal("20"))
    assert brief["X"]["latency"]["ratio_to_declared"] == 200.0
    empty = measured_latency(tmp_path / "nowhere", declared_ms=Decimal("6"))
    assert empty.n == 0 and empty.median_ms is None


# executor


def test_executor_tools_size_within_caps_and_export_signals(tmp_path: Path) -> None:
    s = size_for(
        wanted=Decimal("1"),
        price=Decimal("100"),
        max_position=Decimal("0.5"),
        max_notional=Decimal("30"),
        step=Decimal("0.01"),
    )
    assert s.size == Decimal("0.30") and s.capped_by == "max_notional"
    s2 = size_for(
        wanted=Decimal("0.2"),
        price=Decimal("100"),
        max_position=Decimal("0.5"),
        max_notional=None,
        step=Decimal("0.01"),
    )
    assert s2.size == Decimal("0.20") and s2.capped_by == "declared size"
    tiny = size_for(
        wanted=Decimal("0.004"),
        price=Decimal("100"),
        max_position=Decimal("1"),
        max_notional=None,
        step=Decimal("0.01"),
    )
    assert tiny.size == 0 and "below one step" in tiny.capped_by
    with pytest.raises(ValueError, match="positive"):
        size_for(
            wanted=Decimal("0"),
            price=Decimal("1"),
            max_position=Decimal("1"),
            max_notional=None,
            step=Decimal("1"),
        )
    budget = rate_budget([0, 10 * NS, 50 * NS], now_ns=55 * NS, per_minute=3)
    assert budget["left"] == 0 and budget["frees_in_s"] == pytest.approx(5.0)
    assert rate_budget([], now_ns=0, per_minute=6)["left"] == 6

    out = run_dir(tmp_path, ["1"], latency_ms=[10], refusals=["kill switch armed"])
    (out / "run.json").write_text(json.dumps({"instrument": "AAAUSDT", "label": "replay"}), "utf-8")
    n = export_signals(out, instrument="AAAUSDT", out=out / "signals.jsonl")
    rows = [json.loads(line) for line in (out / "signals.jsonl").read_text("utf-8").splitlines()]
    assert n == 1 and rows[0]["instrument"] == "AAAUSDT" and rows[0]["target"] == "long"
    n_csv = export_signals(out, instrument="AAAUSDT", out=out / "signals.csv", fmt="csv")
    assert n_csv == 1 and (out / "signals.csv").read_text("utf-8").startswith(
        "ts_ns,instrument,target"
    )
    with pytest.raises(ValueError, match="jsonl or csv"):
        export_signals(out, instrument="X", out=out / "x", fmt="xml")


def test_signals_export_from_the_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = run_dir(tmp_path, ["1", "2"], latency_ms=[10, 10], refusals=[])
    (out / "run.json").write_text(json.dumps({"instrument": "AAAUSDT"}), "utf-8")
    assert main(["signals", "export", str(out), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["signals"] == 2 and payload["out"].endswith("signals.jsonl")
    assert main(["signals", "export", str(tmp_path)]) == 1
    assert "not a run directory" in capsys.readouterr().err


# auditor


def test_auditor_tools_total_usage_compare_runs_and_check_evidence(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.jsonl")
    metered = MeteredCompleter(
        FakeCompleter(['{"a":1}', '{"a":2}']),
        role="structure_lead",
        provider="fake",
        limits=BudgetLimits(token_budget=10_000, max_calls_per_hour=100, min_interval_s=0),
        ledger=ledger,
        clock=lambda: 0.0,
    )
    metered.complete(system="s", user="u" * 20, schema={})
    metered.complete(system="s", user="u" * 20, schema={})
    summary = usage_summary(tmp_path / "usage.jsonl", round_trips=2)
    assert summary["calls"] == 2 and summary["tokens_per_round_trip"] == summary["tokens"] // 2
    assert summary["by_role"]["structure_lead"]["mean_latency_ms"] is not None

    live = run_dir(tmp_path / "live", ["10", "-5"], latency_ms=[1, 1], refusals=[])
    replay = run_dir(tmp_path / "replay", ["12", "-5"], latency_ms=[1, 1], refusals=[])
    cmp = run_vs_replay(live, replay)
    assert (
        cmp["matched"] == 2
        and cmp["net_gap_bps_total"] == "-2"
        and cmp["net_gap_bps_per_matched"] == "-1"
    )

    ladder = Ladder(tmp_path / "ladder.json", live_available=True)
    ladder.promote(evidence="layer 5 OPEN on backtest-1", signed_by="op")
    ladder.promote(evidence="felt right", signed_by="op")
    checks = evidence_check(tmp_path / "ladder.json")
    assert [c["names_a_gate"] for c in checks] == [True, False]
    brief = audit_brief(
        usage_path=tmp_path / "usage.jsonl",
        round_trips=2,
        run_dir=live,
        replay_dir=replay,
        ladder_path=tmp_path / "ladder.json",
    )
    assert brief["run_vs_replay"]["matched"] == 2 and len(brief["promotions"]) == 2


# the session with the tools in place


def test_a_session_hands_the_roles_measured_briefs(tmp_path: Path, envelope: Envelope) -> None:
    pytest.importorskip("pyarrow")
    from trading_research_agents.data.parquet import write_trades

    rows = [
        Trade(
            i * MINUTE + 1,
            "bybit",
            "AAAUSDT",
            Decimal(100 + i % 3),
            Decimal("0.01"),
            Side.BUY,
            f"t{i}",
        )
        for i in range(8)
    ]
    parquet = tmp_path / "AAAUSDT.parquet"
    write_trades(rows, parquet, price_scale=2, qty_scale=3)
    (tmp_path / "series.toml").write_text(
        '[[series]]\nname = "yield"\nfile = "yield.csv"\nts = "date"\nvalue = "value"\n'
        'unit = "percent"\nthreshold = { bullish_below = 3.5 }\n',
        "utf-8",
    )
    (tmp_path / "yield.csv").write_text("date,value\n1970-01-01T00:00:00Z,3.0\n", "utf-8")
    (tmp_path / "events.json").write_text(
        '[{"when": "1970-01-01T00:05:00Z", "what": "release"}]', "utf-8"
    )
    text = (EXAMPLES / "strategy.toml").read_text("utf-8")
    start, end = text.index("[strategy]"), text.index("[exit]")
    config = tmp_path / "strategy.toml"
    config.write_text(
        text[:start]
        + '[strategy]\nagent = "structure_lead"\n\n'
        + text[end:]
        + '\n[agents]\nprovider = "local"\nbase_url = "http://localhost:1/v1"\nmodel = "m"\n'
        'mechanism = "m"\n'
        "token_budget = 500000\nmax_calls_per_hour = 1000\nmin_interval_s = 0\nlookback_bars = 4\n"
        "[agents.screen]\nmin_prints_per_minute = 0.1\nvolatile_above_bps = 500\n",
        "utf-8",
    )
    cfg = load_strategy_config(config)
    assert cfg.agents is not None and cfg.agents.screen == {
        "min_prints_per_minute": 0.1,
        "volatile_above_bps": 500.0,
    }
    screener = {
        "kept": [{"instrument": "AAAUSDT", "regime": "ranging", "reason": "as proposed"}],
        "excluded": [],
    }
    analyst = {
        "target": "flat",
        "setup": "no_setup",
        "reason": "r",
        "figures_used": [],
        "confidence": 0.9,
        "horizon_bars": 2,
    }
    risk = {"decision": "continue", "limits": {}, "reason": "fine", "triggers": []}
    alloc = {"size": "0.01", "reason": "r", "allocations": []}
    audit = {
        "action": "answer",
        "tool": "",
        "arguments": {},
        "answer": {"findings": [], "verdict": "sound", "summary": "ok"},
        "thought": "done",
    }
    fake = FakeCompleter([screener, *([analyst] * 6), risk, alloc, audit])
    usage = UsageLedger(tmp_path / "out" / "usage.jsonl")
    limits = BudgetLimits(token_budget=500000, max_calls_per_hour=1000, min_interval_s=0)
    metered: dict[str, MeteredCompleter] = {}

    def completer_for(role: str) -> MeteredCompleter:
        if role not in metered:
            metered[role] = MeteredCompleter(
                fake, role=role, provider="fake", limits=limits, ledger=usage, clock=lambda: 0.0
            )
        return metered[role]

    result = run_session(
        SessionConfig(
            strategy=cfg,
            envelope=envelope,
            datasets={"AAAUSDT": parquet},
            out_dir=tmp_path / "out",
            completer_for=completer_for,
            usage=usage,
            magnitude_bps=Decimal("20"),
            ladder_path=tmp_path / "ladder.json",
            macro_series=tmp_path / "series.toml",
            events=tmp_path / "events.json",
            screen=ScreenSettings(**cfg.agents.screen),
        )
    )
    # The macro role is read by code: its declared state, and no model asked for it.
    assert result.macro is not None and result.macro["read_by"] == "code"
    assert result.macro["declared_state"]["declared"] == "bullish"
    assert not any("macro" in call.system[:200].lower() for call in fake.calls)
    figures = json.loads((tmp_path / "out" / "figures.json").read_text("utf-8"))[0]
    assert (
        figures["proposed_regime"] in ("ranging", "trending", "illiquid", "volatile")
        and "regime_reason" in figures
    )
    brief = json.loads((tmp_path / "out" / "macro-brief.json").read_text("utf-8"))
    assert (
        brief["series"][0]["basis"] == "operator-declared threshold"
        and brief["series"][0]["state"] == "bullish"
    )
    assert brief["events"] == [] or brief["events"][0]["what"] == "release"
    risk_user = json.loads(fake.calls[-3].user)
    assert "measured" in risk_user and "latency" in risk_user["measured"]["AAAUSDT"]
    audit_user = json.loads(fake.calls[-1].user)["question"]
    assert "usage" in audit_user["measured"] and "promotions" in audit_user["measured"]
