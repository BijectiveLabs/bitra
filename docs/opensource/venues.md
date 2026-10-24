---
description: Which exchanges work with TRA today through ccxt, how to add one, and the plugin contract for archived data, live feeds and orders.
---

# Venues

A cycle reaches any venue ccxt knows. The replay and live-session paths use plugins, and the ones
that ship (Bybit for history, the live feed and `tra live`; Binance for history) are found the same
way a third-party one is: through a registry. Nothing in the gates, the chain, the runtime or the
agents names a venue, and a test fails the build if one does.

Most people want the next two sections: which venues work today, and how to add the one you trade
on. The plugin contract further down is for a venue ccxt does not know.

There are three plugin kinds: archived data in, live data in, orders out.

| Kind | Protocol | Registry | Entry point group | Used by |
|---|---|---|---|---|
| Archive venue | `data.venues.ArchiveVenue` (+ optional `FundingSource`) | `data.venues.register` | `trading_research_agents.venues` | `tra data fetch`, `tra data audit --venue`, `tra data funding` |
| Live feed | `trading.feeds.LiveFeedFactory` | `trading.feeds.register` | `trading_research_agents.feeds` | `tra paper`, `tra live` |
| Live venue | `trading.venues.LiveVenueFactory` | `trading.venues.register` | `trading_research_agents.live_venues` | `tra live` |

Data that comes from somewhere other than a venue's public archive (a vendor export, a
Client's own recorder) does not need a plugin at all: a column map takes it through the same
gate. See [The data gate](/opensource/data), section "Any other source".

## Which venues work today

A cycle reaches a venue through ccxt, so "which venues are supported" has two honest answers: what
we have run end to end, and what ccxt says a venue can do. The second is read from the library by
`tra venues`, with no network and no key.

| Venue | Type | What a cycle can do there | Practice environment | Keys in `.env` | Run end to end by us |
|---|---|---|---|---|---|
| Bybit | exchange | everything: orders with the stop and take profit attached | demo, testnet | `BYBIT_API_KEY`, `BYBIT_API_SECRET` | yes, on the demo API: real orders, exits attached, a resting order cancelled |
| OKX | exchange | everything | testnet | `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE` | yes, on the demo API: real orders, exits attached, closed by the venue's own exits |
| Bitget | exchange | everything, on a classic or a unified trading account; a unified account key is recognised and sent through Bitget's unified API ([below](#bitget-s-unified-trading-account)) | demo | `BITGET_API_KEY`, `BITGET_API_SECRET`, `BITGET_API_PASSPHRASE` | yes, on the demo API with a unified account: entries with exits attached, a resting order cancelled, a position closed by the venue's own stop, the kill path |
| BingX | exchange | everything; the demo (VST) is reached with the account's own key ([below](#bingx-the-demo-uses-your-account-s-own-key)) | testnet | `BINGX_API_KEY`, `BINGX_API_SECRET` | yes, on the demo API (VST): entries with exits attached, a refused second entry, a reduce-only close, a cancelled post-only order, a close by the venue's own exit, the kill path |
| Binance | exchange | orders, with the stop and take profit placed right after the fill (`exits = "separate"`); `binance` trades USD-M futures for a futures symbol | demo, testnet | `BINANCE_API_KEY`, `BINANCE_API_SECRET` | yes, on the demo API: entries, exits placed after the fill, a close that takes them off, a cancelled post-only order, the kill path. On 2026-10-20 the demo's order book was stamped hours old, and orders are refused until it is current |
| Gate | exchange | orders, exits placed right after the fill (`exits = "separate"`) | testnet | `GATE_API_KEY`, `GATE_API_SECRET` | yes, on the testnet API: entries with exits placed after the fill, a refused second entry, a close that took them off, a cancelled post-only order, a close by the venue's own exit, the kill path |
| WEEX, BloFin, WOO X | exchange | everything | testnet | `<VENUE>_API_KEY`, `<VENUE>_API_SECRET` (and `_API_PASSPHRASE` for WEEX and BloFin) | ready for its run |
| Hyperliquid | decentralised | everything; leverage is confirmed by the venue's reply, since ccxt cannot read it back | testnet | `HYPERLIQUID_WALLET_ADDRESS`, `HYPERLIQUID_PRIVATE_KEY` | ready for its run; [report wanted](https://github.com/BijectiveLabs/bitra/issues/7) |
| Pacifica | decentralised | everything | testnet | `PACIFICA_PRIVATE_KEY` | ready for its run |
| WOOFi Pro, ModeTrade | decentralised (Orderly Network) | everything | testnet | `<VENUE>_API_KEY`, `<VENUE>_API_SECRET`, `<VENUE>_ACCOUNT_ID` | ready for its run |
| KuCoin Futures, Deepcoin | exchange | everything | none | `<VENUE>_API_KEY`, `<VENUE>_API_SECRET`, `<VENUE>_API_PASSPHRASE` | integrated; no practice environment to run on |
| dYdX, Paradex, Extended, Lighter | decentralised | orders, but no stop or take profit at the venue through ccxt (`exits = "none"`); dYdX also cannot set leverage or report fills | testnet | see `tra venues` | not for a desk that needs its exits at the venue |
| Deribit, Crypto.com, Coinbase International | exchange | orders, but leverage cannot be set through ccxt, so a derivative desk refuses, and no exits at the venue | testnet | see `tra venues` | not for a derivative desk |
| HTX, MEXC, CoinEx and others | exchange | orders with `exits = "none"` | none | see `tra venues` | not run |
| Kraken, Kraken Futures, Phemex; Aster, Apex, GRVT, Derive | exchange; decentralised | data for now: ccxt does not report their positions, and the venue hand will not send without one | | | |

