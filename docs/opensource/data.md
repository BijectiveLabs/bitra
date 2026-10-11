# The data gate

A wrong number is more dangerous than a missing number. A missing number stops the pipeline;
somebody notices, and the cost is an afternoon. A wrong number produces a confident verdict
that propagates into decisions made weeks later by people with no reason to doubt it.

Everything in `trading_research_agents.data` fails closed. A row that cannot become a trade is
counted and shown with its line and reason; it is never dropped silently and never defaulted.
A file whose layout drifted from the one the normaliser was written against stops the run
with the header it actually sent.

## The canonical trade

| Field | Type | Meaning |
|---|---|---|
| `ts_ns` | int | UTC epoch nanoseconds |
| `venue` | str | the plugin's name, or the `source` of a column map |
| `instrument` | str | the source's symbol, e.g. `BTCUSDT` |
| `price` | Decimal | as written by the source, scale preserved |
| `qty` | Decimal | as written by the source, scale preserved |
| `side` | `buy` / `sell` | the taker's side |
| `trade_id` | str | the source's own id |
| `seq` | int or none | a monotonic sequence when the source provides one |

Zero is not a price and not a quantity. A trade with either is refused at construction,
because a zero there is a defaulted field.

## Sources

Data reaches the gate one of two ways, and the gate does not distinguish them afterwards.

A **venue plugin** knows where a venue's public archive lives and how its rows are laid out.
Two ship with the package; any other is a small class registered under the
`trading_research_agents.venues` entry point (see [Venues](/opensource/venues)). `tra data fetch --venue`
and `tra data audit --venue` take any registered name.

A **column map** takes any other CSV: a vendor export, a Client's own recorder, a venue with
no plugin yet. See the next section.

### Bundled venue plugins

Both publish their trade history without an API key.

| Venue | URL | Layout |
|---|---|---|
| Bybit | `https://public.bybit.com/trading/<SYMBOL>/<SYMBOL><YYYY-MM-DD>.csv.gz` | header row; `timestamp` in seconds with fractional part; `side` is `Buy`/`Sell`; `trdMatchID` is the trade id |
| Binance spot | `https://data.binance.vision/data/spot/monthly/trades/<SYMBOL>/<SYMBOL>-trades-<YYYY-MM>.zip` | one CSV, no header: `trade_id, price, qty, quote_qty, time, is_buyer_maker, is_best_match`; `time` in milliseconds before 2025 and microseconds from 2025-01 |

The Binance time unit is inferred from the digit count (13, 16 or 19) and any other width is
rejected. `is_buyer_maker = True` means the buyer rested and the seller crossed, so the taker
side is `sell`.

Archives are read in place (`.csv.gz`, `.zip` with one member, or `.csv`); nothing is unpacked
to disk.

### Any other source: a column map

A `mapping.toml` names every field and its unit; nothing is guessed.

```toml
[mapping]
source = "vendor-x"            # recorded as the trade's venue field
ts = "timestamp"
ts_unit = "ms"                 # s | ms | us | ns | iso8601 (must carry a timezone)
price = "price"
qty = "amount"
trade_id = "id"
side = { column = "side", buy = ["buy", "BUY"], sell = ["sell", "SELL"] }
# seq = "sequence"             # optional monotonic sequence
# instrument = "symbol"        # optional; rows for another instrument are rejected
has_header = true
delimiter = ","
# columns = ["timestamp", "price", "amount", "side", "id"]   # headerless files only
```

```bash
tra data audit export.csv --mapping mapping.toml --instrument BTCUSDT --max-gap 5m \
    --manifest data/manifest.json --parquet data/btc.parquet
```

Columns are matched by name, in any order. A column the map names and the file lacks stops
the file with the header it actually had. A row whose time, price, quantity or side cannot
be read is a `Rejection` with its line number. An ISO 8601 time without a timezone is
rejected rather than assumed UTC. The audit, the manifest and the Parquet that follow are
the same as for a venue archive; the manifest records `source` as the venue.

## Live sources, and why they come in chains

An archive is yesterday. A cycle needs what the market is doing now, and the way to get that
without letting an agent near a vendor is to have the engine fetch it, stamp it, and write it
down. That is `data/sources/` and `tra data pull`.

