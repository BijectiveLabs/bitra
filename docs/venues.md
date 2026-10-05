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
gate. See `docs/data.md`, section "Any other source".

## Which venues work today

A cycle reaches a venue through ccxt, so "which venues are supported" has two honest answers: what
we have run end to end, and what ccxt says a venue can do. The second is read from the library by
`tra venues`, with no network and no key.

| Venue | What a cycle can do there | Practice environment | Keys in `.env` | Run end to end by us |
|---|---|---|---|---|
| Bybit | everything: orders with the stop and take profit attached | demo, testnet | `BYBIT_API_KEY`, `BYBIT_API_SECRET` | yes, on demo: real orders, exits attached, a resting order cancelled |
| OKX | everything | testnet | `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE` | not yet |
| Bitget | everything | demo | `BITGET_API_KEY`, `BITGET_API_SECRET`, `BITGET_API_PASSPHRASE` | not yet |
| BingX | everything | testnet | `BINGX_API_KEY`, `BINGX_API_SECRET` | not yet |
| KuCoin Futures | everything | none | `KUCOINFUTURES_API_KEY`, `KUCOINFUTURES_API_SECRET`, `KUCOINFUTURES_API_PASSPHRASE` | not yet |
| Hyperliquid | everything | testnet | `HYPERLIQUID_WALLET_ADDRESS`, `HYPERLIQUID_PRIVATE_KEY` | not yet |
| Binance | orders, but you carry the stop yourself (`exits = "none"`) | demo, testnet | `BINANCE_API_KEY`, `BINANCE_API_SECRET` | not yet; it is the data fallback in the default project |
| Deribit, HTX, Gate, MEXC | orders with `exits = "none"` | varies, see `tra venues` | `<VENUE>_API_KEY`, `<VENUE>_API_SECRET` | not yet |
| Kraken, Kraken Futures, Phemex | data only for a cycle: they cannot report the position, and the venue hand will not send without it | | | |

"Everything" means ccxt reports the five things the venue hand needs: send an order, attach the
stop and take profit to it, read the position, read the open orders, and cancel. "Run end to end"
is the stronger claim, and only Bybit has earned it. Start any other venue on its practice
environment, watch a few cycles, and read `placed.json` before you point it at money.

The default project (`tra init`) prices Bybit and Hyperliquid channels in `envelope.toml`, reads
data from Bybit with Binance behind it in `data.toml`, and trades `bybit/taker-taker` in the
sandbox, where nothing leaves the machine.

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
it adds `exits = "none"` and says why; for one that can only feed data it prints the data line and
nothing else.

The fees are left blank, deliberately. The fee is the biggest number in the cost gate and it depends
on your account's tier, so a guessed figure would price every trade at a rate nobody checked. The
file will not load until you write yours in.

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

## Stopping a venue

Take it out of the chains in `data.toml`. A kind of data with no source left is refused rather than
guessed, and the cycle says which kind and why. If it was the venue you traded on, change
`[run].venue_key` to a channel you still have, and `tra check` will tell you if anything still
points at the old one. Nothing needs uninstalling, and no code changes.

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
`trading-research-agents`, it is found on first use:

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

A live feed plugin is a factory. Given an instrument, a stop event and a reconnect budget, it
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
`strategy.toml` (`my-venue/taker-taker` selects `my-venue`), or from `--feed NAME`. A name
with no plugin is refused before any connection is opened.

What the bundled feed does, and a new one should: a subscribe that is not acknowledged within
its deadline is not a subscription; a link carrying nothing for two ping intervals is dead even
if the socket looks open; a frame it does not recognise is counted and shown, not dropped. The
runtime does not care how the venue frames a trade. It cares that every `Trade` is exact
`Decimal`, timestamped in UTC nanoseconds, and that silence is reported rather than waited out.

## A live venue

A live venue plugin is a factory that takes `LiveVenueSettings` (instrument, channel,
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

What the runtime expects of it, and what the bundled adapter does: `routes_exits = True`;
`submit` accepts one intent at a time and sends it with an idempotent client order id;
`poll(trade)` confirms the fill from the venue's own order state and reconciles the position
after every fill, raising `VenueError` on any disagreement; closes are reduce-only;
`preflight()` refuses a key with more or fewer permissions than a trade key, a clock too far
from the venue's, and a position the run was not told to adopt; `cost_of` charges the fees
the venue reported. `docs/live.md` is the review a new adapter is held to, and
`tests/test_live_venue.py` is the shape of the tests it should ship with.

## The bundled plugins

| Name | Archive | Funding | Live feed | Live venue |
|---|---|---|---|---|
| `bybit` | daily `.csv.gz`, header row, no published checksum | `GET /v5/market/funding/history`, paged | public v5 trade stream, `[shadow]` extra | v5 REST, market IOC, canary only |
| `binance` | monthly `.zip` of one CSV, no header, `.CHECKSUM` sidecar verified | none | none | none |

They live in `data/venues/`, `trading/feeds/` and `trading/venues/` and nowhere else. They
are examples of the protocols as much as they are integrations, and a plugin for another
venue is expected to be about the same size.

## Fees, latency, settlement

None of that is in the plugin. Fees per channel, measured latency and the settlement clock are
declared in `envelope.toml` and `strategy.toml` for whichever venue a run is on. A venue plugin
moves bytes; the operating envelope says what trading there costs.