No venue is blocked here. The table says two separate things: what ccxt reports a venue can do,
and what we have run ourselves. A venue ccxt says takes orders can be used; the only one the desk
declines is a venue it cannot read the account of, because sending into a position nobody can see
is the one mistake that cannot be undone. Everything else is a limit we state, not a gate we add.

"Everything" means ccxt reports what the venue hand needs: send an order, attach the stop and
take profit to it, read the position and the open orders, cancel, read what became of an order,
read the balance, set the leverage and read the fills. `tra venues` names any of these a venue
lacks. The table above was read from ccxt 4.5.85 on 2026-10-19, and each practice environment was
asked for its markets and an order book with no key: all answered except WOO X's staging API,
which did not respond that day, and the Orderly Network venues, which ask for a key even to list
markets. "Run end to end"
is the stronger claim, a full run with real orders on the venue's demo or testnet API, and Bybit and
OKX are the two that have had it so far, both on their demo APIs. Every other venue that takes
orders is integrated the same way and ready for that run at any time: start it on its practice
environment, watch a few cycles, and read `placed.json` before you point it at money. A report of
that run, from us or from you, moves the venue into the last column.

The default project (`tra init`) prices Bybit and Hyperliquid channels in `envelope.toml`, reads
data from Bybit with Binance behind it in `data.toml`, and trades `bybit/taker-taker` in the
sandbox, where nothing leaves the machine.

## Where an order goes, and what an instrument is

Two things are separate in a desk. `[execution].venue` (and the venue in `[run].venue_key`) is where
orders go, where fees are priced, where positions are read and where discovery looks for
instruments. The chains in `data.toml` are only where candles and books come from, and they may be
other venues. The venue that executes decides what an instrument is; the data venues are asked for
the same market and may refuse it.

Pairs are not spelled alike, and on several venues one id names both a spot market and a
perpetual; ccxt's own lookup of `BTCUSDT` returns the spot market on `binance` and on `bitget`.
Read from ccxt 4.5.84 on 2026-10-08:

| Venue (ccxt id) | Bitcoin perpetual id | Settles in | One contract is | The same id also lists a spot market |
|---|---|---|---|---|
| Bybit (`bybit`) | `BTCUSDT` | USDT, USDC | 1 BTC | yes |
| Binance (`binance`, `binanceusdm`) | `BTCUSDT` | USDT, USDC | 1 BTC | yes on `binance`, no on `binanceusdm` |
| Bitget (`bitget`) | `BTCUSDT` | USDT, USDC | 1 BTC | yes |
| OKX (`okx`) | `BTC-USDT-SWAP` | USDT | 0.01 BTC | no |
| Gate (`gate`) | `BTC_USDT` | USDT | 0.0001 BTC | yes |
| KuCoin Futures (`kucoinfutures`) | `XBTUSDTM` | USDT, USDC | 0.001 BTC | no |
| Hyperliquid (`hyperliquid`) | `0` | USDC only | 1 BTC | no |

So the desk names an instrument by its base and settlement currency, `BTCUSDT` or `ZECUSDC`, and
resolves that name on the venue it is about to ask: a unified symbol such as `BTC/USDT:USDT` as
given, then the venue's own id, then base plus settle. Only linear perpetuals are candidates, so a
name that is spot on one venue is never read as spot, and a name that matches nothing, or two
markets, is refused rather than guessed. Discovery names what it finds the same way, so on OKX it
lists `BTCUSDT`, not `BTC-USDT-SWAP`, and on Hyperliquid `BTCUSDC`.

Two consequences to know:

- **Settlement.** Hyperliquid has no USDT perpetuals. Set `[universe].quote = "USDC"` and name
  instruments `BTCUSDC`.
- **Scaled contracts.** A token that trades per thousand on one venue (`1000PEPE`) and per unit on
  another (`PEPE`) is two different instruments. Data from one will not resolve on the other, and
  the desk says so instead of mixing them.

Sizes are in the base everywhere in the desk: caps, risk, conviction, the records. At the venue they
become contracts, because that is what a venue's order and position are counted in: 0.04 BTC on OKX
is 4 contracts, and 20 million PEPE where a contract is 10 million is 2. A size that is not a whole
number of contracts is refused with both figures, a venue that does not report its contract size is
refused, and what the venue reports back, fills and positions, is read in the base again.

Bybit, OKX, Bitget, Binance, BingX and Gate have been run end to end with real orders, on their
demo or testnet APIs. For the other venues the contract arithmetic and the name resolution are tested against the shapes ccxt returns;
the run against the venue's own account is the step still open, so start any of them on its
practice environment first.

