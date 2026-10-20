---
description: "How to open an exchange's practice environment and make an API key for trading-research-agents, venue by venue, for demo, testnet and live trading, with the settings each exchange needs."
---

# Exchange accounts and keys

Every exchange makes API keys a little differently, and the differences decide whether the first
order goes through. This page is what we learned making keys for the venues we have run end to end,
written so you can do it once and get it right. It covers the practice environment first, then the
same steps for live trading.

## The short version

1. Open the exchange's practice environment (demo or testnet) and make a key there.
2. Give the key trading permissions only. Never withdrawals, never transfers.
3. Put it in the `.env` beside `strategy.toml`, under the names in the table below.
4. Set `[execution].environment` to that venue's practice environment.
5. Run `tra keys --probe`. It reads the account through the same path an order takes and names
   anything to fix. Nothing is sent.

```toml
[execution]
venue = "bitget"
environment = "demo"       # demo, testnet or mainnet, as the table says for your venue
```

## Venue by venue

| Venue | Practice environment | Where the key is made | `.env` names | `environment` | Exits |
|---|---|---|---|---|---|
| Bybit | Demo Trading | in Demo Trading mode of your account, API Management | `BYBIT_API_KEY`, `BYBIT_API_SECRET` | `demo` | attached |
| OKX | Demo trading | in Demo trading, Personal center, Demo Trading API | `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE` | `testnet` | attached |
| Bitget | Demo trading | in Demo trading mode, API Management | `BITGET_API_KEY`, `BITGET_API_SECRET`, `BITGET_API_PASSPHRASE` | `demo` | attached |
| Binance | Demo Trading | on Binance's Demo Trading site, API Management | `BINANCE_API_KEY`, `BINANCE_API_SECRET` | `demo` | separate |
| BingX | VST (virtual USDT) | your account's own API Management: the same key reaches the demo | `BINGX_API_KEY`, `BINGX_API_SECRET` | `testnet` | attached |
| Gate | Testnet | where Gate issues testnet API keys | `GATE_API_KEY`, `GATE_API_SECRET` | `testnet` | separate |
| Hyperliquid | Testnet | a wallet; an API wallet authorised from it is the safer key | `HYPERLIQUID_WALLET_ADDRESS`, `HYPERLIQUID_PRIVATE_KEY` | `testnet` | attached |

The names are fixed: the venue in capitals, then `API_KEY`, `API_SECRET` and, where the exchange asks
for one, `API_PASSPHRASE`. Exchanges label these fields differently (some call the secret a "secret
key"), so the name you copy from the page is not always the one read here. If a name is close but not
exact, `tra check` and `tra keys --probe` say which name they found
and which one they read; they never print a value.

"Exits" is what `[execution].exits` should be. `attached` sends the stop and take profit with the
entry; `separate` places them as two reduce-only orders right after the fill, for exchanges that do
not take them together through ccxt. See [Venues](/opensource/venues#exits-a-venue-will-not-take-with-the-entry).

### Bybit

Switch your account to Demo Trading and make the key there; a key made in the live account does not
reach the demo. Give it contract orders and positions. Before a live tier session (`tra live`), the
desk also reads the key's own permissions on Bybit and refuses one that can move funds, one that is
read-only, one not bound to an IP (unless you accept that with `[live].key_ip_unbound_ok = true`),
and one expiring within a week.

### OKX

Make the key inside Demo trading; it only works there. Three settings are yours, in the app or
website, in Demo trading for a demo key: the account mode to Futures (Spot mode cannot trade
perpetuals, and OKX does not let a key change it), the position mode to one-way, and nothing else.
The desk sets leverage and isolated margin itself.

### Bitget

The Bitget demo we ran on is a unified trading account, and its key page offers two grants, manage
and trade; give both, and no withdrawal. Set the position mode to one-way. The desk recognises a
unified key by its grants and uses Bitget's unified API, where it sets the leverage and margin mode
and reads them back. A classic account key works too.

### Binance

Make the key on Binance's Demo Trading site. Give it futures trading. The desk trades USD-M futures
for a futures symbol and places the exits right after the fill. If Binance's demo serves an old order
book (it did for hours on 2026-10-20), every order is refused until the book is current, and
`tra keys --probe` says so.

### BingX

BingX has no separate key for its demo. A key made in API Management on your account reaches both the
live API and the demo, VST, and the desk's `environment = "testnet"` is what sends everything to VST.
Because the same key can reach your live account, make it a trading key only, with no withdrawals and
no transfers, bound to your IP. Set the position mode to one-way in the VST futures settings; the
desk refuses hedge mode.

### Gate

Use a key issued for Gate's testnet. The desk trades USDT perpetuals there and places the exits after
the fill. A key without wallet read cannot report its fees, so the desk prices with Gate's standard
fees until it can; add wallet read to let it use your own.

### Hyperliquid

There is no API key: orders are signed by a wallet. Use an API wallet that your main wallet
authorises, so the key on disk can trade but cannot withdraw. The address is your main wallet's.

## Going live

The steps are the same in the live account, with three differences:

| | Practice | Live |
|---|---|---|
| Where the key is made | in the demo or testnet | in the live account |
| `[execution].environment` | `demo` or `testnet` | `mainnet` |
| What else must be true | a signed mandate | a signed mandate, and the ladder at its live tier, promoted with evidence ([The live tier](/opensource/live)) |

For a live key, bind it to the IP of the machine the desk runs on, give it the shortest expiry you are
willing to rotate, keep one key per desk, and keep withdrawals off. The key is read from the
environment or the `.env` beside `strategy.toml`, never from a config file, and any message that
reaches a record has it removed first.

## What we have run

| Venue | Run end to end | Where |
|---|---|---|
| Bybit | yes | demo |
| OKX | yes | demo trading (`testnet`) |
| Bitget | yes, on a unified account | demo |
| Binance | yes, except a close by the venue's own exit; orders wait while its demo book is stale | demo |
| BingX | yes | VST (`testnet`) |
| Gate | yes | testnet |
| Hyperliquid | not yet | testnet |

For Bitget, Binance, BingX and Gate "end to end" is one script, the same on each: an entry with its
exits, the position read back, a second entry refused, a reduce-only close that leaves nothing
resting, a post-only order cancelled at its timeout, a position closed by the venue's own exit, and
the kill path. Bybit and OKX were run before that script existed, by desks trading on their demos.
The details per venue are on [Venues](/opensource/venues).

## Opening an account

| Exchange | Website |
|---|---|
| Bybit | [bybit.com](https://www.bybit.com) |
| OKX | [okx.com](https://www.okx.com) |
| Bitget | [bitget.com](https://www.bitget.com) |
| Binance | [binance.com](https://www.binance.com) |
| BingX | [bingx.com](https://bingx.com) |
| Gate | [gate.com](https://www.gate.com) |
| Hyperliquid | [app.hyperliquid.xyz](https://app.hyperliquid.xyz) |

Whether the exchange is available to you, and on what terms, depends on where you live; the exchange
decides that, not this software. Any exchange on this page works the same way in the software however
you opened your account.
