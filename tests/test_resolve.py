"""Which market a name means, on venues that spell the same perpetual five different ways."""

from __future__ import annotations

from decimal import Decimal
from importlib import import_module
from typing import Any

import pytest

from trading_research_agents.data.sources.resolve import ResolveError, resolve, slug_of


def market(
    id_: str, base: str, settle: str, *, swap: bool = True, quote: str | None = None
) -> dict[str, Any]:
    quote = quote or settle
    symbol = f"{base}/{quote}:{settle}" if swap else f"{base}/{quote}"
    return {
        "id": id_,
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "settle": settle if swap else None,
        "swap": swap,
        "linear": True if swap else None,
        "active": True,
    }


def venue(*rows: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    by_id: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_id.setdefault(row["id"], []).append(row)
    return by_id, list(rows)


def test_a_unified_symbol_is_taken_as_given() -> None:
    by_id, rows = venue(market("BTCUSDT", "BTC", "USDT"))
    assert resolve("BTC/USDT:USDT", by_id, rows) == "BTC/USDT:USDT"


def test_the_same_string_on_a_venue_that_lists_spot_is_the_perpetual() -> None:
    by_id, rows = venue(
        market("BTCUSDT", "BTC", "USDT", swap=False, quote="USDT"),
        market("BTCUSDT", "BTC", "USDT"),
    )
    assert resolve("BTCUSDT", by_id, rows) == "BTC/USDT:USDT"


def test_a_venue_with_its_own_spelling_resolves_by_base_and_settle() -> None:
    okx = venue(market("BTC-USDT-SWAP", "BTC", "USDT"), market("ZEC-USDT-SWAP", "ZEC", "USDT"))
    assert resolve("BTCUSDT", *okx) == "BTC/USDT:USDT"
    assert resolve("BTC-USDT-SWAP", *okx) == "BTC/USDT:USDT"

    hyperliquid = venue(market("0", "BTC", "USDC"), market("214", "ZEC", "USDC"))
    assert resolve("ZECUSDC", *hyperliquid) == "ZEC/USDC:USDC"
    with pytest.raises(ResolveError, match="no linear perpetual named 'ZECUSDT'"):
        resolve("ZECUSDT", *hyperliquid)


def test_the_two_settlements_of_one_base_are_two_instruments() -> None:
    both = venue(market("BTCUSDT", "BTC", "USDT"), market("BTCUSDC", "BTC", "USDC"))
    assert resolve("BTCUSDT", *both) == "BTC/USDT:USDT"
    assert resolve("BTCUSDC", *both) == "BTC/USDC:USDC"
    assert slug_of(both[1][1]) == "BTCUSDC"


def test_a_scaled_contract_is_not_mistaken_for_the_plain_one() -> None:
    bybit = venue(market("1000PEPEUSDT", "1000PEPE", "USDT"))
    assert resolve("1000PEPEUSDT", *bybit) == "1000PEPE/USDT:USDT"
    with pytest.raises(ResolveError):
        resolve("PEPEUSDT", *bybit)


def test_a_name_that_could_be_two_markets_is_refused() -> None:
    twice = venue(market("X", "ABC", "USDT"), market("Y", "ABC", "USDT", quote="USDC"))
    with pytest.raises(ResolveError, match="declare which"):
        resolve("ABCUSDT", *twice)


def test_a_step_with_no_declaration_is_what_the_venue_states_in_the_base() -> None:
    from types import SimpleNamespace

    cli = import_module("trading_research_agents.cli.run_command")
    clients = import_module("trading_research_agents.cli.venue_access")._EXECUTION_CLIENTS

    btc = {**market("BTC-USDT-SWAP", "BTC", "USDT"), "precision": {"amount": 0.01}}
    btc["contractSize"] = 0.01
    client = SimpleNamespace(
        precisionMode=4,
        markets={btc["symbol"]: btc},
        markets_by_id={btc["id"]: [btc]},
        load_markets=lambda: None,
    )
    clients[("okx", "testnet")] = client
    try:
        cfg = SimpleNamespace(
            execution=SimpleNamespace(venue="okx", environment="testnet"),
            run=SimpleNamespace(venue_key="okx/taker-taker"),
        )
        # 0.01 of a contract that is 0.01 of a coin: 0.0001 of the base.
        assert cli._venue_step(cfg, "BTCUSDT") == Decimal("0.0001")
        assert cli._venue_step(cfg, "NOPEUSDT") is None
    finally:
        clients.pop(("okx", "testnet"), None)
