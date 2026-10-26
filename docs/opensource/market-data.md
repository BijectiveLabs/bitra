---
description: "The market data plane: one-minute flow and book bars, derivatives, liquidations and dated text, from venue archives and from the recorder, in a verified archive."
---

# Market data

A candle says where the price went. A flow bar also says who moved it. TRA keeps one-minute flow
bars, with the taker's side of every trade, beside what a crypto desk trades on: the order book,
liquidations, funding, open interest, mark and index prices, the long/short ratio, and the dated
text a desk reads. Two commands fill the archive, and every reader checks it.

| Command | Fills | From |
|---|---|---|
| `tra data bars` | flow bars and derivatives history for past days | the venue's daily trade archive and its REST history |
| `tra record` | flow bars, book bars, liquidations, derivatives and text, as they happen | the venue's live streams, its REST history, your news feeds |
| `tra runs verify --market` | nothing; checks every file | the archive |

Both build flow bars with the same builder, so a minute recorded live and the same minute rebuilt
later from the venue's archive are the same object, unless a late trade or a gap mark says why not.

## Past days: `tra data bars`

```bash
tra data bars BTCUSDT 2025-07-01 --to 2025-07-31 --venue bybit
tra runs verify --market data
```

The first downloads each day's archive through `tra data get` if it is not already in `data/`,
builds 1,440 one-minute bars per day, and asks the venue for the derivatives history of the same
days. A day already in the archive, verified and built the same way, is kept as it is, so running
the command again only builds what is missing. The second checks every file against its manifest.

Measured on BTCUSDT, Bybit, 2025-07-01, on one home connection: the flow day file is 70,486 bytes
and the derivatives day file 29,901 bytes. The first run took 477 seconds, most of it spent
downloading the 39.6 MB archive and auditing its 1,116,387 trades; from a day already audited in
`data/`, building the flow bars took 16 seconds and the derivatives backfill 24.

## The recorder

A book cannot be bought back later, and neither can a liquidation stream: what was not recorded
while it happened is gone. `tra record` keeps them, under a supervisor like a desk, or for a bounded
time with `--once 30m`.

```toml
# data.toml
[record]
venue = "bybit"
instruments = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
streams = ["trades", "book", "liquidations"]   # where the venue supports them
book_sample_ms = 1000                           # one look at the book a second
book_levels = 50
requests_per_minute = 300                       # this process's share of the venue's REST limit
context = ["BTCUSDT", "ETHUSDT"]                # recorded too, for cross-asset features
text = ["coindesk"]                             # feeds from sources/news.toml, or "all"

[record.large_trade_quote]
BTCUSDT = 100000
```

```bash
tra record                 # until stopped
tra record --once 30m      # for a first look, or a scheduled job
tra record status          # coverage, gaps and latency over the last day
```

What it does while it runs:

- **Trades** stream into the flow builder, placed by the venue's own time. A minute closes when a
  trade two seconds past its end arrives, or five seconds after its end by the clock, whichever is
  first. A trade seen twice is dropped by its id; a trade for a minute already closed is counted,
  never added.
- **The book** is sampled once a second, not kept whole, into a book bar per minute: the mean and
  widest spread, the imbalance of the first ten levels, and the size resting within ten basis
  points of the mid.
- **Liquidations** are kept as they stream, where the venue streams them. Which side was liquidated
  is read from the venue's own documentation: on Bybit a `Buy` means a long was liquidated. A venue
  whose meaning has not been read keeps its raw side, and the liquidation feature says it cannot
  tell.
- **Derivatives levels** (funding, open interest, mark, index, long/short) are backfilled from the
  venue's history every five minutes, through the same code as `tra data bars`, so a level kept
  live and one rebuilt later are the same.