```bash
pip install 'trading-research-agents[ccxt]'
tra data pull BTCUSDT                       # chains from data.toml, snapshot into runs/pulls/
```

```toml
[data.ohlcv]
chain = ["ccxt:bybit", "ccxt:binance", "file:data"]
ttl = "90s"                    # older than this is not a price, it is a memory
interval_s = 60
limit = 240
on_exhausted = "refuse"        # refuse | use_stale_with_flag
```

Three rules do the work.

- **A chain, not a source.** The first source that answers inside `ttl` wins. Every attempt is
  written into the snapshot, so a rate limit on a free endpoint reads as a line rather than as a
  mystery. `file:data` at the end of a chain means an operator with no keys and no quota still
  gets a cycle, from the days already in `data/`.
- **Every value carries its age.** Age is measured in market time, not in the time we fetched:
  a candle's age is counted from when its interval closed. Nothing older than `ttl` is handed to
  a decision. The `ttl` for candles is written for one-minute bars: on a longer bar the newest
  closed candle is up to a whole bar old before the next one closes, so the system adds that wait.
  A fifteen-minute bar with `ttl = "90s"` is fresh for 15m30s after it closed, and no longer. When the chain is exhausted the default is to refuse the kind of data outright;
  `use_stale_with_flag` is available, and then the snapshot marks the value stale and says so in
  every report that follows.
- **Public data only.** These sources are never given a key, which is why they cannot place an
  order even by accident. Execution reads its own credentials in its own package.

Source names are `scheme:target`. `ccxt:<venue>` is that venue's public endpoints through ccxt,
which speaks one interface to more than a hundred venues; `file:data` is the project's own
directory. A Client with a paid on-chain or derivatives vendor registers a source under the
`trading_research_agents.sources` entry point and then names it in a chain, exactly like a venue
plugin: this package never depends on a vendor to support one.

One snapshot per cycle, written where the cycle keeps the rest of its papers:

```json
{
  "cycle_id": "2026-09-27T10-13-30Z",
  "instrument": "BTCUSDT",
  "values": {
    "funding": {"source": "ccxt:bybit", "tier": "free", "age_s": 0.4, "rate": "0.0000611"},
    "orderbook": {"source": "ccxt:bybit", "tier": "free", "age_s": 0.5, "spread_bps": "0.01"}
  },
  "attempts": [{"kind": "ohlcv", "source": "ccxt:binance", "outcome": "error", "detail": "RateLimitExceeded"}],
  "stale": [],
  "refused": {}
}
```

Measured on one venue's public endpoints on 2026-09-27: a closed minute candle arrived sixteen
seconds after its close, a twenty-five level book half a second old, a funding rate under a
second old, and open interest stamped fifteen minutes back. That last one is the reason every
kind has its own `ttl`: one number for all of them would either reject the open interest a venue
publishes slowly or accept a book that is no longer a book.

### A venue's own parameters

`[data.<kind>].params` is a table handed to the source verbatim. Venues differ in ways no generic
adapter should pretend away, and the operator's file is where a vendor's spelling belongs.

The case that made this necessary, measured on 2026-09-28 against ccxt 4.5.84: asked plainly, Bybit
answers `fetch_open_interest` with the last hourly bucket, so the figure is up to an hour old and
any freshness window under an hour refuses by construction. Asked for a finer bucket it answers
about two minutes back:

```
[data.open_interest]
chain = ["ccxt:bybit"]
ttl = "10m"
params = { intervalTime = "5min" }
```

The parameters a kind was fetched with are recorded beside the value, because they are part of why
the age is what it is. Both `tra data pull` and `tra run` pass them.

## The shelf: what the reading analysts are given

Live values come in chains, above. Everything a reading analyst sees comes from the shelf instead:
one declaration file per class of data, beside the config, under `sources/`.

| File | Class | Shape | Read by |
|---|---|---|---|
| `sources/macro.toml` | dated levels and rates | `[[series]]`, one per file | `macro_analyst` |
| `sources/news.toml` | dated text | `[[news]]`, one per file | `macro_analyst`, `sentiment_analyst` |
| `sources/sentiment.toml` | dated scores | `[[series]]` | `sentiment_analyst` |
| `sources/onchain.toml` | dated chain figures | `[[series]]` | `onchain_analyst` |
| `sources/derivatives.toml` | dated funding, open interest, basis | `[[series]]` | `onchain_analyst` |

