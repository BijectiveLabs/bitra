"""What the system hands the analysts: vendor and venue presets, fetched news, and a
reading analyst's own label where nothing is declared.

Every request goes through a recorded answer, in the shape the vendor's reference documents or the
venue answered on 2026-10-18, so nothing here reaches the network.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.data.news import news_due, plain, pull_news
from trading_research_agents.data.pull import PullError, plan, pull
from trading_research_agents.data.series import (
    NewsSpec,
    SeriesError,
    SeriesSpec,
    Threshold,
    load_news_map,
)

NOW = datetime(2026, 10, 18, 3, 0, tzinfo=UTC)
NOW_NS = int(NOW.timestamp() * 1_000_000_000)


class Recorded:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.asked: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: Mapping[str, str]) -> bytes:
        self.asked.append((url, dict(headers)))
        return self.body


def spec(fetch: dict[str, Any], name: str = "s") -> SeriesSpec:
    return SeriesSpec(
        name=name,
        file=f"{name}.csv",
        ts="date",
        ts_unit="iso8601",
        value="value",
        unit="",
        publish_lag=timedelta(0),
        source="test",
        threshold=Threshold(),
        fetch=fetch,
    )


# -- presets ------------------------------------------------------------------------------------


def test_a_vendor_preset_carries_its_path_its_query_and_its_key_header(tmp_path: Path) -> None:
    answer = {
        "code": "0",
        "data": [
            {
                "time": 1658966400000,
                "aggregated_long_liquidation_usd": 5916885.1,
                "aggregated_short_liquidation_usd": 12969583.9,
            }
        ],
    }
    get = Recorded(json.dumps(answer).encode())
    fetch = {
        "from": "coinglass",
        "id": "futures/liquidation/aggregated-history",
        "params": {"symbol": "BTC", "interval": "1h", "exchange_list": "Binance,OKX"},
        "value": "aggregated_short_liquidation_usd",
        "minus": "aggregated_long_liquidation_usd",
    }
    got = pull(spec(fetch), tmp_path, env={"COINGLASS_API_KEY": "k"}, get=get)
    url, headers = get.asked[0]
    assert url.startswith("https://open-api-v4.coinglass.com/api/futures/liquidation/aggregated-")
    assert "symbol=BTC" in url and "exchange_list=Binance%2COKX" in url
    assert headers["CG-API-KEY"] == "k" and got.rows == 1
    assert (tmp_path / "s.csv").read_text().splitlines()[1].endswith(",7052698.8")


def test_glassnode_and_cryptoquant_put_the_key_where_their_references_say() -> None:
    glass = plan({"from": "glassnode", "id": "transactions/transfers_volume_exchanges_net"}, "g")
    assert glass["url"] == (
        "https://api.glassnode.com/v1/metrics/transactions/transfers_volume_exchanges_net"
    )
    assert (glass["key_header"], glass["ts"], glass["value"], glass["ts_unit"]) == (
        "X-Api-Key",
        "t",
        "v",
        "s",
    )
    quant = plan({"from": "cryptoquant", "id": "btc/exchange-flows/netflow"}, "q")
    assert quant["key_prefix"] == "Bearer " and quant["rows"] == "result.data"


def test_a_venue_answering_rows_as_arrays_is_read_by_position(tmp_path: Path) -> None:
    get = Recorded(b'{"code":"0","data":[["1792292400000","1.79"],["1792292100000","1.80"]]}')
    fetch = {
        "from": "okx",
        "id": "contracts/long-short-account-ratio",
        "params": {"ccy": "BTC", "period": "5m"},
        "value": "1",
    }
    got = pull(spec(fetch), tmp_path, get=get)
    assert get.asked[0][0] == (
        "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=BTC&period=5m"
    )
    assert got.rows == 2 and "Authorization" not in get.asked[0][1]


def test_params_that_are_not_a_table_are_refused() -> None:
    with pytest.raises(PullError, match="params is a table"):
        plan({"from": "bybit", "id": "account-ratio", "params": "symbol=BTCUSDT"}, "b")


# -- news ---------------------------------------------------------------------------------------

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Desk feed</title>
<item><title>Spot ETF sees &lt;b&gt;record&lt;/b&gt; inflow</title>
<pubDate>Sat, 18 Oct 2026 02:30:00 GMT</pubDate></item>
<item><title>Old story</title><pubDate>Sat, 04 Oct 2026 02:30:00 GMT</pubDate></item>
<item><title></title><pubDate>Sat, 18 Oct 2026 02:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>top : CryptoCurrency</title>
<entry><title>Daily discussion</title><updated>2026-10-18T01:00:00+00:00</updated></entry>
</feed>"""