- **Text** from the news feeds you declare is fetched on each feed's own refresh, and every headline
  is kept once, at the moment the recorder first saw it. Its publisher's time is kept beside it and
  never used to decide what was known, because a headline can be dated earlier than anyone could
  read it. The typed judge in `[judge]` reads them into typed events for the text features
  ([The typed judge](/opensource/llm#the-typed-judge)).
- **Hours** are written as parts once they can no longer change: three minutes after the hour for
  flow and the book, five minutes after it for derivatives and text, since a venue publishes a
  bucket after it closes. A closed day is compacted into one file once every stream has written
  its last hour of it.
- **A silent connection** is noticed only when its keepalive gives up, up to a minute and a half
  later with ccxt. What it did not deliver was not seen, so the recorder marks the stream unseen
  from its last message, not from the moment the silence was noticed; holding each hour for three
  minutes is what lets it mark minutes that had already closed.
Every disconnect, reconnect, compaction and minute of latency goes to `data/market/events.jsonl`,
one structured line each; `tra record status` reads it.

## What a flow bar holds

| Field | Meaning |
|---|---|
| `open`, `high`, `low`, `close` | from trades, in venue time; empty for a minute with no trades |
| `volume_base`, `volume_quote` | what traded, in the base and in the quote |
| `taker_buy_base`, `taker_sell_base` | the volume by the taker's side; they add up to the volume |
| `trade_count` | trades in the minute, each trade id once |
| `large_trade_base` | volume from trades of at least the declared notional, when declared |
| `late_trades` | trades that arrived after the minute was closed; counted, never added |
| `gap` | `none`, `partial` or `missing`: whether the minute was seen whole |

A quiet minute (no trades, `gap = none`) and a minute nobody saw (`gap = missing`) are different
facts. Every other interval is resampled from the minutes, exactly: a one-hour bar is the same
whether it was resampled or built directly from the trades.

## What a book bar holds

| Field | Meaning |
|---|---|
| `samples` | looks at the book inside the minute; zero means the book was not seen |
| `spread_bps_mean`, `spread_bps_max` | the quoted spread over the samples, in basis points of the mid |
| `imbalance_top10_mean` | (bid - ask) / (bid + ask) over the first ten levels each side, from -1 to 1 |
| `depth_bid_10bps_sum_base`, `depth_ask_10bps_sum_base` | the size resting within ten basis points of the mid, summed over the samples, in the base |
| `mid_close` | the mid at the last sample |

Spread and imbalance are statistics and are kept as floats; depth is a sum of quantities and stays
exact, its mean computed when read.

## Derivatives

| Series | Known at | Read as current for at most |
|---|---|---|
| funding | its settlement | 9 hours |
| open interest | the end of the venue's bucket | 15 minutes |
| mark, index | the close of the one-minute candle | 2 minutes |
| long/short ratio | the end of the venue's bucket | 2 hours |
| liquidation | the moment the venue stamped it | an event, counted over a window |

A day's derivatives file holds every value that became known during that UTC day. A value older
than its limit is unknown, with the reason; it is never carried forward. A series a venue does not
offer is reported as unsupported. If a series the venue does offer cannot be fetched, no
derivatives day is written and the command exits 1, so a day missing one series never reads as a
day the venue had nothing for. Open interest is kept as ccxt reports it, with the unit it was
measured in; what reads it uses changes, which do not depend on the unit.

Derivatives history is backfilled for Bybit perpetuals; Binance's public archive is spot, which
has none. Bybit's long/short ratio is asked by the venue's own period names (`5min`, not `5m`)
and by windows of one page each, because the venue ignores `since` on that endpoint.

## Gaps

| What happened | What the archive says |
|---|---|
| a stream dropped | the minute it dropped in is partial, the minutes after it are missing until a message arrives, and the span is in `gaps.jsonl` with the error |
| a connection went silent before its keepalive noticed | the same, counted from the last message it delivered; the `disconnected` event carries `silent_since` |
| the recorder was not running | the minutes before it started and after it stopped are missing, with a gap mark each |
| a later run covers the same hour | it fills only the minutes the earlier run missed; what was seen stays |
| a day's archive could not be fetched | nothing is written; the day reads as missing and `gaps.jsonl` records why; running again fills it |
| an hour no run covered | written as missing minutes when the day is compacted, with a gap mark |

## On disk

```text
data/market/<venue>/<instrument>/flow_1m/<YYYY-MM-DD>.parquet
data/market/<venue>/<instrument>/flow_1m/<YYYY-MM-DD>/part-<HH>.parquet   while the day is open
data/market/<venue>/<instrument>/book_1m/...
data/market/<venue>/<instrument>/deriv/...
data/market/text/<feed>/text/...
data/market/<venue>/<instrument>/gaps.jsonl
data/market/events.jsonl
```

Each file has a manifest beside it (`.manifest.json`) with its sha256, its rows, the minutes that
were partial or missing, what it was built from, and its scales. Prices and sizes are stored as
exact decimals; a value that does not fit its declared scale is refused, not rounded. A file whose
bytes no longer match its manifest is refused by every reader and named by `tra runs verify
--market`. Compaction checks every part before it writes the day, and removes the parts only once
the day file is written and verified, so a crash leaves the parts or the day, never neither.

