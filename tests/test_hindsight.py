"""Hindsight: each gate's refusals replayed beside what it let through.

The replay is the part that can be wrong quietly, so it is pinned bar by bar: the take, the stop,
both in one bar, the hold limit, and a path that does not cover the hold. The grouping is pinned on
the records a cycle actually writes, and the verdicts on samples built to be clear or too small.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.agents.quant_auditor.tools import desk_round_trips, quant_brief
from trading_research_agents.data.schema import Candle
from trading_research_agents.execution.hindsight import (
    ADMITTED,
    Decision,
    Outcome,
    decision_of,
    gate_of,
    levels_check,
    outcomes,
    price_path,
    replay,
    report,
    side_check,
)

AT = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
BAR = 300
NS = 1_000_000_000


def bar(i: int, o: str, h: str, low: str, c: str) -> Candle:
    start = int(AT.timestamp()) * NS + i * BAR * NS
    return Candle(start, Decimal(o), Decimal(h), Decimal(low), Decimal(c), Decimal(1))


def decision(
    side: int = 1, stop: str | None = "100", take: str | None = "200", **kw: Any
) -> Decision:
    values: dict[str, Any] = {
        "cycle": "c1",
        "instrument": "BTCUSDT",
        "at": AT,
        "side": side,
        "gate": ADMITTED,
        "stop_bps": None if stop is None else Decimal(stop),
        "take_bps": None if take is None else Decimal(take),
        "round_trip_bps": Decimal("10"),
    }
    values.update(kw)
    return Decision(**values)


def flat(count: int, price: str = "100", start: int = 0) -> list[Candle]:
    return [bar(i, price, price, price, price) for i in range(start, start + count)]


# -- the replay ---------------------------------------------------------------------------------


def test_a_long_reaches_its_take_and_books_exactly_the_take() -> None:
    path = [bar(0, "100", "101", "99.5", "100.5"), bar(1, "100.5", "102.5", "100.4", "102")]
    path += flat(10, "102", start=2)
    assert replay(decision(), path, hold_s=3600, bar_s=BAR) == (Decimal("200"), "take")


def test_a_bar_that_reaches_both_levels_counts_as_the_stop() -> None:
    path = [bar(0, "100", "103", "98", "100"), *flat(11, start=1)]
    assert replay(decision(), path, hold_s=3600, bar_s=BAR) == (Decimal("-100"), "stop")


def test_a_short_is_the_mirror_of_a_long() -> None:
    path = [bar(0, "100", "100.2", "97.9", "98"), *flat(11, "98", start=1)]
    assert replay(decision(side=-1), path, hold_s=3600, bar_s=BAR) == (Decimal("200"), "take")
    up = [bar(0, "100", "101.1", "100", "101"), *flat(11, "101", start=1)]
    assert replay(decision(side=-1), up, hold_s=3600, bar_s=BAR) == (Decimal("-100"), "stop")


def test_with_no_levels_it_closes_at_the_hold_limit() -> None:
    path = [*flat(3), bar(3, "100", "100.6", "100", "100.5")]
    mine = replay(decision(stop=None, take=None), path, hold_s=4 * BAR, bar_s=BAR)
    assert mine == (Decimal("50"), "hold")
    # The same bars on the other side are the same move, signed the other way.
    other = replay(decision(stop=None, take=None), path, hold_s=4 * BAR, bar_s=BAR, side=-1)
    assert other == (Decimal("-50"), "hold")


def test_a_path_that_does_not_cover_the_hold_is_not_replayed() -> None:
    assert replay(decision(), flat(5), hold_s=3600, bar_s=BAR) is None
    late = [bar(i + 2, "100", "100", "100", "100") for i in range(12)]
    assert replay(decision(), late, hold_s=3600, bar_s=BAR) is None
    assert replay(decision(), [], hold_s=3600, bar_s=BAR) is None


def test_the_path_is_fetched_a_page_at_a_time_from_the_decision() -> None:
    every = flat(30)
    asked: list[int] = []

    def fetch(instrument: str, since_ms: int, limit: int) -> list[Candle]:
        asked.append(since_ms)
        return [c for c in every if c.start_ns >= since_ms * 1_000_000][:limit]

    got = price_path(fetch, decision(), hold_s=3600, bar_s=BAR, page=5)
    # The hold and the bar it closes on, so a page past the end is not asked for.
    assert len(got) >= 13 and got[0].start_ns == every[0].start_ns
    assert asked[0] == int(AT.timestamp()) * 1000 and len(asked) == 3


# -- what a cycle record says -------------------------------------------------------------------


def record(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "cycle_id": "2026-10-10T12-00-00Z-BTCUSDT",
        "instrument": "BTCUSDT",
        "finished_at": AT.isoformat(),
        "analyst": {"target": "long", "stop_price": "99", "target_price": "103"},
        "seen": {"mid": "100"},
        "refusals": [],
        "held": False,
    }
    base.update(kw)
    return base


@pytest.mark.parametrize(
    ("fields", "gate"),
    [
        ({"intent": {"mid": "100"}, "placed": {"sent": True}}, ADMITTED),
        ({"intent": {"mid": "100"}}, ADMITTED),
        (
            {
                "intent": {"mid": "100"},
                "placed": {"sent": False},
                "refusals": ["venue: not sent, x"],
            },
            "venue",
        ),
        ({"stopped_by": "cost at 0.01: depth"}, "cost"),
        ({"refusals": ["data funding: stale", "cost gate: net below zero"]}, "cost"),
        ({"refusals": ["validator_pro: no model answered"]}, "validators"),
        (
            {"refusals": ["macro_analyst usd_broad: shown, not counted", "regime: fading a trend"]},
            "regime",
        ),
        ({}, "no reason recorded"),
    ],
)
def test_the_gate_is_the_one_that_stopped_the_cycle(fields: dict[str, Any], gate: str) -> None:
    assert gate_of(record(**fields)) == gate


def test_levels_come_from_the_sent_intent_then_the_exits_then_the_analyst() -> None:
    sent = decision_of(record(intent={"mid": "100", "stop_price": "98", "take_price": "104"}))
    assert sent is not None and (sent.stop_bps, sent.take_bps) == (Decimal(200), Decimal(400))
    exits = decision_of(record(exits={"entry": "100", "stop_price": "99.5", "take_price": "101"}))
    assert exits is not None and (exits.stop_bps, exits.take_bps) == (Decimal(50), Decimal(100))
    named = decision_of(record())
    assert named is not None and (named.stop_bps, named.take_bps) == (Decimal(100), Decimal(300))


def test_levels_on_the_wrong_side_are_not_used_and_no_side_is_no_decision() -> None:
    wrong = decision_of(
        record(analyst={"target": "short", "stop_price": "99", "target_price": "103"})
    )
    assert wrong is not None and wrong.side == -1 and wrong.stop_bps is None
    assert decision_of(record(analyst={"target": "flat"})) is None
    assert decision_of(record(held=True)) is None


def test_the_round_trip_is_the_one_the_cycle_priced() -> None:
    gated = decision_of(record(cost={"cost": {"round_trip_bps": "12.5"}}))
    assert gated is not None and gated.round_trip_bps == Decimal("12.5")
    early = decision_of(record(), {"round_trip_bps": "11"})
    assert early is not None and early.round_trip_bps == Decimal("11")


# -- replaying a desk ---------------------------------------------------------------------------


def test_a_hold_not_over_waits_and_a_replayed_path_is_kept(tmp_path: Path) -> None:
    calls = []

    def fetch(instrument: str, since_ms: int, limit: int) -> list[Candle]:
        calls.append(since_ms)
        return [c for c in flat(20) if c.start_ns >= since_ms * 1_000_000][:limit]

    old = decision(cycle="old", round_trip_bps=None)
    new = decision(cycle="new", at=AT + timedelta(hours=10))
    now = AT + timedelta(hours=10, minutes=30)
    found, skipped = outcomes([old, new], fetch, hold_s=3600, bar_s=BAR, cache=tmp_path, now=now)
    assert [o.decision.cycle for o in found] == ["old"] and skipped["hold not over"] == 1
    # No round trip on the record: the median of the ones priced stands in.
    assert found[0].round_trip_bps == Decimal("10") and found[0].net_bps == Decimal("-10")
    assert (tmp_path / "old.json").exists()

    calls.clear()
    again, _ = outcomes([old], fetch, hold_s=3600, bar_s=BAR, cache=tmp_path, now=now)
    assert calls == [] and again[0].gross_bps == found[0].gross_bps


def test_a_kept_path_for_another_hold_or_a_broken_file_is_fetched_again(tmp_path: Path) -> None:
    calls = []

    def fetch(instrument: str, since_ms: int, limit: int) -> list[Candle]:
        calls.append(since_ms)
        return [c for c in flat(30) if c.start_ns >= since_ms * 1_000_000][:limit]

    now = AT + timedelta(hours=5)
    (tmp_path / "c1.json").write_text("{not json", encoding="utf-8")
    found, _ = outcomes([decision()], fetch, hold_s=3600, bar_s=BAR, cache=tmp_path, now=now)
    assert len(found) == 1 and calls
    calls.clear()
    longer, _ = outcomes([decision()], fetch, hold_s=7200, bar_s=BAR, cache=tmp_path, now=now)
    assert len(longer) == 1 and calls
    assert json.loads((tmp_path / "c1.json").read_text(encoding="utf-8"))["hold_s"] == 7200


def outcome(gate: str, net: float, opposite: float = 0.0, day: int = 0, n: int = 0) -> Outcome:
    d = decision(cycle=f"{gate}{day}{n}", gate=gate, at=AT + timedelta(days=day, minutes=n))
    return Outcome(d, Decimal(str(net)) + 10, "hold", Decimal(str(opposite)) + 10, Decimal(10))


def test_a_small_sample_is_too_few_to_tell() -> None:
    found = [outcome(ADMITTED, 5), outcome("regime", -50), outcome("regime", -60)]
    payload = report(found, hold_s=3600, bar_s=BAR, skipped={})
    assert payload["gates"][0]["verdict"] == "too few to tell"
    assert payload["side"]["verdict"] == "too few to tell"


def test_a_gate_that_refused_worse_and_one_that_refused_better_are_both_named() -> None:
    found = [
        outcome(ADMITTED, x, n=i) for i, x in enumerate([5, -5, 10, -10, 8, -8, 12, 0, 3, -3, 6])
    ]
    found += [outcome("targets", x, n=i) for i, x in enumerate(range(-90, -79))]
    found += [outcome("regime", x, n=i) for i, x in enumerate(range(80, 91))]
    gates = {g["gate"]: g for g in report(found, hold_s=3600, bar_s=BAR, skipped={})["gates"]}
    assert gates["targets"]["verdict"] == "refused worse trades than it let through"
    assert gates["regime"]["verdict"].startswith("refused better trades")
    assert gates["targets"]["decisions"] == 11 and gates["targets"]["sum_net_bps"] == "-935.0"


def test_a_model_whose_reverse_wins_day_after_day_is_told_so() -> None:
    found = [outcome(ADMITTED, -40 + i, 30, day=d, n=i) for d in range(6) for i in range(3)]
    side = side_check(found)
    assert side["days"] == 6 and side["verdict"].startswith("the reverse of the model's side")
    assert side["model_side"]["mean_net_bps"] == "-39.0"


# -- the quant auditor reads what the venue closed ----------------------------------------------


def test_the_quant_auditor_sees_the_desks_own_round_trips(tmp_path: Path) -> None:
    from trading_research_agents.agents.audits import read_cycles

    lines = [
        {"cycle": "a", "entry": "100", "size": "2", "net": "-0.5"},
        {"cycle": "b", "net": "1.2", "net_bps": "60.0", "entry": "100", "size": "2"},
        {"cycle": "c", "note": "the venue returned no exit fills"},
    ]
    (tmp_path / "round_trips.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines), encoding="utf-8"
    )
    trips = desk_round_trips(tmp_path)
    assert [t["cycle"] for t in trips] == ["a", "b"] and Decimal(trips[0]["net_bps"]) == -25
    (tmp_path / "hindsight.json").write_text(json.dumps({"replayed": 3, "side": {}}), "utf-8")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "cycle.json").write_text(json.dumps(record()), encoding="utf-8")

    brief = quant_brief(read_cycles(tmp_path), desk=tmp_path)
    assert brief["round_trips"]["closed"] == 2
    assert brief["round_trips"]["worst_realised_net_bps"] == -25.0
    assert brief["hindsight"] == {"replayed": 3, "side": {}}


# -- the command --------------------------------------------------------------------------------


def test_tra_hindsight_replays_a_desk_and_writes_its_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from trading_research_agents.cli.main import main
    from trading_research_agents.data.sources.base import Fetched
    from trading_research_agents.data.sources.ccxt_source import CcxtSource

    target = tmp_path / "desk"
    assert main(["init", str(target), "--profile", "autonomous"]) == 0
    monkeypatch.chdir(target)
    capsys.readouterr()
    cycles = target / "runs" / "cycles"
    written: list[tuple[dict[str, Any], str]] = [
        ({"intent": {"mid": "100"}, "placed": {"sent": True}}, "long"),
        ({"refusals": ["regime: fading a trend"]}, "short"),
    ]
    for i, (gate_fields, target_side) in enumerate(written):
        folder = cycles / f"2026-10-10T12-0{i}-00Z-BTCUSDT"
        folder.mkdir(parents=True)
        rec = record(
            cycle_id=folder.name,
            finished_at=(AT + timedelta(minutes=i)).isoformat(),
            analyst={"target": target_side},
            **gate_fields,
        )
        (folder / "cycle.json").write_text(json.dumps(rec), encoding="utf-8")

    def fetch(self: CcxtSource, kind: str, instrument: str, **options: Any) -> Fetched:
        since = options["since_ms"] * 1_000_000
        bars = [bar(i, "100", "100.5", "100", "100.5") for i in range(80)]
        bars = [c for c in bars if c.start_ns >= since][: options["limit"]]
        return Fetched("ohlcv", instrument, bars, self.name, "free", 1.0, bars[-1].start_ns)

    monkeypatch.setattr(CcxtSource, "fetch", fetch)
    assert main(["hindsight", "--hold", "1h", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["replayed"] == 2 and payload["admitted"]["decisions"] == 1
    assert payload["gates"][0]["gate"] == "regime"
    assert (cycles / "hindsight.json").exists() and len(list((cycles / "hindsight").iterdir())) == 2


# -- the analyst's own levels beside the ones code applied --------------------------------------


def test_the_analysts_own_levels_are_kept_beside_the_ones_code_applied() -> None:
    held = decision_of(record(intent={"mid": "100", "stop_price": "98", "take_price": "104"}))
    assert held is not None and (held.stop_bps, held.take_bps) == (Decimal(200), Decimal(400))
    assert (held.own_stop_bps, held.own_take_bps) == (Decimal(100), Decimal(300))


def test_both_sets_of_levels_are_replayed_on_the_same_path() -> None:
    # A dip of 60 bps takes out the analyst's own 50 bps stop; code's 100 bps one stands.
    path = [bar(0, "100", "100.1", "99.4", "100"), *flat(11, start=1)]

    def fetch(instrument: str, since_ms: int, limit: int) -> list[Candle]:
        return [c for c in path if c.start_ns >= since_ms * 1_000_000][:limit]

    named = decision(own_stop_bps=Decimal(50), own_take_bps=Decimal(400))
    found, _ = outcomes([named], fetch, hold_s=3600, bar_s=BAR, now=AT + timedelta(hours=5))
    assert found[0].gross_bps == Decimal(0) and found[0].own_gross_bps == Decimal(-50)
    assert found[0].own_net_bps == Decimal(-60)


def test_levels_that_beat_the_analysts_own_day_after_day_are_named() -> None:
    found = []
    for d in range(6):
        for i in range(3):
            o = outcome(ADMITTED, 20 + i, day=d, n=i)
            found.append(
                Outcome(
                    o.decision,
                    o.gross_bps,
                    "hold",
                    o.opposite_gross_bps,
                    o.round_trip_bps,
                    own_gross_bps=o.gross_bps - 30,
                )
            )
    levels = levels_check(found)
    assert levels["verdict"] == "the levels code applied did better than the analyst's own"
    assert levels["difference_bps"] == "30.0" and levels["days"] == 6
    assert levels_check(found[:4])["verdict"] == "too few to tell"