What kind of market a desk may send to is one setting, `[execution].markets`. Every market, on
every venue, is read into one shape from what ccxt reports (see
[Architecture](/opensource/architecture#adoption-rules)), and the order path decides from that
shape what applies: contracts, margin, leverage, where the exits go. Three types are built:
`linear perpetual` (stock perpetuals among them), `spot` (tokenized stocks among them) and
`prediction`. The list defaults to perpetuals only, a desk adds the others by naming them
(`markets = ["linear perpetual", "spot"]`), and a symbol of a type not listed, or not built yet
(an inverse perpetual, a dated future, an option), is refused by name before anything is sent. A
bare name such as `BTCUSDT` resolves to the perpetual; a spot pair is named by its unified symbol,
`SOL/USDT`, in `[data].symbols`.

What the same order path does when the market is spot:

- **Long only.** A short on spot sells coins the account does not hold, which needs a borrow; it is
  refused.
- **Paid in full.** No leverage is set and no liquidation is estimated. The order and its fee must
  fit in `[execution].max_margin_share` of the free quote balance.
- **The position is the coin in the account.** An amount under the venue's minimum order is fee
  residue and counts as nothing; anything above it is the desk's position, so an entry is refused
  while the account already holds the coin. Run a spot desk on an account, or a subaccount, that
  is the desk's own.
- **Exits at the venue.** The stop and take profit go with the entry, as on perpetuals. Bybit keeps
  them as one order with both prices; OKX keeps them as an OCO algo order. Both hold the coins.
- **Closes free the coins first.** A close, by time or by the kill switch, cancels the resting exits,
  wherever the venue keeps them, then sells what is held, rounded down to the venue's step.
- **Fees in the coin.** A spot buy is often charged in the coin bought; the round trip counts that
  fee in the quote currency at the fill price.

And in the cycle, before any order:

- **A short is refused by code** before it is priced, whatever the analyst proposed.
- **Funding and open interest are not asked for.** They belong to contracts; on spot they would be
  refused every cycle and wake the technical auditor for nothing.
- **The fees are the spot pair's.** A spot pair usually costs more than the perpetual on the same
  coin (on Bybit, 10 bps a side against 5.5), so a channel can carry both: its own fees for the
  perpetual and a `[channel.spot]` table for the pair, and a spot cycle is priced at the second.
  `tra check` compares each with what the venue charges for that market and says so when one is
  priced lower.

To set a desk up for spot, name the pair in `data.toml` under `[data].symbols` (for example
`SOLUSDT = "SOL/USDT"`), list `spot` in `[execution].markets`, and add the pair's fees to the
channel:

```toml
[[channel]]
venue = "bybit"
name = "taker-taker"
entry_fee_bps = 5.5
exit_fee_bps = 5.5

[channel.spot]
entry_fee_bps = 10
exit_fee_bps = 10
```
 Spot was run on 2026-10-17: the order path end to end on the Bybit demo and the OKX demo
(SOL/USDT; entry with the exits attached, the exits found at the venue, close, flat), and a full
desk cycle on SOL/USDT in sandbox and in venue mode on the Bybit demo, which priced the round trip
at the spot fee and refused the trade on its break-even rate. Start on a demo or testnet API.

## Stocks on a crypto venue

Several venues list equities as crypto instruments, and they arrive through the same order path as
any other market, because to the venue they are one: a stock perpetual is a linear perpetual (Bybit
`AAPL/USDT:USDT`, OKX `TSLA/USDT:USDT`, Bitget `NVDA/USDT:USDT`), and a tokenized stock is a spot
pair (Bybit `AAPLX/USDT`). Leverage, contract size, exits and closes work as they do for crypto;
the market's own facts decide.

What is different is the underlying. Its exchange keeps hours, and the token does not: it quotes
on a Sunday at a price nobody is making. So:

- **What the underlying is comes from the venue.** Bybit marks stock perpetuals `stock` and
  tokenized stocks `xstocks`; OKX marks contracts with `instCategory` 3. A venue that does not say
  (Bitget, among others) is read as crypto, and you name its equities in `[execution].equities`.
- **An entry waits for the session.** With `[execution].equity_session = "regular"` (the default)
  a cycle on an equity does not ask anyone while the US regular session is closed (09:30 to 16:00
  New York time, Monday to Friday, daylight saving from the time zone database), and refuses a
  trade whose hold would run past that day's close, because the next open can gap past the stop.
  `"any"` trades at every hour, deliberately.
- **Holidays are yours to declare.** The package carries no exchange calendar; list the closed
  dates in `[execution].equity_closed`.
- **On-chain figures do not bear on a share**, so the on-chain analyst is not asked.
- **Some accounts need a step first.** Bybit refuses a stock perpetual until the account has
  signed its agreement for them, in the app or on the website; the refusal names it.
- **Prices stay inside the venue's band.** OKX refuses an order priced outside its current price
  limit, and publishes the limit; a taker entry there is held inside it.

Run on 2026-10-17: a tokenized stock round trip on the Bybit demo (AAPLX/USDT: entry with its
exits, close, flat) and a stock perpetual on the OKX demo (TSLA/USDT:USDT, five contracts of 0.01:
the entry filled inside the price band with its exits attached). That weekend the OKX demo's book
sat outside its band, and the reduce-only close was accepted but not filled; the close is now
confirmed against the account, recorded as not closed when the account still holds, and tried again
on the next pass, while the exits at the venue keep standing.

## Prediction markets

ccxt reaches prediction venues too (Kalshi, Polymarket, Limitless, Myriad and others, in its
`ccxt.prediction` namespace), and a desk names one the way it names an exchange: `kalshi`,
`polymarket`. Two names live on both sides, so their prediction venues are `prediction.binance`
and `prediction.hyperliquid`. ccxt's prediction clients are async; the package runs them on a loop
of their own, so everything above sees the same client it sees for an exchange.

What is traded is an outcome: a share of one answer to one question (YES on "will the Fed cut in
December"), priced between the venue's floor and ceiling, paying 1 if it wins and nothing if it
loses. The venue addresses it by a handle, `MARKET:YES`, and that handle goes in `data.toml`:

```toml
[data]
symbols = { FEDCUT = "FED_CUTS_IN_DECEMBER:YES" }
```

List `prediction` in `[execution].markets`, and the cycle on that instrument is decided by a
different role:

- **The event analyst decides**, not the technical analyst: it reads the question, the outcome,
  when it resolves, the book, the recent prices and your dated notes, and answers with the
  probability that the outcome wins. With nothing beyond the price to go on, the answer is the
  mid with a low confidence, and the desk does nothing.
- **Code prices it to resolution.** Bought at the ask with a fee per share, the share is worth
  `probability - ask - fee`; the desk buys only when that, as a share of the ask, clears the margin.
  A probability that cites nothing from its brief is refused.
- **The stake is the stop.** A share cannot lose more than it cost, so the size is cut until its
  whole cost fits `[controls].max_risk_per_trade_bps` and the position caps. Long only: to bet the
  other way, buy the other outcome.
- **The take profit rests at the venue.** After the fill, the shares are offered at the analyst's
  probability as a resting limit sell; otherwise they are held to resolution or until time closes
  them. A close takes that offer off and sells at the venue's floor, immediate or cancel, since
  these venues take no market order. A share held to resolution is written up at its payout.
- **Funding and open interest are not asked for**, and the screen reads an outcome by its move,
  since the venues publish its prices without a volume.

How a venue charges goes on the channel, as one of two forms:

```toml
[channel.prediction]
fee_coefficient = 0.07     # 0.07 x shares x price x (1 - price), the way Kalshi charges
# or a flat rate on what the share costs:
# entry_fee_bps = 100
# exit_fee_bps = 100
```

A channel that says neither is refused rather than priced at zero.

Run on 2026-10-17: a sandbox cycle on Polymarket's public data (an outcome on the 2028 US
presidential election), where the event analyst answered with the mid and a confidence of 0.2,
citing the book, and the cycle stopped at the confidence floor. The order path is covered by tests
against ccxt's prediction shapes. Sending orders needs a demo or testnet API: Kalshi has a demo
environment that ccxt reaches (an API key and an RSA private key); Polymarket signs with a wallet
and has no practice environment.

## Which venue the data comes from

A desk trades on one venue, the one `[run].venue_key` names, and by default it reads from that
venue alone. The shipped `data.toml` says `venue` in every chain rather than an exchange's name:

```toml
[data.ohlcv]
chain = ["venue", "file:data"]

[data.orderbook]
chain = ["venue"]
```

`venue` is read as the venue orders go to. Candles, funding and open interest come from its live
markets. The book comes from the environment the orders meet, so a desk trading on a practice
account prices on that account's book (`ccxt:okx@testnet`, `ccxt:bybit@demo`), and a desk trading
on mainnet prices on the live one.

What decides and what pays are then the same venue, which is how a professional desk separates
them:

| Figure | Where it may come from | Why |
|---|---|---|
| Order book (spread, depth, the cost a trade is priced at) | the execution venue only | another venue's book is not the book the order meets |
| Funding | the execution venue, or `file:data` | the funding a position pays is its own venue's |
| Candles, open interest | the execution venue, or another venue on purpose | a signal can read a deeper market; it never prices the order |
| The price an order is sent at | the execution venue's book, read again at the moment of sending | the snapshot is as old as the analysts were slow |

In venue mode the first two are enforced: a chain that reads the book or the funding from any
venue other than the one orders go to is refused before anything is pulled, with the line that
names it. A source from a plugin is asked which venue it serves (its `venue` attribute), and one
that does not say is refused the same way: a book of unknown origin is not the venue's book. A candle chain may fall back to another venue deliberately,
`chain = ["venue", "ccxt:binance", "file:data"]`, and `tra check` says so every time. Whatever the
chains say, an order is priced once more against the execution venue's own book when it is sent:
the drift check refuses a decision the market has moved away from, and the slippage bound is
measured from that venue's touch.

## Exits a venue will not take with the entry

Some venues take an order and a stop, but not both in one request. Binance futures is the largest
of them. `[execution].exits` says what the desk does there:

| Setting | What happens |
|---|---|
| `"attached"` (default) | the stop and take profit go out with the entry; a venue that cannot take them refuses the order before anything is sent |
| `"separate"` | the entry goes out alone; the moment it fills, the stop and take profit follow as two reduce-only orders of their own, for what filled |
| `"none"` | no exit at the venue; you carry them yourself, and `tra check` lists that as something to fix |

With `"separate"`, three things keep a position from ever standing unprotected:

1. **A fill whose exits cannot be placed is closed at once.** The leg that did go out comes off, a
   reduce-only market order closes the position, and the record says why.
2. **Exits outlive the position when one of them fires.** The stop closes the trade and the take
   profit is still resting, sized for a position that is gone. Before the next entry on that
   instrument, with nothing held, the desk takes off every exit it finds resting. One that will
   not come off refuses the entry. A close by time or by `tra kill` takes them off as well.
3. **It runs only where the resting exits are read.** Binance keeps a futures stop and take profit
   as algo orders, which ccxt lists and cancels only when asked for them by kind, and that read is
   built in. On a venue where it is not, `"separate"` is refused rather than left to chance.

It is for positions the venue can reduce: perpetuals and futures. A spot desk on the same venue
keeps `"attached"` where the venue takes it, or `"none"`.

There is a moment between the fill and the exits, the time two orders take to send. A desk that
cannot accept it trades where exits go with the entry. Run on Binance's demo API on 2026-10-20:
both exits rested as algo orders, a close took them off, and a fill whose stop the venue would not
take was closed at once.

## Broker tags

Exchanges run broker programmes. Software that sends orders for its users identifies itself with
a tag, and the exchange shares part of the fee it already charges with whoever made the software.
On most venues the user pays the same fee either way. Hyperliquid is the exception: there the
software's share is charged on top.

This package adds no tag of its own today. ccxt, which sends the orders, adds its own on several
venues. As read from ccxt 4.5.85:

| Venue | What ccxt sends | What it costs you |
|---|---|---|
| Bybit | `Referer: CCXT` on every signed request | nothing extra; the share comes out of Bybit's fee |
| BingX | `X-SOURCE-KEY: CCXT` | nothing extra |
| Bitget | `X-CHANNEL-API-CODE` with ccxt's channel code | nothing extra |
| MEXC | `source: CCXT` | nothing extra |
| OKX, Binance, HTX | a broker prefix on the client order id, but only when the order carries no id of its own | nothing, and no tag either: the desk always sends its own client order id |
| Hyperliquid | by ccxt's default, an approval signed with your wallet for ccxt's builder address at up to 0.01%, then a builder fee of 1 basis point on every order. This package turns the fee off: the approval is signed at 0% and the builder is attached at zero | nothing extra by default. With the fee turned back on, **1 bp per order on top of Hyperliquid's fee** |

The default is set on every client that signs with your keys, and `[execution].ccxt_options` is
laid over it, as handed to ccxt:

```toml
[execution]
ccxt_options = { builderFee = true }   # ccxt's Hyperliquid builder fee back on
```

A desk that turns the fee on adds 1 bp to each side of its Hyperliquid channel in `envelope.toml`,
so the round trip the gates price is the one it pays. Credentials are refused in this table; they
come from the environment only.

Bijective plans to join these programmes as a software vendor. When a venue issues an id, it will
be set the same way, shown by `tra check`, and you can replace it or leave it empty. The venue will
be listed on this page first. Until then nothing this package sends carries a Bijective tag.

## Changing venue, and turning the others off

A desk talks to the venues its files name, and nothing else. With the shipped `data.toml` that is
one venue, so the venue a desk trades on is also the only exchange it opens a connection to. Keys
are read for the execution venue only; data sources are never given a key.

Changing venue is one line:

```toml
[run]
venue_key = "okx/taker-taker"        # was "bybit/taker-taker"
```

The chains that say `venue` follow. Two things have to exist on the new venue first, and `tra
check` names whichever is missing: a channel for it in `envelope.toml` with the fees your account
pays there (`tra venues okx --setup` reads them and prints the lines), and its keys in `.env`.
A desk made with `tra init --venue` also names the venue and the environment in `[execution]`;
change those with it, or `tra check` refuses the mismatch, since an order would otherwise go to
one venue at the other's fee.

To trade two venues at once, run two desks, one per venue: each has its own account, positions,
limits and kill switch, and stopping one never touches the other. To stop trading on a venue,
stop its desk (the kill switch flattens and stops it). To stop reading from one, take its name out
of the chains: a kind of data with no source left is refused rather than guessed, and the cycle
says which kind and why. Nothing needs uninstalling, and no code changes.

## Trading on another exchange

The route is the same for any venue ccxt can take orders on. OKX is the example, and its demo is
the first thing to try.

```bash
tra init my-desk --profile autonomous --venue okx      # data, orders and fees wired for OKX
cd my-desk
# put OKX_API_KEY, OKX_API_SECRET and OKX_API_PASSPHRASE in .env, and your model key
tra keys --probe --prepare        # fills the fees, reads the account, sets one-way on a demo
tra mandate --sign "Your Name" --venue-mode
tra check                                             # says what is left, if anything
```

What `--venue` does: the data chains read from the venue the desk trades on, `[run].venue_key` and
`[execution].venue` name it, `[execution].environment` is its demo or testnet API where it has one,
leverage is 3x isolated, one order is capped at 200, the exits are placed the way that venue takes
them, the order mode stays `sandbox`, and `.env.example` lists the keys that venue signs with. An
agent desk sizes each order at 100 of the quote currency. Sizes use the venue's own step, so there
is no table of increments to copy. What to set once at the exchange is printed at the end.

Fees are filled in for you. `tra check`, `tra keys --probe` and `tra run` ask the account for its maker and taker
fee and write them on the blank lines of `envelope.toml`, with a comment that says where they came
from. When the account cannot report them (no keys yet, or a demo or testnet API that does not
answer: Bybit's demo returns a parameter error for every fee request), the venue's standard tier
from its market list is written instead and labelled so; it is the base tier, the one a new
account pays, so an account on a better tier pays less than the gate prices, never more. Those lines are replaced
by the account's own figure as soon as it reports one. A fee you write yourself is never replaced,
and `tra check` warns when it is below what your account pays.

OKX's demo trading is reached through ccxt's sandbox switch, so in this project it is
`environment = "testnet"`. A demo key only works on the demo.

A demo or testnet API has its own order book, and it can sit apart from the live one: on 2026-10-10
OKX's demo FIL book was 6 bps wide against 1 bps live. So the book a desk prices on is the
practice environment's when its orders go there (`venue` reads as `ccxt:okx@testnet` or
`ccxt:bybit@demo`), and the drift check, the spread and the slippage a desk records are measured on
the book its orders meet. Candles, funding and open interest stay on the live market. Any
`ccxt:<venue>` source takes `@demo` or `@testnet`.

The price can drift too. On OKX's demo API, in 6 of 52 decisions recorded to 2026-10-13, the
demo book's mid sat outside the live market's range for that minute: 30 to 40 bps on NEAR and 149
bps once on MAGIC. The analyst reads live candles, while the cost and the entry are priced on the
demo book. Treat demo results on thin instruments as a test of the plumbing, not of the strategy;
Bybit's demo book stayed inside the live range in all 63 recorded decisions.

Three things only you can do, in the OKX app or website (in Demo trading for a demo key): set the
account mode to Futures mode (Menu, Settings, Trading mode), because an account in Spot mode cannot
trade perpetuals at all and OKX does not let an API key change it; set the position mode to
one-way (net), because the desk refuses long/short mode (`tra keys --probe --prepare` does this on
a demo or testnet account); and turn venue mode on when you are ready
(`tra mandate --sign "Your Name" --venue-mode`; on mainnet the ladder as well).

What the desk does for OKX that it does not need to for Bybit: margin mode and leverage are applied
as isolated (ccxt would otherwise default to cross), the client order id is letters and digits only
and at most 32 characters, sizes are sent as contracts, and the stop and take profit go with the
entry as attached algo orders at the market price. The request ccxt builds for a limit IOC entry
with both was inspected offline on 2026-10-08 and carries `tdMode` isolated, `ordType` ioc,
`attachAlgoOrds` and the id. It has been sent to OKX's demo API since 2026-10-10: entries
filled with both exits attached, and positions closed by those exits at the venue.

### Binance futures

`venue = "binance"` with a futures symbol (`BTC/USDT:USDT`) trades USD-M futures: the client is sent
to the futures API, since ccxt's `binance` class otherwise reads the spot wallet for the balance.
The position is read from Binance's position list, the balance from Binance's own rows where
ccxt's parse comes back empty (as it did on the demo API), and a whole leverage is sent as an
integer, which Binance requires. Exits go out after the fill (`exits = "separate"`).

Every order is priced against the venue's book at the moment of sending, and a book the venue
stamped more than 30 seconds ago is refused, on any venue. On 2026-10-20 Binance's demo API served a
book 3.2 hours old, about 35 bps from where its own trades printed; `tra keys --probe` names it.

### BingX: the demo uses your account's own key

BingX has no separate key page for its demo. A key made in API Management on your account reaches
both its live API and its demo, VST (virtual USDT), at `open-api-vst.bingx.com`; which one is used
depends only on where the request goes. With `environment = "testnet"` the desk sends everything to
VST, and mainnet stays behind the ladder and the mandate like everywhere else. Because the key can
reach the live account, make it a trading key only: no withdrawals, no transfers, bound to your IP.

### Gate

`venue = "gate"` with a futures symbol trades USDT perpetuals: the client is sent to the futures
account and loads only the markets of the symbol's kind (Gate's testnet timed out on its full
lists). Gate keeps the leverage on the position, so the desk reads it from the position list, where
isolated margin shows as a leverage above zero. Client order ids carry Gate's required `t-` prefix
and fit its 28 characters. When the venue's stop closes a position, the take profit it placed after
the fill is still resting; it is taken off before the next entry on that instrument and by
`tra kill`, as on every venue with exits placed after the fill.

### Bitget's unified trading account

Bitget's demo offers the unified trading account, and its key page then shows two grants, manage
and trade. ccxt reaches that account with its `uta` option for orders, positions, balances, fills
and leverage, but still changes the margin mode and reads the leverage through Bitget's classic
futures API, which a unified key cannot reach. The desk handles the difference:

- The key's grants are read once, when the client is built. A key with only unified grants is
  sent through the unified API; `uta` in `[execution].ccxt_options`, either way, is not overruled.
- On that account the leverage is kept per symbol and per margin mode, and both can exist at once.
  The desk sets the declared leverage in the declared margin mode, reads it back from that row, and
  sends the margin mode with every order, the close included, because an order that names none
  trades crossed.
- The position mode is read from the account settings, and long/short (hedge) mode is refused.
- ccxt reports a fee Bitget charged as a negative cost there (a rebate, read literally); the fee
  is taken from Bitget's own `feeDetail` instead, and a filled order with no average price is
  priced from its own fills.

What you set, once, in Bitget's Demo trading: the position mode to one-way, and funds in the demo
account. Run on 2026-10-20 at 3x isolated on BTCUSDT: entries filled with the stop and take
profit attached as Bitget's position exits, a second entry was refused while one was open, a
reduce-only close left nothing resting, a post-only order was cancelled at its timeout, and a
position was closed by its own stop at the venue three minutes after entry. Bitget refuses a take
profit that is not beyond the order's own limit price, so a target inside the slippage band of a
taker entry is refused by the venue: nothing fills, and the cycle records Bitget's answer.

## Adding a venue you trade on

Four steps, and the command writes most of them for you.

**1. Ask what the venue can do.**

```bash
tra venues okx
```

```
okx
  data        funding, ohlcv, open_interest, orderbook
  execution   create_order: yes, attached_exits: yes, position: yes, open_orders: yes, cancel: yes
  practice    testnet
  keys        OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE
  verdict     venue mode ready: orders with the stop and take profit attached, account readable
```

That is read from ccxt, not promised here: no network, no key, no order. The `keys` line is what
this venue signs with, taken from ccxt's own list. Some want a passphrase, and Hyperliquid signs with
a wallet address and a private key instead of an API key.

**2. Print the blocks to paste.**

```bash
tra venues okx --setup
```

It prints what goes in `envelope.toml` (two channels, taker and maker), the line to change in
`data.toml`, the `[run]`, `[execution]` and `[live]` tables for `strategy.toml` with the practice
environment already chosen, and the variable names for `.env`. For a venue that cannot attach exits
it adds `exits = "separate"` where the venue takes them as orders of their own and this package
reads where they rest, else `exits = "none"`, and says why; for one that can only feed data it
prints the data line and nothing else.

The fee lines come filled in. The fee is the biggest number in the cost gate and it depends on
your account's tier, so the setup asks the account first and falls back to the venue's standard
tier, labelled as such, when the account cannot answer yet (see above).

**3. Fill in the blanks.** Your two fees, each instrument's size step (`size_step` as a table, one
per instrument), and the keys in `.env`. If an instrument's ccxt symbol is not the obvious one, add
it under `[data].symbols`.

