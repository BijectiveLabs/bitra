# Where the analysts' figures come from

The technical analyst reads the venue: candles, the book, funding and open interest, pulled through
`data.toml` on every cycle with their age attached. The macro, on-chain and sentiment analysts read
something else: dated series you declare under `sources/`. This page is about those, and about how
you can tell, on any cycle, that they read a real figure and how old it was.

## The short version

| You want | Do this |
|---|---|
| see what each analyst reads, from where, and how old it is | `tra sources show` |
| refresh every fetched series now | `tra sources pull` |
| keep them fresh while a desk runs | nothing: `tra run` refreshes each fetched series once its `refresh` interval has passed |
| know a cycle did not lean on an old figure | a value older than its `max_age` is shown as `stale` and never counted |

## One file per class of data

```
my-desk/sources/
  macro.toml         yields, the dollar, inflation prints
  sentiment.toml     a fear index, a positioning score
  onchain.toml       venue flows, supply held, active addresses
  derivatives.toml   funding history, open interest history, basis
  news.toml          dated text: releases, statements, your own notes
```

Each `[[series]]` names a CSV file and how to read it. The analyst only ever reads that file. What
changes from desk to desk is who keeps the file current: you, or `fetch`.

```toml
[[series]]
name = "fear_greed"
file = "sentiment/fear_greed.csv"
ts = "date"
ts_unit = "iso8601"
value = "value"
unit = "index"
source = "alternative.me Crypto Fear & Greed Index"
max_age = "2d"
fetch = { from = "alternative.me" }
threshold = { bullish_above = 60, bearish_below = 25 }
```

## Adding a series in one step

```bash
tra sources add macro us10y --from fred --id DGS10 --high bearish
tra sources add onchain eth_net_flow --from coinmetrics --id FlowInExNtv --minus FlowOutExNtv \
    --fetch-asset eth --asset ETH --high bearish --band 10
```

The command pulls the series now, writes its file, and appends a `[[series]]` block to
`sources/<class>.toml` with the fetch, how late the source publishes and how old a value may be
for that source, and a threshold drawn from the series' own last year: the 25th and 75th
percentiles by default (`--band 10` draws the 10th and 90th, `--days` sets the window). The block
says where the lines came from, and they are yours to move.

One thing it cannot work out is which side is good for the trade, so it asks: `--high bullish`
when a high value supports a long, `--high bearish` when it argues against one. Some are settled
(a high US yield weighs on crypto), some are a view (one desk reads high greed as momentum,
another as a warning), and the choice is yours either way. A paid vendor goes in
the same way with `--from http --url ... --rows ... --ts ... --value ... --key-env ...`.

## A series about one asset

A chain figure belongs to a chain. Bitcoin's exchange flows say a good deal about a Bitcoin trade and
less about a Zcash one. Name the asset a series is about, and say how much it counts elsewhere:

```toml
[[series]]
name = "eth_exchange_net_flow"
# ...
asset = "ETH"          # full weight on ETHUSDT; left out, a series counts the same everywhere
elsewhere = 0.5        # the share it counts on any other instrument; 0 mutes it there, 1 is full
```

The asset of an instrument is its name without the quote: `ZECUSDT` is `ZEC`. Each row in the brief
carries its asset, and a row counted at less than full says so (`counts`). The change is in the
conviction score only: the analyst still reads every figure, and a series with no `asset` behaves
as it always did.

## What is free, and what is not

Checked against the Coin Metrics community catalog on 2026-10-07, daily frequency, no key:

| Asset | What the community tier carries |
|---|---|
| BTC, ETH | exchange inflow and outflow in coins and in USD (`FlowInExNtv`, `FlowOutExNtv`, `FlowInExUSD`, `FlowOutExUSD`), supply on exchanges (`SplyExNtv`), active addresses, transfer count, MVRV |
| USDT | supply (`SplyCur`), active addresses, transfer count (`TxTfrCnt`); `IssTotUSD` answered zero for every day, so it is not usable |
| XRP, ZEC and most others | supply, transfer count, market cap; no exchange flows |
| SOL | market cap only |

