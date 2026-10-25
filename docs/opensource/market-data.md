---
description: "The market data archive: one-minute flow bars built from venue archives, derivatives history, gap marks, and how every day file is verified."
---

# Market data

A candle says where the price went. A flow bar also says who moved it. TRA builds one-minute flow
bars from a venue's own trade archive, with the taker's side of every trade, and keeps the
market's derivatives history beside them: funding, open interest, mark and index prices and the
long/short ratio, as the venue published them.

## One command

```bash
tra data bars BTCUSDT 2025-07-01 --to 2025-07-31 --venue bybit
tra runs verify --market data
```

The first downloads each day's archive through `tra data get` if it is not already in `data/`,
builds 1,440 one-minute bars per day, and asks the venue for the derivatives history of the same
days. A day already in the archive, verified and built the same way, is kept as it is, so running
the command again only builds what is missing. The second checks every day file against its
manifest.

## What a flow bar holds

| Field | Meaning |
|---|---|
| `open`, `high`, `low`, `close` | from trades, in venue time; empty for a minute with no trades |
| `volume_base`, `volume_quote` | what traded, in the base and in the quote |
| `taker_buy_base`, `taker_sell_base` | the volume by the taker's side; they add up to the volume |
| `trade_count` | trades in the minute, each trade id once |
| `large_trade_base` | volume from trades of at least `--large-trade-quote` notional, when declared |
| `late_trades` | trades that arrived after the minute was closed; counted, never added |
| `gap` | `none`, `partial` or `missing`: whether the minute was seen whole |

A quiet minute (no trades, `gap = none`) and a minute nobody saw (`gap = missing`) are different
facts. A day whose archive could not be fetched is not written; it reads as missing, and
`gaps.jsonl` records why. Running the command again fills it.

Every other interval is resampled from the minutes, exactly: a fifteen-minute or one-hour bar is
the same whether it was resampled or built directly from the trades.

## Derivatives

| Series | Known at | Read as current for at most |
|---|---|---|
| funding | its settlement | 9 hours |
| open interest | the end of the venue's bucket | 15 minutes |
| mark, index | the close of the one-minute candle | 2 minutes |
| long/short ratio | the end of the venue's bucket | 2 hours |

A day's derivatives file holds every value that became known during that UTC day, so the candle
of 23:59 belongs to the next day, the moment it closes. A value older than its limit is unknown,
with the reason; it is never carried forward. A series a venue does not offer is reported as
unsupported. If a series the venue does offer cannot be fetched, no derivatives day is written
and the command exits 1, so a day missing one series never reads as a day the venue had nothing
for. A day asked for after it ended is kept on the next run; one asked for while it was still
running is asked again. Open interest is kept as ccxt reports it, with the unit it was measured in; what
reads it uses changes, which do not depend on the unit.

Derivatives history is backfilled for Bybit perpetuals. Binance's public archive is spot, which
has none. Bybit's long/short ratio for past days cannot be reached through ccxt today, so it is
archived as zero points and reads as unknown.

## On disk

```text
data/market/<venue>/<instrument>/flow_1m/<YYYY-MM-DD>.parquet
data/market/<venue>/<instrument>/flow_1m/<YYYY-MM-DD>.manifest.json
data/market/<venue>/<instrument>/deriv/<YYYY-MM-DD>.parquet
data/market/<venue>/<instrument>/deriv/<YYYY-MM-DD>.manifest.json
data/market/<venue>/<instrument>/gaps.jsonl
```

Prices and sizes are stored as exact decimals; a value that does not fit its declared scale is
refused, not rounded. Each manifest holds the file's sha256, its rows, the minutes that were
partial or missing, and what the day was built from. A day file whose bytes no longer match its
manifest is refused by every reader and named by `tra runs verify --market`.

Measured on BTCUSDT, Bybit, 2025-07-01, on one home connection: the flow day file is 70,486 bytes
and the derivatives day file 29,901 bytes. The first run took 477 seconds, most of it spent
downloading the 39.6 MB archive and auditing its 1,116,387 trades; from a day already audited in
`data/`, building the flow bars took 16 seconds and the derivatives backfill 24.