def news(fetch: dict[str, Any], **kw: Any) -> NewsSpec:
    return NewsSpec(name="n", file="news/n.csv", source=kw.pop("source", ""), fetch=fetch, **kw)


def test_a_feed_becomes_dated_lines_and_old_ones_are_let_go(tmp_path: Path) -> None:
    first = pull_news(
        news({"from": "rss", "url": "https://example.com/rss"}),
        tmp_path,
        get=Recorded(RSS),
        now=NOW,
    )
    rows = (tmp_path / "news/n.csv").read_text(encoding="utf-8").splitlines()
    assert rows == [
        "when,what,source",
        "2026-10-18T02:30:00+00:00,Spot ETF sees record inflow,Desk feed",
    ]
    assert first.added == 1
    again = pull_news(
        news({"from": "rss", "url": "https://example.com/rss"}, source="r/CryptoCurrency"),
        tmp_path,
        get=Recorded(ATOM),
        now=NOW,
    )
    assert again.rows == 2 and again.added == 1
    assert "Daily discussion,r/CryptoCurrency" in (tmp_path / "news/n.csv").read_text()


def test_a_feed_with_a_document_type_is_not_parsed(tmp_path: Path) -> None:
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel/></rss>'
    with pytest.raises(PullError, match="document type"):
        pull_news(
            news({"from": "rss", "url": "https://example.com/rss"}), tmp_path, get=Recorded(bomb)
        )


def test_a_json_news_api_is_read_by_the_fields_named(tmp_path: Path) -> None:
    body = {
        "results": [
            {
                "title": "Exchange halts withdrawals",
                "published_at": "2026-10-18T02:50:00Z",
                "source": {"title": "Wire"},
            },
            {"title": "no time"},
        ]
    }
    fetch = {
        "from": "http",
        "url": "https://news.example.com/posts/",
        "rows": "results",
        "source": "source.title",
        "key_env": "NEWS_KEY",
        "key_param": "auth_token",
    }
    get = Recorded(json.dumps(body).encode())
    got = pull_news(news(fetch), tmp_path, env={"NEWS_KEY": "t"}, get=get, now=NOW)
    assert got.rows == 1 and "auth_token=t" in get.asked[0][0]
    assert "Exchange halts withdrawals,Wire" in (tmp_path / "news/n.csv").read_text()


def test_text_is_reduced_to_one_plain_line() -> None:
    assert plain("<p>Fed &amp; ECB\n\n hold</p>\x07") == "Fed & ECB hold"
    assert len(plain("x" * 400)) == 280


def test_a_news_declaration_names_how_it_is_fetched(tmp_path: Path) -> None:
    path = tmp_path / "news.toml"
    path.write_text(
        '[[news]]\nname = "wire"\nfile = "news/wire.csv"\nrefresh = "5m"\n'
        'fetch = { from = "rss", url = "https://example.com/rss" }\n',
        encoding="utf-8",
    )
    (wire,) = load_news_map(path)
    assert wire.refresh == timedelta(minutes=5) and wire.fetch == {
        "from": "rss",
        "url": "https://example.com/rss",
    }
    assert news_due(wire, tmp_path, NOW)
    path.write_text(
        '[[news]]\nfile = "n.csv"\nfetch = { from = "scrape", url = "https://x" }\n',
        encoding="utf-8",
    )
    with pytest.raises(SeriesError, match="rss"):
        load_news_map(path)
