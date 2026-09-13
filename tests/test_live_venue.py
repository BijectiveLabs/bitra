"""The security review of the live tier, executed rather than described.

A local double of the venue's v5 REST verifies every signature with the documented recipe,
serves scripted key, clock, position and order states, and records what the adapter sent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from trading_research_agent.data import Direction, Side, Trade
from trading_research_agent.envelope import Envelope
from trading_research_agent.execution import FundingSchedule
from trading_research_agent.strategy import Bar, ExitRule, Signal, Target
from trading_research_agent.trading import (
    Controls,
    KillSwitch,
    Limits,
    RecordedFeed,
    TradingConfig,
    VenueError,
    run,
)
from trading_research_agent.trading.canary import CanaryGuard, CanaryLimits
from trading_research_agent.trading.venue import Intent
from trading_research_agent.trading.venues import (
    LiveVenueSettings,
    UnknownLiveVenueError,
    live_venue,
    live_venues,
)
from trading_research_agent.trading.venues.bybit import BybitApiError, BybitLiveVenue, BybitRest

KEY = "key-0123456789abcdef"
SECRET = "secret-that-must-never-appear-anywhere"  # noqa: S105 - a test double's credential
MINUTE = 60 * 1_000_000_000


@dataclass
class VenueState:
    """What the double believes, and what it was told to do."""

    now_ms: int = 1_735_689_600_000
    key_info: dict[str, Any] = field(
        default_factory=lambda: {
            "readOnly": 0,
            "permissions": {"ContractTrade": ["Order", "Position"], "Wallet": []},
            "ips": ["203.0.113.7"],
            "expiredAt": "2027-01-01T00:00:00Z",
        }
    )
    position: Decimal = Decimal(0)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    creates: list[dict[str, Any]] = field(default_factory=list)
    fill_fraction: Decimal = Decimal(1)
    fee_bps: Decimal = Decimal("5.5")
    price: Decimal = Decimal("100")
    drift_position_after_fill: Decimal = Decimal(0)
    lose_first_create: bool = False
    rate_limit_once: bool = False
    bad_signatures: int = 0
    requests: list[str] = field(default_factory=list)


def handler_for(state: VenueState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _reply(
            self, code: int, payload: dict[str, Any], headers: dict[str, str] | None = None
        ) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _ok(self, result: dict[str, Any]) -> None:
            self._reply(200, {"retCode": 0, "retMsg": "OK", "result": result})

        def _err(self, code: int, msg: str) -> None:
            self._reply(200, {"retCode": code, "retMsg": msg, "result": {}})

        def _verify(self, payload: str) -> bool:
            key = self.headers.get("X-BAPI-API-KEY", "")
            ts = self.headers.get("X-BAPI-TIMESTAMP", "")
            window = self.headers.get("X-BAPI-RECV-WINDOW", "")
            sign = self.headers.get("X-BAPI-SIGN", "")
            if key != KEY:
                self._err(10003, "API key is invalid")
                return False
            expected = hmac.new(
                SECRET.encode(), f"{ts}{key}{window}{payload}".encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, sign):
                state.bad_signatures += 1
                self._err(10004, "error sign")
                return False
            skew = int(ts) - state.now_ms
            if not (-int(window) <= skew < 1000):
                self._err(10002, "request time exceeds the time window")
                return False
            return True

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            state.requests.append(f"GET {parts.path}")
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            if parts.path == "/v5/market/time":
                self._ok(
                    {
                        "timeSecond": str(state.now_ms // 1000),
                        "timeNano": str(state.now_ms * 1_000_000),
                    }
                )
                return
            if not self._verify(parts.query):
                return
            if parts.path == "/v5/user/query-api":
                self._ok(dict(state.key_info))
            elif parts.path == "/v5/position/list":
                size = abs(state.position)
                side = "" if size == 0 else ("Buy" if state.position > 0 else "Sell")
                self._ok(
                    {"list": [{"symbol": query.get("symbol"), "size": str(size), "side": side}]}
                )
            elif parts.path == "/v5/order/realtime":
                order = state.orders.get(query.get("orderLinkId", ""))
                self._ok({"list": [order] if order else []})
            else:
                self._reply(404, {"retCode": 404, "retMsg": "no such path"})

        def do_POST(self) -> None:
            parts = urlsplit(self.path)
            state.requests.append(f"POST {parts.path}")
            length = int(self.headers.get("Content-Length", "0"))
            text = self.rfile.read(length).decode()
            if not self._verify(text):
                return
            if parts.path != "/v5/order/create":
                self._reply(404, {"retCode": 404, "retMsg": "no such path"})
                return
            body = json.loads(text)
            state.creates.append(body)
            link = body["orderLinkId"]
            if state.rate_limit_once:
                state.rate_limit_once = False
                self._reply(
                    200,
                    {"retCode": 10006, "retMsg": "Too many visits!", "result": {}},
                    {"X-Bapi-Limit-Reset-Timestamp": str(state.now_ms + 200)},
                )
                return
            if link in state.orders:
                self._err(110030, "Duplicate orderId")
                return
            qty = Decimal(body["qty"])
            sign = Decimal(1) if body["side"] == "Buy" else Decimal(-1)
            if body.get("reduceOnly") and (
                state.position == 0 or (state.position > 0) == (sign > 0)
            ):
                self._err(110023, "Currently you can only reduce your position on this contract")
                return
            done = (qty * state.fill_fraction).quantize(Decimal("0.001"))
            state.position += sign * done + state.drift_position_after_fill
            state.orders[link] = {
                "orderLinkId": link,
                "orderId": f"oid-{len(state.orders) + 1}",
                "orderStatus": "Filled" if done == qty else "PartiallyFilledCanceled",
                "cumExecQty": str(done),
                "avgPrice": str(state.price),
                "cumExecFee": str(state.price * done * state.fee_bps / Decimal(10_000)),
                "updatedTime": str(state.now_ms + 5),
            }
            if state.lose_first_create:
                # The order landed but the response never made it back to the client.
                state.lose_first_create = False
                self.send_response(502)
                self.end_headers()
                return
            self._ok({"orderId": state.orders[link]["orderId"], "orderLinkId": link})

    return Handler


@pytest.fixture
def double() -> Iterator[tuple[VenueState, str]]:
    state = VenueState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def settings(envelope: Envelope, **overrides: Any) -> LiveVenueSettings:
    channel = next(c for c in envelope.channels if c.key == "bybit/taker-taker")
    base: dict[str, Any] = {
        "instrument": "BTCUSDT",
        "channel": channel,
        "funding": FundingSchedule(interval_s=28800, rates={}),
        "environment": "demo",
        "run_id": "0123456789abcdef",
        "poll_interval_ms": 0,
    }
    base.update(overrides)
    return LiveVenueSettings(**base)


def venue(state: VenueState, url: str, envelope: Envelope, **overrides: Any) -> BybitLiveVenue:
    clock = lambda: state.now_ms / 1000  # noqa: E731 - the double's clock is the test's clock
    rest = BybitRest(
        base_url=url, api_key=KEY, api_secret=SECRET, clock=clock, sleep=lambda s: None
    )
    return BybitLiveVenue(rest, settings(envelope, **overrides), clock=clock)


def print_(i: int, price: str = "100") -> Trade:
    return Trade(
        i * MINUTE + 1, "bybit", "BTCUSDT", Decimal(price), Decimal("0.01"), Side.BUY, f"t{i}"
    )


def intent(direction: Direction, size: str = "0.01") -> Intent:
    return Intent(1, "BTCUSDT", direction, Decimal(size), "test")


# S1..S3: the transport and the preflight


def test_signatures_follow_the_documented_recipe_and_a_bad_secret_is_refused(
    double: tuple[VenueState, str],
) -> None:
    state, url = double
    clock = lambda: state.now_ms / 1000  # noqa: E731
    good = BybitRest(base_url=url, api_key=KEY, api_secret=SECRET, clock=clock)
    assert good.get("/v5/user/query-api", {})["readOnly"] == 0
    assert state.bad_signatures == 0
    bad = BybitRest(base_url=url, api_key=KEY, api_secret="wrong", clock=clock)  # noqa: S106
    with pytest.raises(BybitApiError, match="retCode 10004"):
        bad.get("/v5/user/query-api", {})
    assert state.bad_signatures == 1
    with pytest.raises(VenueError, match="https"):
        BybitRest(base_url="http://example.com", api_key=KEY, api_secret=SECRET)


def test_the_secret_never_appears_in_repr_or_errors(double: tuple[VenueState, str]) -> None:
    state, url = double
    clock = lambda: state.now_ms / 1000  # noqa: E731
    rest = BybitRest(base_url=url, api_key=KEY, api_secret=SECRET, clock=clock)
    assert SECRET not in repr(rest)
    assert repr(rest).endswith("key=...cdef)")
    state.key_info["readOnly"] = 1
    with pytest.raises(VenueError) as caught:
        venue(state, url, ENVELOPE()).preflight()
    assert SECRET not in str(caught.value)


def ENVELOPE() -> Envelope:  # noqa: N802 - a fixture-free accessor for the examples envelope
    from trading_research_agent.envelope import load_envelope

    return load_envelope(Path(__file__).resolve().parent.parent / "examples" / "envelope.toml")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"readOnly": 1}, "read-only"),
        (
            {"permissions": {"ContractTrade": ["Order", "Position"], "Wallet": ["Withdraw"]}},
            "wallet",
        ),
        ({"permissions": {"ContractTrade": ["Position"], "Wallet": []}}, "Order and Position"),
        ({"ips": []}, "not bound to an IP"),
        ({"expiredAt": "2025-01-03T00:00:00Z"}, "expires in"),
    ],
)
def test_a_key_that_is_more_or_less_than_a_trade_key_is_refused(
    double: tuple[VenueState, str], envelope: Envelope, change: dict[str, Any], message: str
) -> None:
    state, url = double
    state.key_info.update(change)
    with pytest.raises(VenueError, match=message):
        venue(state, url, envelope).preflight()


def test_an_unbound_ip_can_be_accepted_deliberately(
    double: tuple[VenueState, str], envelope: Envelope
) -> None:
    state, url = double
    state.key_info["ips"] = []
    report = venue(state, url, envelope, key_ip_unbound_ok=True).preflight()
    assert report["key"]["ips_bound"] is False and report["position"] == "0"


def test_clock_skew_and_a_foreign_position_stop_the_session_before_it_starts(
    double: tuple[VenueState, str], envelope: Envelope
) -> None:
    state, url = double
    v = venue(state, url, envelope)
    v.clock = lambda: (state.now_ms + 1500) / 1000
    with pytest.raises(VenueError, match="local clock"):
        v.preflight()
    state.position = Decimal("0.02")
    with pytest.raises(VenueError, match=r"holds 0\.02"):
        venue(state, url, envelope).preflight()
    adopted = venue(state, url, envelope, adopt_open_position=True)
    assert adopted.preflight()["position"] == "0.02"
    with pytest.raises(VenueError, match="preflight"):
        venue(state, url, envelope).submit(intent(Direction.LONG))


# S5..S10: orders


def test_an_entry_fills_and_a_close_is_reduce_only(
    double: tuple[VenueState, str], envelope: Envelope
) -> None:
    state, url = double
    v = venue(state, url, envelope)
    v.preflight()
    assert v.submit(intent(Direction.LONG)) is None
    queued = v.pending
    assert queued is not None and queued.direction is Direction.LONG
    with pytest.raises(VenueError, match="one order at a time"):
        v.submit(intent(Direction.LONG))
    fill = v.poll(print_(1))
    assert fill is not None and fill.size == Decimal("0.010") and fill.price == Decimal("100")
    assert v.pending is None and state.creates[0]["orderLinkId"] == "tra-01234567-000001"
    assert "reduceOnly" not in state.creates[0]
    assert state.creates[0]["timeInForce"] == "IOC" and state.creates[0]["orderType"] == "Market"

    v.submit(intent(Direction.SHORT))
    close = v.poll(print_(2))
    assert close is not None and state.creates[1]["reduceOnly"] is True
    assert state.position == 0
    cost = v.cost_of(Direction.LONG, fill.ts_ns, close.ts_ns, Decimal("3"))
    assert cost.fee_round_trip_bps == Decimal("11.0")  # as charged: 5.5 bps per touch
    assert cost.spread_cost_bps == 0 and cost.slippage_bps == 0


def test_a_lost_response_is_retried_with_the_same_link_id_not_a_new_order(
    double: tuple[VenueState, str], envelope: Envelope
) -> None:
    state, url = double
    state.lose_first_create = True
    v = venue(state, url, envelope)
    v.preflight()
    v.submit(intent(Direction.LONG))
    assert v.pending is not None and any("attempt 1 failed" in e for e in v.events)
    fill = v.poll(print_(1))
    assert fill is None  # the resend met 110030: the first order had landed
    fill = v.poll(print_(2))
    assert fill is not None
    links = [c["orderLinkId"] for c in state.creates]
    assert links == ["tra-01234567-000001", "tra-01234567-000001"]
    assert state.position == Decimal("0.010")


def test_rate_limits_are_waited_out_not_hammered(
    double: tuple[VenueState, str], envelope: Envelope
) -> None:
    state, url = double
    state.rate_limit_once = True
    waits: list[float] = []
    v = venue(state, url, envelope)
    v.rest.sleep = waits.append
    v.preflight()
    v.submit(intent(Direction.LONG))
    assert v.poll(print_(1)) is not None
    assert waits and 0.1 <= waits[0] <= 5.0
    assert [c["orderLinkId"] for c in state.creates] == ["tra-01234567-000001"] * 2


def test_a_partial_fill_is_reported_at_its_executed_size(
    double: tuple[VenueState, str], envelope: Envelope
) -> None:
    state, url = double
    state.fill_fraction = Decimal("0.5")
    v = venue(state, url, envelope)
    v.preflight()
    v.submit(intent(Direction.LONG, "0.02"))
    fill = v.poll(print_(1))
    assert fill is not None and fill.size == Decimal("0.010")
    assert any("partial" in e for e in v.events)


def test_reconciliation_failure_is_a_venue_error(
    double: tuple[VenueState, str], envelope: Envelope
) -> None:
    state, url = double
    state.drift_position_after_fill = Decimal("0.005")
    v = venue(state, url, envelope)
    v.preflight()
    v.submit(intent(Direction.LONG))
    with pytest.raises(VenueError, match="reconciliation failed"):
        v.poll(print_(1))


def test_a_reduce_only_refusal_names_the_model_mismatch(
    double: tuple[VenueState, str], envelope: Envelope
) -> None:
    state, url = double
    state.position = Decimal("0.01")
    v = venue(state, url, envelope, adopt_open_position=True)
    v.preflight()
    state.position = Decimal(0)  # the venue got flat behind the model's back
    with pytest.raises(VenueError, match="only a reduce"):
        v.submit(intent(Direction.SHORT))


# S7, S11, S14 through the runtime


class OneLong:
    name = "one-long"

    def __init__(self) -> None:
        self.fired = False

    def on_bar(self, bar: Bar) -> Signal | None:
        if not self.fired and bar.close is not None:
            self.fired = True
            return Signal(Target.LONG, "the one entry of this test")
        return None


def limits() -> Limits:
    return Limits(
        max_risk_per_trade_bps=Decimal("5000"),
        max_position=Decimal("1"),
        max_orders_per_minute=10,
        daily_loss_limit_bps=Decimal("100000"),
    )


def test_the_runtime_routes_a_stop_through_the_venue_and_records_the_actual_fill(
    double: tuple[VenueState, str], envelope: Envelope, tmp_path: Path
) -> None:
    state, url = double
    v = venue(state, url, envelope)
    v.preflight()
    prints = [print_(0), print_(1), print_(2), print_(3, "90"), print_(4, "90"), print_(5, "90")]
    config = TradingConfig(
        feed=RecordedFeed("BTCUSDT", prints),
        venue=v,
        strategy=OneLong(),
        exit_rule=ExitRule(Decimal("0.05"), 100, "five percent is beyond the mechanism's reach"),
        controls=Controls(limits(), KillSwitch()),
        out_dir=tmp_path,
        bar_interval_ns=MINUTE,
        size=Decimal("0.01"),
        spread_cost_bps=Decimal("1"),
        equity=Decimal("10000"),
    )
    state.price = Decimal("100")
    report = run(config)
    assert report.fills == 2 and report.round_trips == 1
    row = json.loads((tmp_path / "trades.jsonl").read_text().splitlines()[0])
    assert row["cause"] == "sl" and row["entry_px"] == "100" and row["exit_px"] == "100"
    assert state.creates[1]["reduceOnly"] is True
    assert Decimal(row["cost_bps"]) == Decimal("11.0")


def test_canary_caps_end_the_session_flat(
    double: tuple[VenueState, str], envelope: Envelope, tmp_path: Path
) -> None:
    state, url = double
    v = venue(state, url, envelope)
    v.preflight()
    caps = CanaryLimits(
        max_notional=Decimal("50"),
        max_round_trips=5,
        session_loss_limit_bps=Decimal("1000"),
        max_session_minutes=3,
    )
    config = TradingConfig(
        feed=RecordedFeed("BTCUSDT", [print_(i) for i in range(8)]),
        venue=v,
        strategy=OneLong(),
        exit_rule=ExitRule(Decimal("0.05"), 100, "five percent is beyond the mechanism's reach"),
        controls=Controls(limits(), KillSwitch()),
        out_dir=tmp_path,
        bar_interval_ns=MINUTE,
        size=Decimal("0.01"),
        spread_cost_bps=Decimal("1"),
        equity=Decimal("10000"),
        canary=CanaryGuard(caps),
    )
    report = run(config)
    assert report.stopped_by is not None and report.stopped_by.startswith("canary: session ran")
    assert report.open_position is None and state.position == 0
    assert state.creates[-1]["reduceOnly"] is True


def test_a_venue_error_mid_session_stops_the_loop_and_arms_the_kill_switch(
    double: tuple[VenueState, str], envelope: Envelope, tmp_path: Path
) -> None:
    state, url = double
    state.drift_position_after_fill = Decimal("0.005")
    v = venue(state, url, envelope)
    v.preflight()
    switch = KillSwitch()
    config = TradingConfig(
        feed=RecordedFeed("BTCUSDT", [print_(i) for i in range(4)]),
        venue=v,
        strategy=OneLong(),
        exit_rule=ExitRule(Decimal("0.05"), 100, "five percent is beyond the mechanism's reach"),
        controls=Controls(limits(), switch),
        out_dir=tmp_path,
        bar_interval_ns=MINUTE,
        size=Decimal("0.01"),
        spread_cost_bps=Decimal("1"),
        equity=Decimal("10000"),
    )
    report = run(config)
    assert report.stopped_by is not None and report.stopped_by.startswith("venue: ")
    assert switch.armed and len(state.creates) == 1


def test_canary_limits_refuse_a_missing_or_zero_cap() -> None:
    with pytest.raises(ValueError, match="max_notional"):
        CanaryLimits(Decimal(0), 1, Decimal(1), 1)
    with pytest.raises(ValueError, match="max_session_minutes"):
        CanaryLimits(Decimal(1), 1, Decimal(1), 0)
    guard = CanaryGuard(CanaryLimits(Decimal("10"), 2, Decimal("50"), 30))
    assert guard.refuse_entry(ts_ns=0, notional=None, round_trips=0, net_bps=Decimal(0))
    assert guard.refuse_entry(ts_ns=0, notional=Decimal("11"), round_trips=0, net_bps=Decimal(0))
    assert guard.refuse_entry(ts_ns=0, notional=Decimal("9"), round_trips=2, net_bps=Decimal(0))
    assert guard.refuse_entry(ts_ns=0, notional=Decimal("9"), round_trips=0, net_bps=Decimal("-50"))
    assert (
        guard.refuse_entry(ts_ns=0, notional=Decimal("9"), round_trips=0, net_bps=Decimal(0))
        is None
    )


# The registry


def test_the_live_venue_is_a_plugin_that_reads_keys_from_the_environment(
    envelope: Envelope,
) -> None:
    assert "bybit" in live_venues()
    factory = live_venue("BYBIT")
    assert factory.env_keys == ("BYBIT_API_KEY", "BYBIT_API_SECRET")
    assert set(factory.environments) == {"demo", "testnet", "mainnet"}
    with pytest.raises(VenueError, match="missing BYBIT_API_KEY, BYBIT_API_SECRET"):
        factory(settings(envelope), {})
    with pytest.raises(VenueError, match="environment"):
        factory(
            settings(envelope, environment="prod"), {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}
        )
    built = factory(settings(envelope), {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"})
    assert isinstance(built, BybitLiveVenue)
    assert built.label == "bybit-demo" and built.rest.base_url == "https://api-demo.bybit.com"
    with pytest.raises(UnknownLiveVenueError, match="registered"):
        live_venue("no-such-venue")
