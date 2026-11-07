"""ccxt's own broker and channel tags never leave with a user's order.

The requests are the ones ccxt builds and signs for an order, taken through its own implicit API
with the network stubbed, so what is checked is what ccxt would have sent.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest

ccxt = pytest.importorskip("ccxt")

from trading_research_agents.trading.untagged import (  # noqa: E402
    embedded_tags,
    untagged,
    without_ccxt_headers,
)

# venue -> (implicit order method, params with the order's own client id, the auth header kept)
ORDER = {
    "bybit": ("private_post_v5_order_create", {"orderLinkId": "own-1"}, "X-BAPI-SIGN"),
    "gate": ("private_futures_post_settle_orders", {"settle": "usdt", "text": "t-own"}, "SIGN"),
    "bitget": ("private_mix_post_v2_mix_order_place_order", {"clientOid": "own-1"}, "ACCESS-SIGN"),
    "bingx": ("swap_v2_private_post_trade_order", {"clientOrderID": "own-1"}, "X-BX-APIKEY"),
    "mexc": ("spot_private_post_order", {"newClientOrderId": "own-1"}, "X-MEXC-APIKEY"),
    "okx": ("private_post_trade_order", {"clOrdId": "own-1"}, "OK-ACCESS-SIGN"),
}
IDENTITY = {"ccxt", "p4sve"}


def sent_by(venue: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The url, headers and body that leave for `venue` when an order is placed."""
    method, params, _ = ORDER[venue]
    client = getattr(ccxt, venue)({"apiKey": "k", "secret": "s", "password": "p"})
    left: dict[str, Any] = {}

    def record(url: str, method: str = "GET", headers: Any = None, body: Any = None) -> str:
        left.update(url=url, headers=headers or {}, body=body)
        return "{}"

    client.fetch = record
    untagged(client)
    getattr(client, method)({**params, **(extra or {})})
    return left


@pytest.mark.parametrize("venue", sorted(ORDER))
def test_no_header_that_carries_ccxts_identity_leaves(venue: str) -> None:
    left = sent_by(venue)
    values = {str(v).lower() for v in left["headers"].values() if v is not None}
    assert not values & IDENTITY, (venue, left["headers"])


@pytest.mark.parametrize("venue", sorted(ORDER))
def test_the_venues_own_authentication_still_leaves(venue: str) -> None:
    left = sent_by(venue)
    assert ORDER[venue][2] in left["headers"], (venue, left["headers"])


def test_ccxt_does_send_these_tags_unguarded() -> None:
    """The control: without the guard the same orders carry the tag, so the tests above can fail."""
    tagged = []
    for venue in ("bybit", "bingx", "bitget", "mexc"):
        method, params, _ = ORDER[venue]
        client = getattr(ccxt, venue)({"apiKey": "k", "secret": "s", "password": "p"})
        left: dict[str, Any] = {}
        client.fetch = lambda url, method="GET", headers=None, body=None, _l=left: (
            _l.update(headers=headers or {}) or "{}"
        )
        getattr(client, method)({**params, "settle": "usdt"})
        if {str(v).lower() for v in left["headers"].values() if v is not None} & IDENTITY:
            tagged.append(venue)
    assert tagged == ["bybit", "bingx", "bitget", "mexc"]


def test_a_tag_kept_in_the_clients_default_headers_is_removed() -> None:
    """Gate and Alpaca keep ccxt's identity in the headers every request starts from."""
    for venue, header in (("gate", "X-Gate-Channel-Id"), ("alpaca", "APCA-PARTNER-ID")):
        client = getattr(ccxt, venue)()
        assert header in client.headers, "the control: ccxt does carry it"
        untagged(client)
        assert header not in client.headers


def test_an_okx_order_without_its_own_client_id_is_refused_not_sent() -> None:
    """ccxt puts its broker id in the signed body of an order that has no client id of its own.

    The batch form of the implicit call is where ccxt's signing adds it; the single form does not.
    """
    method = ORDER["okx"][0]
    client = ccxt.okx({"apiKey": "k", "secret": "s", "password": "p"})
    sent: list[str] = []

    def record(url: str, method: str = "GET", headers: Any = None, body: Any = None) -> str:
        sent.append(url)
        return "{}"

    client.fetch = record
    untagged(client)
    with pytest.raises(ccxt.BadRequest, match="6b9ad766b55dBCDE"):
        getattr(client, method)([{"instId": "BTC-USDT"}])
    assert sent == [], "a refused request must not reach the venue"


def test_a_tag_in_the_signed_content_is_found_wherever_it_sits() -> None:
    assert embedded_tags("https://x/api", '{"clOrdId":"x-cvBPrNm9abc"}') == ["x-cvBPrNm9"]
    assert embedded_tags("https://x/api?source=CCXT", None) == ["CCXT"]
    assert embedded_tags("https://x/api", '{"builderCode":"CCXT"}') == ["CCXT"]
    assert embedded_tags("https://x/api", '{"clOrdId":"my-ccxt-desk-1"}') == []


def test_a_header_the_user_set_themselves_is_left_alone() -> None:
    kept = without_ccxt_headers({"Referer": "https://example.test", "X-Own": "1", "source": "CCXT"})
    assert kept == {"Referer": "https://example.test", "X-Own": "1"}


def test_kucoins_partner_headers_leave_together_or_not_at_all() -> None:
    kept = without_ccxt_headers(
        {
            "KC-API-KEY": "k",
            "KC-API-PARTNER": "ccxt",
            "KC-API-PARTNER-SIGN": "s",
            "KC-API-PARTNER-VERIFY": "true",
        }
    )
    assert kept == {"KC-API-KEY": "k"}


def test_a_request_with_no_headers_stays_without() -> None:
    assert without_ccxt_headers(None) is None


# Exchanges whose ccxt source holds the literal "CCXT" or "ccxt" in quotes, as of ccxt 4.5.91. An
# upgrade that adds one fails here, so a person reads what the new literal does before it ships.
# Reviewed: bybit, bingx, gate, mexc, bithumb, cryptocom, pacifica, paradex, toobit, woofipro and
# kucoin. Literal present, meaning not read: the rest. The guard above applies to all of them.
LITERAL_IN_CCXT_SOURCE = {
    "alpaca", "aster", "bingx", "bithumb", "bitopro", "btse", "bybit", "coinbase", "cryptocom",
    "cryptomus", "foxbit", "gate", "kucoin", "mexc", "pacifica", "paradex", "toobit", "weex",
    "whitebit", "woofipro", "xt", "zebpay",
}  # fmt: skip


def test_a_ccxt_upgrade_that_adds_an_identity_literal_is_noticed() -> None:
    root = pathlib.Path(ccxt.__file__).parent
    pattern = re.compile("['\"](CCXT|ccxt)['\"]")
    found = {
        p.stem
        for p in root.glob("*.py")
        if p.stem not in {"__init__", "base"} and pattern.search(p.read_text(encoding="utf-8"))
    }
    assert found == LITERAL_IN_CCXT_SOURCE
