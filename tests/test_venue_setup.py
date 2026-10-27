"""Adding a venue: the keys it signs with, the blocks to paste, and a cycle you can watch."""

from __future__ import annotations

import tomllib
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trading_research_agents.agents.progress import Step
from trading_research_agents.cli import tui_cycle
from trading_research_agents.cli.venue_info import venue_setup
from trading_research_agents.data.sources.capability import Capability
from trading_research_agents.trading.dispatch import DispatchError, VenueHand, credentials_for

# ccxt's own attribute name, mixed case and all.
Passphrase = SimpleNamespace(
    requiredCredentials={"apiKey": True, "secret": True, "password": True, "uid": False}
)
Wallet = SimpleNamespace(
    requiredCredentials={"apiKey": False, "walletAddress": True, "privateKey": True}
)


FULL = {
    "create_order": True,
    "attached_exits": True,
    "position": True,
    "open_orders": True,
    "cancel": True,
}
DATA = {"ohlcv": True, "orderbook": True, "funding": True, "open_interest": True}


def test_a_venue_is_asked_for_exactly_what_it_signs_with() -> None:
    assert credentials_for("acme", client=Passphrase) == {
        "apiKey": "ACME_API_KEY",
        "secret": "ACME_API_SECRET",
        "password": "ACME_API_PASSPHRASE",
    }
    assert credentials_for("chain", client=Wallet) == {
        "walletAddress": "CHAIN_WALLET_ADDRESS",
        "privateKey": "CHAIN_PRIVATE_KEY",
    }
    # A venue that says nothing is read as the ordinary pair, never as no credentials at all.
    assert credentials_for("quiet", client=SimpleNamespace(requiredCredentials={})) == {
        "apiKey": "QUIET_API_KEY",
        "secret": "QUIET_API_SECRET",
    }


def test_a_missing_passphrase_is_named_before_anything_is_built() -> None:
    with pytest.raises(DispatchError, match="ACME_API_PASSPHRASE"):
        VenueHand(
            venue="bybit",
            symbol="BTC/USDT:USDT",
            max_notional=Decimal(100),
            keys=credentials_for("acme", client=Passphrase),
            env={"ACME_API_KEY": "k" * 12, "ACME_API_SECRET": "s" * 12},
        )


def test_the_setup_for_a_full_venue_is_every_block_and_leaves_the_fee_to_you() -> None:
    able = Capability(venue="acme", data=DATA, execution=FULL, demo=False, testnet=True)
    text = venue_setup(able, ["ACME_API_KEY", "ACME_API_SECRET", "ACME_API_PASSPHRASE"])
    assert 'venue_key = "acme/taker-taker"' in text and 'environment = "testnet"' in text
    assert "ACME_API_PASSPHRASE=" in text and 'exits = "none"' not in text
    # Pasted as it is, the envelope refuses to load: no venue is priced at a fee nobody wrote.
    channel = text.split("[[channel]]")[1].split("[[channel]]")[0]
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads("[[channel]]" + channel)


def test_the_setup_says_when_you_carry_the_exits_or_when_only_data_is_possible() -> None:
    no_exits = Capability(
        venue="acme", data=DATA, execution={**FULL, "attached_exits": False}, demo=True
    )
    assert 'exits = "none"' in venue_setup(no_exits, ["ACME_API_KEY", "ACME_API_SECRET"])
    blind = Capability(venue="acme", data=DATA, execution={**FULL, "position": False})
    said = venue_setup(blind, [])
    assert "cannot report the position" in said and "[[channel]]" not in said
    real = Capability(venue="acme", data=DATA, execution=FULL)
    assert "the first order here is real money" in venue_setup(real, [])


def test_the_live_cycle_view_names_who_is_working_and_for_how_long() -> None:
    now = [100.0]
    view = tui_cycle.CycleView(
        SimpleNamespace(tokens=0, cost=Decimal(0)), ["data", "technical"], clock=lambda: now[0]
    )
    view.on_step(Step(name="technical", state="running"))
    view.on_step(Step(name="macro", state="running"))
    now[0] = 104.0
    assert view.status() == "4s | working: technical, macro | 0 tokens"
    view.on_step(Step(name="technical", state="done", seconds=3.2))
    assert "working: macro |" in view.status()