**4. Check, then prove the keys.**

```bash
tra check            # the three files read against each other
tra keys --probe     # an account read through the same client an order would use; nothing sent
```

`tra check` refuses a desk whose files disagree: an instrument the sources cannot map, data from a
venue whose fees you are not pricing, or an `[execution].venue` that is not the venue in
`venue_key`. `tra keys --probe` names a missing passphrase, a key the venue rejects, and a clock
more than a second away from the venue's (the venue hand refuses to send in that case too).

What protects you once orders flow, on any venue: a size off the venue's step or under its minimum
is refused rather than quietly rounded by ccxt; the account is read before every order; an open
position or a working order refuses a second entry; and a maker order that does not fill is
cancelled, never chased.

## A venue ccxt does not know

Write a plugin. The three kinds below are the whole contract: an archive venue for history, a live
feed for the public tape, a live venue for orders. Each is a small class registered by entry point.

## An archive venue

```python
import csv
from collections.abc import Iterable, Iterator
from datetime import date
from trading_research_agents.data import Rejection, Trade


class MyVenue:
    name = "my-venue"  # what `--venue` accepts; lower-cased on registration
    cadence = "daily"  # or "monthly": how the venue slices its archives

    def archive_url(
        self, instrument: str, *, day: date | None, month: tuple[int, int] | None
    ) -> str:
        if day is None:
            raise ValueError("my-venue archives are daily; pass --day")
        return f"https://archive.example/{instrument}/{day.isoformat()}.csv.gz"

    def checksum_url(self, archive_url: str) -> str | None:
        return None  # or the URL of a published digest, which fetch verifies

    def normalise(self, lines: Iterable[str], *, instrument: str) -> Iterator[Trade | Rejection]:
        # Rows to canonical trades. A bad row is a Rejection with its line, never dropped.
        for number, row in enumerate(csv.reader(lines), start=1):
            ...
```

