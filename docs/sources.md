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

## Fetching

| `from` | What it is | Key |
|---|---|---|
| `alternative.me` | Crypto Fear & Greed Index, daily | none |
| `fred` (with `id`, such as `DGS10`) | Federal Reserve Economic Data: yields, the dollar, inflation, thousands more | `FRED_API_KEY`, free from fred.stlouisfed.org |
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

## Freshness

`max_age` is measured from the moment a value describes, not from when the file was written. Past
it, the value is still shown in the brief, in the cycle's `series read` table and in `cycle.json`,
marked `stale`, and it is left out of the declared state, the confluence matrix and conviction. A
role whose every series is stale, with no dated text either, is not asked at all: there would be
nothing for it to read that could count. The default is four days; set it to what your feed
actually delivers.

`publish_lag` is the other half: how long after the moment it describes a value became knowable.
A daily yield posted after the close is `1d`; a monthly print released two weeks later is `14d`. A
value is never used before that.

## What each cycle shows

The cycle panel has a `series read` table: the analyst, the series, the value, the moment it
describes, its age in hours, the state your threshold gives it (or `stale`), and where it came from.
`--plain` prints the same lines, and `cycle.json` keeps them under `series`. `tra check` lists every
declared series the same way, and names the stale ones as things to fix.
