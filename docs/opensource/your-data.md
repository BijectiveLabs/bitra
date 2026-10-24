---
description: "Bringing your own data to TRA: trade history, dated series and live feeds from a vendor, in CSV, JSON or Parquet, normalised and checked before any agent reads it."
---

# Your own data

Most desks already have data: a vendor subscription, a recorder of their own, years of exports.
None of it has to be reshaped by hand to fit this package. You say what each column is and what
unit it is in, and the package does the rest: it reads the file, turns every row into its own
shape, checks it, and tells you about every row it could not use. An agent never reads your file
directly. It reads the figures the system computed from it, with their age and where they came from.

## What goes where

| You have | Formats | Where it goes | What happens to it |
|---|---|---|---|
| trade history (prints) | CSV, `.csv.gz`, a `.zip` of one CSV, Parquet | `tra data audit FILE --mapping mapping.toml` | each row becomes a canonical trade, the file is audited, and a clean dataset is written as Parquet with its manifest. Backtests, research sessions and the `file:data` link of a live chain read it from there |
| a dated series: rates, flows, a sentiment score, funding history, anything with a time and a value | CSV, JSON, Parquet | one `[[series]]` entry in `sources/<class>.toml` | read with the publication lag you declare, so a figure counts only from when it was knowable; graded against your thresholds; shown with its age; ignored past its `max_age` |
| dated text: releases, statements, your own notes | CSV, JSON | `sources/news.toml` | read as events by the macro analyst and as notes by the sentiment analyst |
| a live feed from a data vendor: candles, the book, funding, open interest | anything your code can read | a source plugin, named in a chain in `data.toml` | asked like any other source, its answer stamped with its age and origin, refused when older than the chain's `ttl` |

## How it is stored

Whatever shape a file arrives in, the package keeps one shape of its own, and where the data is
large it keeps it in a columnar store:

| Data | Arrives as | Kept as |
|---|---|---|
| trade history | a venue archive, a CSV export, your own Parquet | the canonical trade, one Parquet file per dataset under `data/`: prices and sizes as `decimal128` at the scale the source used, time as UTC nanoseconds, `zstd` compressed, with a `manifest.json` beside it (source files and their SHA-256, row counts, the audit's findings) |
| candles for a cycle | built from that Parquet, or fetched live | in the cycle's `snapshot.json`, with the source and age of each value |
| dated series and text | CSV, JSON, Parquet | read in place, through the as-of rule, on every cycle; they are small, and a file you correct is read as corrected on the next cycle |
| what the desk decided | written by the desk | one directory per cycle (briefs, answers, intents, orders) as JSON, and one index line per cycle per day |

There is no database server to run. The datasets are plain Parquet, so the same files open in
DuckDB, Polars, pandas or Spark for your own analysis, without an export step:

```sql
-- DuckDB, over every dataset of one instrument
SELECT date_trunc('hour', ts) AS hour, count(*) AS trades, sum(qty) AS volume
FROM 'data/BTCUSDT-*.parquet' GROUP BY 1 ORDER BY 1;
```

## The rules every file is held to

These are the same for a venue's archive, a vendor's export and a file you wrote yourself:

- **Every unit is declared.** A timestamp column says whether it is seconds, milliseconds,
  microseconds, nanoseconds or ISO 8601. Nothing is inferred from the size of a number.
- **A time needs a zone.** An ISO time with no offset is refused, not assumed to be UTC. A bare
  date (`2026-10-01`) is the one exception: it is read as UTC midnight, and the publication lag
  moves it to when the figure was actually known.
- **Prices and sizes stay exact.** They are read as decimals with the precision the source used,
  stored as `decimal128` in Parquet, and never rounded. A value that cannot be stored exactly at
  the declared scale is refused.
- **A bad row is shown, not dropped.** A row whose time, price, size or side cannot be read
  becomes a rejection with its row number and the reason. Above 0.1 percent of rejected rows the
  audit fails. A missing column stops the file with the header it actually had.
- **Zero is not a value.** A trade with a zero price or size is a defaulted field, and refused.
- **A row about something else is refused.** With an instrument column declared, a row for
  another instrument is a rejection, not a silent mix.

## Trade history

A `mapping.toml` names each field:

```toml
[mapping]
source = "vendor-x"            # recorded as the trade's venue
ts = "time"
ts_unit = "ns"                 # s | ms | us | ns | iso8601
price = "px"
qty = "size"
trade_id = "id"
side = { column = "aggressor", buy = ["B"], sell = ["S"] }
# instrument = "symbol"        # optional; rows for another instrument are rejected
```

```bash
tra data audit vendor.parquet --mapping mapping.toml --instrument BTCUSDT --max-gap 5m \
    --manifest data/manifest.json --parquet data/BTCUSDT-2025-01-01.parquet
```

```
vendor-x BTCUSDT: 2000 trades, 0 rejected
2025-01-01 00:00:00Z to 2025-01-01 00:33:19Z
wrote 2000 trades to data/BTCUSDT-2025-01-01.parquet (decimal scales 2/3)
PASSED
```

The audit checks order, duplicates, gaps, sequence resets and changes of scale; the full list is
in [The data gate](/opensource/data#the-audit). The dataset is written only when the audit passed.

## Parquet

A Parquet file carries types that a CSV leaves to the map, so each type is read the way its
meaning requires and then goes through the same map as a CSV:

| Parquet type | Read as | Declare |
|---|---|---|
| timestamp with a time zone | nanoseconds since the epoch, exact | `ts_unit = "ns"` |
| timestamp without a zone | a time with no offset, refused by every `ts_unit` | store it with its zone |
| date | `YYYY-MM-DD`, UTC midnight before the publication lag | `ts_unit = "iso8601"` |
| decimal | its digits as stored, scale kept | |
| float | the shortest text that reads back as the same float | |
| null | an empty cell; a row that needs it is rejected | |

Reading Parquet needs the `data` extra (`pip install 'bitra[data]'`).

## Dated series

```toml
[[series]]
name = "us10y"
file = "macro/us10y.parquet"   # .csv, .json or .parquet
ts = "date"
ts_unit = "iso8601"
value = "yield"
unit = "percent"
publish_lag = "1d"             # known the day after the date it describes
source = "your vendor"
max_age = "4d"
threshold = { bearish_above = 4.5, bullish_below = 3.5 }
```

How series reach the analysts, how they are refreshed and how their age is shown is in
[Data sources](/opensource/sources).

## A live feed from a vendor

A vendor that answers in real time arrives as a source plugin: a small class in your own package,
registered under the `trading_research_agents.sources` entry point and then named in a chain like
any bundled source. This package never depends on the vendor.

```toml
# pyproject.toml of your plugin package
[project.entry-points."trading_research_agents.sources"]
vendorx = "your_package.vendorx:source"
```

```python
import time
from decimal import Decimal

from trading_research_agents.data.schema import Candle
from trading_research_agents.data.sources.base import Fetched, SourceError


class VendorX:
    name = "vendorx"
    tier = "paid"

    def kinds(self) -> tuple[str, ...]:
        return ("ohlcv",)

    def fetch(self, kind: str, instrument: str, **options) -> Fetched:
        rows = your_client.candles(instrument, options["interval_s"], options["limit"])
        if not rows:
            raise SourceError(f"vendorx has no candles for {instrument}")
        candles = [
            Candle(r.start_ns, Decimal(r.o), Decimal(r.h), Decimal(r.l), Decimal(r.c), Decimal(r.v))
            for r in rows
        ]
        closed = candles[-1].start_ns + options["interval_s"] * 1_000_000_000
        return Fetched(kind, instrument, candles, self.name, self.tier, time.time(), closed)


def source(target: str, settings: dict) -> VendorX:
    return VendorX()
```

```toml
# data.toml
[data.ohlcv]
chain = ["vendorx", "venue", "file:data"]
ttl = "90s"
```

The source does the normalising: whatever the vendor sends, what comes back is the package's own
shape, with the market time the value describes. In venue mode the book and the funding rate must
come from the venue the orders go to, so a plugin that serves them says which venue with a `venue`
attribute, and one that does not say is refused for those two kinds. Candles and open interest may
come from anywhere you choose, and `tra check` names the source each kind comes from.

## Not covered yet

- A file of candles alone, without the trades they were built from, as a dataset. Today candles
  for a backtest are built from trades; a live chain can take candles from a source plugin.
- Order book history from a file.

Both are on the roadmap. Until then, a plugin covers live candles and books from any vendor.