`normalise` is held to the same standard as the bundled ones: it checks the layout it was
written against on every file, and stops with `ArchiveFormatError` and the header it actually
saw when the layout drifted. A row that cannot become a trade is a `Rejection` with its line
number and reason. The audit counts them; it does not forgive them.

Register it one of two ways.

In your own package, as an entry point in `pyproject.toml`; installed next to
`bitra`, it is found on first use:

```toml
[project.entry-points."trading_research_agents.venues"]
my-venue = "my_package.venue:MyVenue"
```

Or in a script, before the CLI or the gate is used:

```python
from trading_research_agents.data.venues import register

register(MyVenue())
```

An entry point may name the class or an instance; a class is instantiated with no arguments.

## A live feed

Every venue ccxt streams is already a live feed, through `trading/feeds/ccxt_trades.py`, with no
key. A plugin is for a venue ccxt does not stream, and it takes precedence for its name. It is a
factory. Given an instrument, a stop event and a reconnect budget, it
returns a `Feed`: an iterator of `Trade` that the runtime pulls prints from.

```python
import threading
from trading_research_agents.trading.feed import Feed


class MyFeedFactory:
    name = "my-venue"

    def __call__(
        self, instrument: str, *, stop: threading.Event, max_reconnects: int | None
    ) -> Feed:
        return MyPublicFeed(instrument, stop=stop, max_reconnects=max_reconnects)
```

