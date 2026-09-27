# Venues are plugins

The package knows no venue. Two ship with it as plugins, in their own directories, and are
found the same way a third-party one is: through a registry. Nothing in the gates, the chain,
the runtime or the agent names a venue, and a test fails the build if one does. The research
this project descends from ran on particular venues; the software does not inherit that.

There are three plugin kinds: archived data in, live data in, orders out.

| Kind | Protocol | Registry | Entry point group | Used by |
|---|---|---|---|---|
| Archive venue | `data.venues.ArchiveVenue` (+ optional `FundingSource`) | `data.venues.register` | `trading_research_agents.venues` | `tra data fetch`, `tra data audit --venue`, `tra data funding` |
| Live feed | `trading.feeds.LiveFeedFactory` | `trading.feeds.register` | `trading_research_agents.feeds` | `tra paper`, `tra live` |
| Live venue | `trading.venues.LiveVenueFactory` | `trading.venues.register` | `trading_research_agents.live_venues` | `tra live` |

Data that comes from somewhere other than a venue's public archive (a vendor export, a
Client's own recorder) does not need a plugin at all: a column map takes it through the same
gate. See `docs/data.md`, section "Any other source".

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
