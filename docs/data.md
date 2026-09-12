# The data gate

A wrong number is more dangerous than a missing number. A missing number stops the pipeline;
somebody notices, and the cost is an afternoon. A wrong number produces a confident verdict
that propagates into decisions made weeks later by people with no reason to doubt it.

Everything in `trading_research_agent.data` fails closed. A row that cannot become a trade is
counted and shown with its line and reason; it is never dropped silently and never defaulted.
A file whose layout drifted from the one the normaliser was written against stops the run
with the header it actually sent.

## The canonical trade

| Field | Type | Meaning |
|---|---|---|
| `ts_ns` | int | UTC epoch nanoseconds |
| `venue` | str | `bybit`, `binance` |
| `instrument` | str | venue symbol, e.g. `BTCUSDT` |
| `price` | Decimal | as written by the venue, scale preserved |
| `qty` | Decimal | as written by the venue, scale preserved |
| `side` | `buy` / `sell` | the taker's side |
| `trade_id` | str | the venue's own id |
| `seq` | int or none | a monotonic sequence when the venue provides one |

Zero is not a price and not a quantity. A trade with either is refused at construction,
because a zero there is a defaulted field.

## Sources

Both venues publish their trade history without an API key.

| Venue | URL | Layout |
|---|---|---|
| Bybit | `https://public.bybit.com/trading/<SYMBOL>/<SYMBOL><YYYY-MM-DD>.csv.gz` | header row; `timestamp` in seconds with fractional part; `side` is `Buy`/`Sell`; `trdMatchID` is the trade id |
| Binance spot | `https://data.binance.vision/data/spot/monthly/trades/<SYMBOL>/<SYMBOL>-trades-<YYYY-MM>.zip` | one CSV, no header: `trade_id, price, qty, quote_qty, time, is_buyer_maker, is_best_match`; `time` in milliseconds before 2025 and microseconds from 2025-01 |

The Binance time unit is inferred from the digit count (13, 16 or 19) and any other width is
rejected. `is_buyer_maker = True` means the buyer rested and the seller crossed, so the taker
side is `sell`.

Archives are read in place (`.csv.gz`, `.zip` with one member, or `.csv`); nothing is unpacked
to disk.

## The audit

```python
from datetime import timedelta
from trading_research_agent.data import audit, bybit_trades, open_archive_lines

lines = open_archive_lines(path)
report = audit(bybit_trades(lines, instrument="BTCUSDT"), max_gap=timedelta(minutes=5))
report.passed, report.findings
```

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

`pip install 'trading-research-agent[data]'` adds `pyarrow`, and with it the storage contract:
one Parquet file per archive, price and quantity as `decimal128(38, s)` with the scale the
venue actually uses, timestamps as UTC nanoseconds, `zstd` compressed, the scales recorded in
the file's own metadata.

```python
from trading_research_agent.data.parquet import read_trades, write_trades

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

## Downloading

`tra data fetch` streams the archive to a `.part` file, hashes it as it arrives, checks it
against the declared length and, for Binance, against the venue's published `.CHECKSUM`, and
renames it only then. A mismatch leaves nothing on disk.