```toml
[project.entry-points."trading_research_agents.feeds"]
my-venue = "my_package.feed:MyFeedFactory"
```

`tra trade shadow` picks the feed from the venue half of `[run].venue_key` in
`strategy.toml` (`my-venue/taker-taker` selects `my-venue`), or from `--feed NAME`. A name it
cannot stream is refused before any connection is opened.

What the ccxt feed does, and a plugin should: quantities in the base, each trade once, in time
order; a trade that does not parse stops the feed rather than being skipped; a dropped connection
is reopened and counted, up to the reconnect budget. The runtime does not care how the venue
frames a trade. It cares that every `Trade` is exact
`Decimal`, timestamped in UTC nanoseconds, and that silence is reported rather than waited out.

## A live venue

Every exchange ccxt knows is already a live venue for `tra live`, through
`trading/venues/ccxt_live.py`, on the same client and hand venue mode sends with; nothing needs
writing for one of them. A plugin is for a venue ccxt does not cover, and it takes precedence for
its name. It is a factory that takes `LiveVenueSettings` (instrument, channel,
funding schedule, environment, run id, the two deliberate overrides) and the environment
mapping, and returns an object satisfying `trading.venue.Venue` plus `preflight()`. The
factory declares which environment variables it needs (`env_keys`) so the CLI can say which
one is missing, and which environments it knows (`environments`).

