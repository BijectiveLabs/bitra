"""Gate's testnet and BingX's demo (VST), in the shapes they answered on 2026-10-20."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from trading_research_agents.trading.capital import read_account
from trading_research_agents.trading.dispatch import VenueHand
from trading_research_agents.trading.hand_state import _client_id, key_hint
from trading_research_agents.trading.venues.margin import leverage_params, route


class Client:
    def __init__(self, venue: str, default: str | None) -> None:
        self.id = venue
        self.has = {"fetchLeverage": True}
        self.options: dict[str, Any] = {
            "defaultType": default,
            "fetchMarkets": {"types": ["spot", "swap", "future", "option"]},
        }
        self.leverage_calls: list[tuple[Any, ...]] = []

    def fetch_balance(self) -> dict[str, Any]:
        return {"VST": {"total": 100000.0, "free": 99000.0}, "total": {"VST": 100000.0}}

    def fetch_leverage(self, symbol: str) -> dict[str, Any]:
        raise RuntimeError("gate fetchLeverage() does not support swap markets")

    def fetch_positions(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        row = {
            "symbol": "BTC/USDT:USDT",
            "contracts": 0.0,
            "leverage": 3.0,
            "marginMode": "isolated",
        }
        return [row]


def test_a_futures_symbol_reads_the_futures_wallet_and_only_the_markets_traded() -> None:
    gate = Client("gate", None)
    route("gate", gate, "BTC/USDT:USDT")
    assert gate.options["defaultType"] == "swap"
    assert gate.options["fetchMarkets"]["types"] == ["swap"]
    unresolved = Client("gate", None)
    route("gate", unresolved, "BTCUSDT")
    assert unresolved.options["fetchMarkets"]["types"] == ["spot", "swap"]
    bingx = Client("bingx", "spot")
    route("bingx", bingx, "BTC/USDT:USDT")
    assert bingx.options["defaultType"] == "swap"


def test_bingx_demo_reads_vst_as_the_usdt_its_contracts_settle_in() -> None:
    account = read_account(Client("bingx", "swap"), "USDT")
    assert account.wallet == Decimal("100000.0") and account.free == Decimal("99000.0")
    assert leverage_params("bingx") == {"side": "BOTH"} and leverage_params("bybit") == {}


def test_leverage_kept_on_the_position_is_read_from_the_position_list() -> None:
    hand = VenueHand(
        venue="gate",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(1000),
        keys=("K", "S"),
        client=Client("gate", "swap"),
    )
    assert hand._leverage_now() == (Decimal(3), "isolated")


def test_a_venue_with_a_shorter_client_id_gets_a_fixed_digest_of_the_same_id() -> None:
    intent = {"at": "2026-10-20T08-11-19-547Z", "instrument": "BTCUSDT"}
    full = _client_id(intent)
    assert full.startswith("tra20261020T081119547Z") and len(full) <= 32
    short = _client_id(intent, 28)
    assert len(short) == 28 and short.isalnum() and short == _client_id(intent, 28)
    assert short != _client_id({**intent, "instrument": "ETHUSDT"}, 28)
    gate = _client_id(intent, 28, "t-")
    assert gate.startswith("t-tra") and len(gate) == 28


def test_a_key_set_under_another_name_is_named_never_shown() -> None:
    env = {"GATE_API_KEY": "k" * 32, "GATE_SECRET_KEY": "s" * 64, "OTHER_TOKEN": "x" * 20}
    said = key_hint(["GATE_API_SECRET"], ("GATE_API_KEY", "GATE_API_SECRET"), env)
    assert "GATE_SECRET_KEY is set" in said and "GATE_API_SECRET" in said
    assert "s" * 64 not in said and "OTHER_TOKEN" not in said
    assert key_hint(["GATE_API_SECRET"], ("GATE_API_KEY", "GATE_API_SECRET"), {}) == ""
