"""The security review of the live tier, executed rather than described.

The live tier sends through the same ccxt client and `VenueHand` as venue mode. The double below
answers with ccxt 4.5.85's unified shapes and raises ccxt's own exceptions, keeps the venue's
position in contracts, and records every order it was sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

ccxt = pytest.importorskip("ccxt")

from trading_research_agents.data import Direction, Side, Trade  # noqa: E402
from trading_research_agents.envelope import Envelope  # noqa: E402
from trading_research_agents.execution import FundingSchedule  # noqa: E402
from trading_research_agents.strategy import Bar, ExitRule, Signal, Target  # noqa: E402
from trading_research_agents.trading import (  # noqa: E402
    Controls,
    KillSwitch,
    Limits,
    RecordedFeed,
    TradingConfig,
    VenueError,
    run,
)
from trading_research_agents.trading.canary import CanaryGuard, CanaryLimits  # noqa: E402
from trading_research_agents.trading.dispatch import VenueHand  # noqa: E402
from trading_research_agents.trading.venue import Intent  # noqa: E402
from trading_research_agents.trading.venues import (  # noqa: E402
    LiveVenueSettings,
    UnknownLiveVenueError,
    live_venue,
    live_venues,
)
from trading_research_agents.trading.venues.ccxt_live import (  # noqa: E402
    CcxtLiveFactory,
    CcxtLiveVenue,
)

KEY = "key-0123456789abcdef"
SECRET = "secret-that-must-never-appear-anywhere"  # noqa: S105 - a test double's credential
MINUTE = 60 * 1_000_000_000
UNIT = Decimal("0.001")
SYMBOL = "BTC/USDT:USDT"


@dataclass
class Double:
    """What the venue believes, and what it was told to do."""

    now_ms: int = 1_735_689_600_000
    key_info: dict[str, Any] = field(
        default_factory=lambda: {
            "readOnly": 0,
            "permissions": {"ContractTrade": ["Order", "Position"], "Wallet": []},
            "ips": ["203.0.113.7"],
            "expiredAt": "2027-01-01T00:00:00Z",
        }
    )
    # In the base. The venue counts contracts; the double converts at its edge, like a venue does.
    position: Decimal = Decimal(0)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    creates: list[dict[str, Any]] = field(default_factory=list)
    fill_fraction: Decimal = Decimal(1)
    fee_bps: Decimal = Decimal("5.5")
    price: Decimal = Decimal("100")
    drift_position_after_fill: Decimal = Decimal(0)
    lose_first_create: bool = False
    rate_limit_once: bool = False
    has: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    def privateGetV5UserQueryApi(self) -> dict[str, Any]:  # noqa: N802 - ccxt's own name
        return {"retCode": 0, "result": dict(self.key_info)}

    def fetch_time(self) -> int:
        return self.now_ms

    def load_markets(self) -> dict[str, Any]:
        return self.markets

    @property
    def markets(self) -> dict[str, Any]:
        return {SYMBOL: self.market(SYMBOL)}

    @property
    def markets_by_id(self) -> dict[str, Any]:
        return {"BTCUSDT": [self.market(SYMBOL)]}

    def market(self, symbol: str) -> dict[str, Any]:
        return {
            "id": "BTCUSDT",
            "symbol": SYMBOL,
            "type": "swap",
            "swap": True,
            "contract": True,
            "linear": True,
            "base": "BTC",
            "quote": "USDT",
            "settle": "USDT",
            "contractSize": float(UNIT),
            "active": True,
        }

    def fetch_position(self, symbol: str) -> dict[str, Any] | None:
        if self.position == 0:
            return None
        side = "long" if self.position > 0 else "short"
        return {"symbol": symbol, "contracts": float(abs(self.position) / UNIT), "side": side}

    def create_order(
        self,
        symbol: str,
        kind: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = dict(params or {})
        self.creates.append(
            {
                "symbol": symbol,
                "type": kind,
                "side": side,
                "amount": amount,
                "price": price,
                **params,
            }
        )
        client_id = params["clientOrderId"]
        if self.rate_limit_once:
            self.rate_limit_once = False
            raise ccxt.RateLimitExceeded("bybit Too many visits!")
        if any(o["clientOrderId"] == client_id for o in self.orders.values()):
            raise ccxt.DuplicateOrderId("bybit Duplicate orderId")
        sign = Decimal(1) if side == "buy" else Decimal(-1)
        if params.get("reduceOnly") and (self.position == 0 or (self.position > 0) == (sign > 0)):
            raise ccxt.InvalidOrder(
                "bybit Currently you can only reduce your position on this contract"
            )
        contracts = Decimal(str(amount))
        done = (contracts * self.fill_fraction).to_integral_value()
        self.position += sign * done * UNIT + self.drift_position_after_fill
        order_id = f"oid-{len(self.orders) + 1}"
        self.orders[order_id] = {
            "id": order_id,
            "clientOrderId": client_id,
            "status": "closed" if done == contracts else "canceled",
            "filled": float(done),
            "average": float(self.price),
            "fee": {"cost": float(self.price * done * UNIT * self.fee_bps / Decimal(10_000))},
            "lastTradeTimestamp": self.now_ms + 5,
        }
        if self.lose_first_create:
            # The order landed but the answer never made it back to the client.
            self.lose_first_create = False
            raise ccxt.RequestTimeout("bybit GET timed out")
        return {"id": order_id, "clientOrderId": client_id}

    def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any] | None:
        return dict(self.orders[order_id]) if order_id in self.orders else None

    def fetch_closed_orders(self, symbol: str) -> list[dict[str, Any]]:
        return [dict(o) for o in self.orders.values()]


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


def venue(double: Double, envelope: Envelope, **overrides: Any) -> CcxtLiveVenue:
    clock = lambda: double.now_ms / 1000  # noqa: E731 - the double's clock is the test's clock
    hand = VenueHand(
        venue="bybit",
        symbol=SYMBOL,
        instrument="BTCUSDT",
        max_notional=Decimal(1),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=double,
        env={"BYBIT_API_KEY": KEY, "BYBIT_API_SECRET": SECRET},
        clock=clock,
    )
    return CcxtLiveVenue(hand, settings(envelope, **overrides), clock=clock)


def print_(i: int, price: str = "100") -> Trade:
    return Trade(
        i * MINUTE + 1, "bybit", "BTCUSDT", Decimal(price), Decimal("0.01"), Side.BUY, f"t{i}"
    )


def intent(direction: Direction, size: str = "0.01") -> Intent:
    return Intent(1, "BTCUSDT", direction, Decimal(size), "test")


# The preflight


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
    envelope: Envelope, change: dict[str, Any], message: str
) -> None:
    double = Double()
    double.key_info.update(change)
    with pytest.raises(VenueError, match=message):
        venue(double, envelope).preflight()


def test_an_unbound_ip_can_be_accepted_deliberately(envelope: Envelope) -> None:
    double = Double()
    double.key_info["ips"] = []
    report = venue(double, envelope, key_ip_unbound_ok=True).preflight()
    assert report == {"symbol": SYMBOL, "contract_size": "0.001", "position": "0"}


def test_clock_skew_and_a_foreign_position_stop_the_session_before_it_starts(
    envelope: Envelope,
) -> None:
    double = Double()
    skewed = venue(double, envelope)
    skewed.hand.clock = lambda: (double.now_ms + 1500) / 1000
    with pytest.raises(VenueError, match="clock is \\+1500 ms from bybit"):
        skewed.preflight()
    double.position = Decimal("0.02")
    with pytest.raises(VenueError, match=r"holds 0\.02"):
        venue(double, envelope).preflight()
    adopted = venue(double, envelope, adopt_open_position=True)
    assert Decimal(adopted.preflight()["position"]) == Decimal("0.02")
    with pytest.raises(VenueError, match="preflight"):
        venue(double, envelope).submit(intent(Direction.LONG))


def test_the_secret_never_appears_in_an_error(envelope: Envelope) -> None:
    class Leaky(Double):
        def privateGetV5UserQueryApi(self) -> dict[str, Any]:  # noqa: N802
            raise ccxt.AuthenticationError(f"bybit rejected key {KEY} with secret {SECRET}")

    with pytest.raises(VenueError) as caught:
        venue(Leaky(), envelope).preflight()
    assert SECRET not in str(caught.value) and KEY not in str(caught.value)
    assert "[redacted]" in str(caught.value)


# Orders


def test_an_entry_fills_and_a_close_is_reduce_only(envelope: Envelope) -> None:
    double = Double()
    v = venue(double, envelope)
    v.preflight()
    assert v.submit(intent(Direction.LONG)) is None
    queued = v.pending
    assert queued is not None and queued.direction is Direction.LONG
    with pytest.raises(VenueError, match="one order at a time"):
        v.submit(intent(Direction.LONG))
    fill = v.poll(print_(1))
    assert fill is not None and fill.size == Decimal("0.010") and fill.price == Decimal("100")
    sent = double.creates[0]
    assert v.pending is None and sent["clientOrderId"] == "tra01234567000001"
    # Sized in the base, sent in contracts: 0.01 BTC is ten contracts of 0.001.
    assert sent["type"] == "market" and sent["amount"] == 10.0 and "reduceOnly" not in sent

    v.submit(intent(Direction.SHORT))
    close = v.poll(print_(2))
    assert close is not None and double.creates[1]["reduceOnly"] is True
    assert double.position == 0
    cost = v.cost_of(Direction.LONG, fill.ts_ns, close.ts_ns, Decimal("3"))
    assert cost.fee_round_trip_bps == Decimal("11.0")  # as charged: 5.5 bps per touch
    assert cost.spread_cost_bps == 0 and cost.slippage_bps == 0


def test_a_lost_response_is_found_by_its_client_id_not_sent_again(envelope: Envelope) -> None:
    double = Double()
    double.lose_first_create = True
    v = venue(double, envelope)
    v.preflight()
    v.submit(intent(Direction.LONG))
    assert v.pending is not None and any("attempt 1 failed" in e for e in v.events)
    # The order landed; the venue knows its client id, so it is read rather than sent twice.
    fill = v.poll(print_(1))
    assert fill is not None and fill.size == Decimal("0.010")
    assert [c["clientOrderId"] for c in double.creates] == ["tra01234567000001"]
    assert double.position == Decimal("0.010") and len(double.orders) == 1


def test_a_duplicate_answer_is_taken_as_the_order_having_landed(envelope: Envelope) -> None:
    @dataclass
    class Hidden(Double):
        hide: bool = True

        def fetch_closed_orders(self, symbol: str) -> list[dict[str, Any]]:
            # A venue whose list lags: the landed order is not there on the first look.
            hidden, self.hide = self.hide, False
            return [] if hidden else super().fetch_closed_orders(symbol)

    double = Hidden()
    double.lose_first_create = True
    v = venue(double, envelope)
    v.preflight()
    v.submit(intent(Direction.LONG))
    assert v.poll(print_(1)) is None  # not listed yet, so sent again: the venue says duplicate
    fill = v.poll(print_(2))
    assert fill is not None and len(double.orders) == 1
    assert [c["clientOrderId"] for c in double.creates] == ["tra01234567000001"] * 2


def test_a_rate_limit_is_waited_out_and_sent_again_not_doubled(envelope: Envelope) -> None:
    double = Double()
    double.rate_limit_once = True
    v = venue(double, envelope)
    v.preflight()
    v.submit(intent(Direction.LONG))
    assert v.poll(print_(1)) is None
    assert v.poll(print_(2)) is not None
    assert [c["clientOrderId"] for c in double.creates] == ["tra01234567000001"] * 2
    assert len(double.orders) == 1


def test_a_send_that_keeps_failing_stops_after_three_attempts(envelope: Envelope) -> None:
    class Down(Double):
        def create_order(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise ccxt.ExchangeNotAvailable("bybit 503")

    v = venue(Down(), envelope)
    v.preflight()
    v.submit(intent(Direction.LONG))
    v.poll(print_(1))
    with pytest.raises(VenueError, match="3 attempts failed"):
        v.poll(print_(2))
    assert v.pending is None


def test_a_partial_fill_is_reported_at_its_executed_size(envelope: Envelope) -> None:
    double = Double()
    double.fill_fraction = Decimal("0.5")
    v = venue(double, envelope)
    v.preflight()
    v.submit(intent(Direction.LONG, "0.02"))
    fill = v.poll(print_(1))
    assert fill is not None and fill.size == Decimal("0.010")
    assert any("partial" in e for e in v.events)


def test_reconciliation_failure_is_a_venue_error(envelope: Envelope) -> None:
    double = Double()
    double.drift_position_after_fill = Decimal("0.005")
    v = venue(double, envelope)
    v.preflight()
    v.submit(intent(Direction.LONG))
    with pytest.raises(VenueError, match="reconciliation failed"):
        v.poll(print_(1))


def test_a_reduce_only_refusal_names_the_model_mismatch(envelope: Envelope) -> None:
    double = Double()
    double.position = Decimal("0.01")
    v = venue(double, envelope, adopt_open_position=True)
    v.preflight()
    double.position = Decimal(0)  # the venue got flat behind the model's back
    with pytest.raises(VenueError, match="does not match the venue's"):
        v.submit(intent(Direction.SHORT))


def test_a_spot_symbol_is_refused_before_the_first_print(envelope: Envelope) -> None:
    class Spot(Double):
        def market(self, symbol: str) -> dict[str, Any]:
            return {"symbol": "BTC/USDT", "type": "spot", "spot": True, "quote": "USDT"}

    with pytest.raises(VenueError, match="is a spot market; the live tier trades linear"):
        venue(Spot(), envelope).preflight()


# Through the runtime


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


def config(v: CcxtLiveVenue, prints: list[Trade], tmp_path: Path, **extra: Any) -> TradingConfig:
    base: dict[str, Any] = {
        "feed": RecordedFeed("BTCUSDT", prints),
        "venue": v,
        "strategy": OneLong(),
        "exit_rule": ExitRule(Decimal("0.05"), 100, "five percent is beyond the mechanism's reach"),
        "controls": Controls(limits(), KillSwitch()),
        "out_dir": tmp_path,
        "bar_interval_ns": MINUTE,
        "size": Decimal("0.01"),
        "spread_cost_bps": Decimal("1"),
        "equity": Decimal("10000"),
    }
    base.update(extra)
    return TradingConfig(**base)


def test_the_runtime_routes_a_stop_through_the_venue_and_records_the_actual_fill(
    envelope: Envelope, tmp_path: Path
) -> None:
    import json

    double = Double()
    v = venue(double, envelope)
    v.preflight()
    prints = [print_(0), print_(1), print_(2), print_(3, "90"), print_(4, "90"), print_(5, "90")]
    report = run(config(v, prints, tmp_path))
    assert report.fills == 2 and report.round_trips == 1
    row = json.loads((tmp_path / "trades.jsonl").read_text().splitlines()[0])
    assert row["cause"] == "sl"
    # The fills the venue reported, not the print that triggered the stop at 90.
    assert Decimal(row["entry_px"]) == Decimal(row["exit_px"]) == Decimal("100")
    assert double.creates[1]["reduceOnly"] is True
    assert Decimal(row["cost_bps"]) == Decimal("11.0")


def test_canary_caps_end_the_session_flat(envelope: Envelope, tmp_path: Path) -> None:
    double = Double()
    v = venue(double, envelope)
    v.preflight()
    caps = CanaryLimits(
        max_notional=Decimal("50"),
        max_round_trips=5,
        session_loss_limit_bps=Decimal("1000"),
        max_session_minutes=3,
    )
    report = run(config(v, [print_(i) for i in range(8)], tmp_path, canary=CanaryGuard(caps)))
    assert report.stopped_by is not None and report.stopped_by.startswith("canary: session ran")
    assert report.open_position is None and double.position == 0
    assert double.creates[-1]["reduceOnly"] is True


def test_a_venue_error_mid_session_stops_the_loop_and_arms_the_kill_switch(
    envelope: Envelope, tmp_path: Path
) -> None:
    double = Double()
    double.drift_position_after_fill = Decimal("0.005")
    v = venue(double, envelope)
    v.preflight()
    switch = KillSwitch()
    report = run(
        config(v, [print_(i) for i in range(4)], tmp_path, controls=Controls(limits(), switch))
    )
    assert report.stopped_by is not None and report.stopped_by.startswith("venue: ")
    assert switch.armed and len(double.creates) == 1


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


def test_every_ccxt_venue_is_a_live_venue_that_reads_its_keys_from_the_environment(
    envelope: Envelope,
) -> None:
    assert {"bybit", "okx", "bitget", "hyperliquid"} <= set(live_venues())
    factory = live_venue("BYBIT")
    assert isinstance(factory, CcxtLiveFactory)
    assert factory.env_keys == ("BYBIT_API_KEY", "BYBIT_API_SECRET")
    assert set(factory.environments) == {"demo", "testnet", "mainnet"}
    assert live_venue("okx").env_keys == ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE")
    with pytest.raises(VenueError, match="missing BYBIT_API_KEY, BYBIT_API_SECRET"):
        factory(settings(envelope), {})
    with pytest.raises(VenueError, match="environment"):
        factory(
            settings(envelope, environment="prod"), {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}
        )
    with pytest.raises(UnknownLiveVenueError, match="not an exchange ccxt knows"):
        live_venue("no-such-venue")