```python
class MyLiveFactory:
    name = "my-venue"
    env_keys = ("MY_VENUE_API_KEY", "MY_VENUE_API_SECRET")
    environments = ("demo", "mainnet")

    def __call__(self, settings: LiveVenueSettings, env: Mapping[str, str]) -> LiveVenue:
        return MyLiveVenue(settings, env)
```

```toml
[project.entry-points."trading_research_agents.live_venues"]
my-venue = "my_package.live:MyLiveFactory"
```

What the runtime expects of it, and what the ccxt live venue does: `routes_exits = True`;
`submit` accepts one intent at a time and sends it with an idempotent client order id;
`poll(trade)` confirms the fill from the venue's own order state and reconciles the position
after every fill, raising `VenueError` on any disagreement; closes are reduce-only;
`preflight()` refuses a clock too far from the venue's, a key with more or fewer permissions than
a trade key where the venue reports them, and a position the run was not told to adopt; `cost_of` charges the fees
the venue reported. [The live tier](/opensource/live) is the review a new adapter is held to, and
`tests/test_live_venue.py` is the shape of the tests it should ship with.

## The bundled plugins

| Name | Archive | Funding | Live feed | Live venue |
|---|---|---|---|---|
| `bybit` | daily `.csv.gz`, header row, no published checksum | `GET /v5/market/funding/history`, paged | through ccxt, like every venue | through ccxt, like every venue |
| `binance` | monthly `.zip` of one CSV, no header, `.CHECKSUM` sidecar verified | none | through ccxt, like every venue | through ccxt, like every venue |

The archives live in `data/venues/`, and a venue's account checks that ccxt does
not unify (OKX's account mode, Bybit's key permissions) in `trading/venues/accounts.py`, Bitget's
unified account in `trading/venues/margin.py` and `trading/venues/fills.py`; nowhere
else names a venue. They
are examples of the protocols as much as they are integrations, and a plugin for another
venue is expected to be about the same size.

## Fees, latency, settlement

None of that is in the plugin. Fees per channel, measured latency and the settlement clock are
declared in `envelope.toml` and `strategy.toml` for whichever venue a run is on. A venue plugin
moves bytes; the operating envelope says what trading there costs.