The split is by class of data, not by vendor. A vendor is a line in a file, so a desk that moves
from a free export to a paid subscription edits one entry and nothing else changes: not the code,
not the briefs, not the prompts. That also means no vendor's format becomes the shape everything
else has to match.

Nothing on the shelf fetches. Each entry names a local file the operator produced, with its
columns, its timestamp unit and its `publish_lag`, and the series engine applies the same as-of
rule to all of them. `[[news]]` entries name dated-text files (`when,what[,source]`) and the macro
analyst reads them as events while the sentiment analyst reads them as notes.

`tra init` writes all five files as forms with the entries commented out. A form declares nothing,
so an untouched project asks only the technical analyst. `tra check` prints what each file
declares, flags an export that is declared but missing, and prints the roster.

## The audit

```python
from datetime import timedelta
from trading_research_agents.data import archive_venue, audit, open_archive_lines

rows = archive_venue("bybit").normalise(open_archive_lines(path), instrument="BTCUSDT")
report = audit(rows, max_gap=timedelta(minutes=5))
report.passed, report.findings
```

With a column map, `mapped_trades(lines, load_mapping(path), instrument=...)` stands in for
the plugin's `normalise`; everything after it is the same call.

One pass, bounded memory. `max_gap` is declared by the caller because it belongs to the
instrument: a quiet hour on a thin listing is normal, on the largest perpetual it is a dead
feed, and the file cannot tell the two apart.

| Check | Severity | Fires when |
|---|---|---|
| `empty` | fail | no row became a trade |
| `rejected_rows` | fail above `max_rejected_fraction` (default 0.1 percent), else warn | rows could not become trades |
| `mismatch` | fail | a row is for another venue or instrument |
| `duplicates` | fail | a trade id repeats within a window of 10,000 rows |
| `out_of_order` | fail | a timestamp goes backwards |
| `sequence_resets` | fail | a sequence number does not increase |
| `gaps` | warn | an interval between trades exceeds `max_gap`; the five largest are listed |
| `price_scale`, `qty_scale` | warn | the number of decimal places changes within the file |
| `concentration` | warn | one UTC day carries more than half the rows of a multi-day file |

`passed` is true when at least one row became a trade and nothing failed. Warnings are kept in
the manifest for the reader to judge; the audit does not judge them.

## The manifest

Written beside the data as `manifest.json`: venue, instrument, first and last timestamp (UTC),
rows accepted and rejected, every source file with its SHA-256 and size, the normaliser
version, the audit result with all findings, and the creation time. A manifest that does not
parse as this schema is refused, and a manifest is never written for a dataset with no rows.

## The dataset

`pip install 'trading-research-agents[data]'` adds `pyarrow`, and with it the storage contract:
one Parquet file per archive, price and quantity as `decimal128(38, s)` with the scale the
venue actually uses, timestamps as UTC nanoseconds, `zstd` compressed, the scales recorded in
the file's own metadata.

```python
from trading_research_agents.data.parquet import read_trades, write_trades

write_trades(trades, path, price_scale=2, qty_scale=3)
for trade in read_trades(path):
    ...
```

- A value that cannot be represented exactly at the declared scale is refused, and no file is
  left behind. A wider scale pads; it never rounds.
- Reading returns the trades exactly as written, nanoseconds included, so a round trip is an
  equality check and not an approximation.
- `tra data audit ... --parquet out.parquet` writes the dataset only when the audit passed,
  in a second pass over the archive, with the scales the audit observed.

## One command for a day

```bash
tra data get BTCUSDT 2025-01-01          # venue from strategy.toml, or --venue
```

fetches the archive, audits it, writes `data/BTCUSDT-2025-01-01.parquet` with its manifest,
and, when the venue plugin publishes funding, `data/BTCUSDT-funding.json` for the day and
its neighbours. `--max-gap` defaults to `[data].max_gap` in `strategy.toml`, else five
minutes, and the choice is printed. The steps below are what it does, one by one, for anyone
who needs them apart.

## Downloading

`tra data fetch --venue NAME` asks the venue plugin for the archive URL and, when the venue
publishes one, the URL of its checksum. The archive streams to a `.part` file, is hashed as
it arrives, checked against the declared length and the checksum, and renamed only then. A
mismatch leaves nothing on disk.