Large-holder and whale figures, per-exchange flows and stablecoin movements between venues are not
in the free tier. They are what a paid provider sells, and the desk reads them the same way: one
`[[series]]` with `fetch = { from = "http", ... }` pointed at that provider, and a threshold. What
the on-chain analyst reads is whatever is declared in `onchain.toml` and `derivatives.toml`; nothing
in the code is tied to a vendor, and a vendor with no declaration is not read.

## Fetching

| `from` | What it is | Key |
|---|---|---|
| `alternative.me` | Crypto Fear & Greed Index, daily | none |
| `fred` (with `id`, such as `DGS10`) | Federal Reserve Economic Data: yields, the dollar, inflation, thousands more | `FRED_API_KEY`, free from fred.stlouisfed.org |
| `coinmetrics` (with `id`, optional `asset`, `minus`) | Coin Metrics community API: exchange flows, active addresses, supply, for many assets; `minus` subtracts a second metric, so inflow less outflow is one net-flow series | none |
| `blockchain.info` (with `id`, such as `n-unique-addresses`) | Blockchain.com charts, Bitcoin only | none |
| `http` | any endpoint that answers JSON over https, including paid on-chain and derivatives vendors | whatever the vendor asks for |

A paid vendor is one line. Take the path, the field names and where the key goes from the vendor's
own docs:

```toml
fetch = { from = "http", url = "https://api.your-vendor.example/v1/metric?asset=BTC",
          rows = "data", ts = "t", ts_unit = "s", value = "v",
          key_env = "VENDOR_API_KEY", key_header = "Authorization", key_prefix = "Bearer " }
```

| Field | Meaning |
|---|---|
| `url` | https only; a redirect off https is refused |
| `rows` | where the list of rows sits in the answer, dotted (`data`, `result.points`); empty when the answer is the list |
| `ts`, `ts_unit` | the time field and its unit: `s`, `ms`, `us`, `ns` or `iso8601` |
| `value` | the number field; a row whose value is not a number is skipped, never guessed |
| `key_env` | the environment variable holding the key (shell, `--env-file`, or the `.env` beside `strategy.toml`) |
| `key_param` | put the key in the query string under this name |
| `key_header`, `key_prefix` | or put it in this header, after this prefix |

A pull merges into the file, so history you already had stays. A pull that fails leaves the file as
it was and says why, with the key blanked out of the message. The desk keeps running; the file's age
decides whether its last value still counts.

Text is never fetched. Headlines from a news API or posts from a forum need your own small exporter,
written under that service's terms, into a `news.toml` file. Scores built from them go in
`sentiment.toml`, which can fetch.

## When an analyst is not asked

A reading analyst with nothing declared for it is not paid to say it saw nothing. It still has a
row in the cycle table, marked `not asked`, with the reason: nothing declared in
`sources/onchain.toml` or `sources/derivatives.toml`, not in `[agents].analysts`, every series past
its `max_age`, or every file unreadable. A role asked but missing from `[conviction].weights` is
flagged by `tra check`: it answers, and its answer moves nothing.

On-chain figures for Bitcoin are read as market-wide context for every instrument a desk trades,
the way a yield is. A desk that wants a chain per instrument declares one series per asset.

## Freshness

`max_age` is measured from the moment a value describes, not from when the file was written. Past
it, the value is still shown in the brief, in the cycle's `series read` table and in `cycle.json`,
marked `stale`, and it is left out of the declared state, the confluence matrix and conviction. A
role whose every series is stale, with no dated text either, is not asked at all: there would be
nothing for it to read that could count. The default is four days; set it to what your feed
actually delivers. A stale series never stops the desk: `tra check` lists it as a note, not
something to fix, and a fetched series is pulled again by the next `tra run`. A weekly series such as
the broad dollar index needs a `max_age` of at least a week plus its publication lag, or it will be
stale for part of every week.

`publish_lag` is the other half: how long after the moment it describes a value became knowable.
A daily yield posted after the close is `1d`; a monthly print released two weeks later is `14d`. A
value is never used before that.

## What each cycle shows

The cycle panel has a `series read` table: the analyst, the series, the value, the moment it
describes, its age in hours, the state your threshold gives it (or `stale`), and where it came from.
`--plain` prints the same lines, and `cycle.json` keeps them under `series`. `tra check` lists every
declared series the same way, and names the stale ones as things to fix.
