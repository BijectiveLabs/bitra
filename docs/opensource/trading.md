---
description: "The trading agent runtime: replay, paper, shadow and canary tiers, the controls in front of every order, and how closed trades feed back into research."
---

# The trading agent

Two agents, one ledger. The research agent turns an idea into a sealed pre-registration and a
layer-5 verdict. The trading agent takes a strategy that registration describes and runs it in
real time, through the same gates, and its closed round trips go back to layer 5 as
`trades.jsonl`. Neither can be used without the other: layer 6 demands an exit bound to the
mechanism the memo named, and the runtime demands controls that the research never needed.

```
feed  ->  strategy.on_bar  ->  controls.admit  ->  venue.submit  ->  exits (layer 6)  ->  trades.jsonl
```

In this runtime, which runs a scripted strategy, no language model is anywhere on the order
path: not to decide, not to assess risk, not to narrate an incident mid-run. The agent desk
(`tra run`) is described in [The cycle](/opensource/cycle). Capital is governed by the system that is tested and fails closed.

## What this release runs, and what it does not

| Tier | Feed | Venue | Command |
|---|---|---|---|
| T0 research | recorded (Parquet from `tra data audit`) | simulated | `tra backtest` (`trade replay`) |
| T1 paper | live public trades | simulated | `tra paper` (`trade shadow`) |
| T2 shadow | live public trades | simulated, intents recorded, zero orders | `tra paper` (`trade shadow`) |
| T3 canary | live public trades | a live venue plugin, hard caps | `tra live` (`trade live`), see [The live tier](/opensource/live) |
| T4 production | | | **not in this release**; the ladder refuses it |

The common spellings are aliases of the tiers. Real orders exist only at canary, through a
plugin behind a security review that runs as tests ([The live tier](/opensource/live)); the research this
descends from never reached the live tier because its funnel worked, and the canary exists
to find out whether the venue behaves the way the simulation assumed, not to make money.

## The decision core

```python
class Strategy(Protocol):
    name: str

    def on_bar(self, bar: Bar) -> Signal | None: ...
```

Pure: internal state is built only from bars already seen; no clock, no socket, no order. A
`Bar` with no trades is empty and is never filled from the previous close. `Signal` says what
the core wants (`long`, `short`, `flat`) and why, in words the ledger keeps.

The package ships no strategy. A run names its core in `strategy.toml`, by file or by
import, and the parameters it is built with:

```toml
[strategy]
file = "cores/my_core.py"        # looked up next to strategy.toml first, then from the cwd
class = "MyCore"
# import = "my_package.cores:MyCore"   # or a core installed as a package

[strategy.params]
lookback = 60
```

[`examples/strategies/`](https://github.com/BijectiveLabs/trading-research-agents/blob/develop/examples/strategies/) holds two cores so the runtime and the harness can be exercised end
to end: `trailing_zscore.py` for the runtime and `pair_reversion.py` for the walk-forward
harness. They come from the research record this project descends from, where both were
measured and closed. They are examples of the mechanism, not recommendations, and nothing
in the package imports them. A test fails the build if a decision core appears inside the
package.

The walk-forward harness is the same shape: `walk_forward(panel, core, lookback=...)` takes
a `FittedCore` that says how to fit a trailing window and what to do with the fit, refits
at every bar, freezes the parameters at entry, and refuses a precomputed one. The harness
is the mechanism; the core is yours.

## Exits bound to the mechanism (layer 6)

Every position carries an `ExitRule` declared before it opens: a stop as a fraction of price,
an optional take-profit, a timeout in bars, and one sentence saying why this exit is where the
mechanism is done. The rule refuses to exist without the sentence.

Stops are simulated on every closed bar with deliberately adverse assumptions: a touch is a
fire, a gap through the stop fills at the worse open, a bar that touches both take-profit and
stop is a stop, a take-profit fills exactly at its limit. Only OHLC is known inside a bar, and
a favourable guess manufactures an edge that does not exist.

## The runtime

- Entries fill on the first print at or after the measured latency, never on the print that
  fired the signal. Slippage is charged on the adverse side.
- Timeouts and signal exits go back through the venue like any other intent.
- The cost of every round trip comes from the cost identity with the operator's envelope.
- Every intent (admitted or refused, with the reason), every fill, and every closed round trip
  is appended to its own file as it happens: `intents.jsonl`, `fills.jsonl`, `trades.jsonl`,
  and `run.json` at the end.
- One code path. The runtime sees the feed and the venue as protocols; replay and shadow
  differ only in what they are given.

## What a round trip went through, not only what it earned

Every closed round trip in `trades.jsonl` carries two more figures beside its net: the furthest
the position ever stood in front, and the furthest it ever stood under water, both in basis
points against the entry and both with the bar they happened on.

| Field | Meaning |
|---|---|
| `mfe_bps` | maximum favourable excursion: the best the position ever stood, positive |
| `mae_bps` | maximum adverse excursion: the worst it ever stood, negative |
| `bars_to_mfe`, `bars_to_mae` | which bar each happened on, counting from the entry |
| `bars` | how many bars the position was held through |

The runtime already walks every bar while a position is open, so this costs one comparison per
bar. The figures are signed the way a trader reads them, whichever way the position was facing: a
short is helped by the bar's low and hurt by its high.

Why they are worth keeping. A trade that closed at plus two after standing twenty in front is an
exit problem, and no amount of work on entries will fix it. A trade that closed at plus two after
standing fourteen under water is a stop that nearly fired, and it will fire on the next one like
it. Neither fact is visible in the net, and both change what an operator should do next.

The risk manager's brief carries the summary over a run: the median and the best favourable
excursion, the median and the worst adverse one, and the median of what the winners gave back
before they closed. `execution/excursion.py` computes it; nothing about it enters the cost
identity, and nothing about it changes what a trade earned.

## Controls

The per-strategy tier of the circuit-breaker hierarchy, independent of the strategy:

| Control | Default | Behaviour |
|---|---|---|
| `max_risk_per_trade_bps` | `50` | stop distance times notional over equity; unknown equity refuses |
| `max_position` | none | size cap per order, in units; `max_position_quote` says it in quote currency |
| `max_orders_per_minute` | `6` | rate limit at the venue layer, so an order storm from a strategy bug is refused there |
| `daily_loss_limit_bps` | `300` | once tripped, no new entries that UTC day |
| `max_drawdown_bps` | none | from the highest equity across days; no new entries until the peak file is cleared |
| `on_drawdown` | `stop` | `flatten` also arms the kill switch: positions flattened, run stopped |
| kill switch | `KILL` in the template | a file or a call; once armed it stays armed, the position is flattened, and the run stops |

Flattening is always admitted, kill switch included. A control that blocks the exit is a trap.
A clock that goes backwards refuses rather than evaluating.

## The ladder

`tra ladder promote --evidence "..." --signed-by "..."` moves one tier up and needs both. `tra
ladder demote --reason "..."` moves down on a reason alone. The asymmetry is deliberate: up
slowly with evidence, down instantly. Canary is reachable when a live venue plugin is
registered; production is refused by this release, and the ladder says so. `tra live` runs
only when `ladder.json` stands at canary.

## Running it

```bash
tra init my-run && cd my-run       # strategy.toml, envelope.toml, an example core, .env.example
tra data get BTCUSDT 2025-01-01    # fetch, audit, Parquet, funding: data/BTCUSDT-2025-01-01.parquet
tra check                          # what is ready, what is missing, in plain words
tra backtest                       # newest dataset in data/, funding beside it, out under runs/
tra chain validate --registration runs/<id>/preregistration.json \
    --trades runs/backtest-<time>/trades.jsonl --cost-bps 15 --block-length 7
pip install 'trading-research-agents[shadow]'
tra paper                          # live public trades; touch KILL to flatten and stop
```

Every flag the commands used to need still exists (`--config`, `--envelope`, `--parquet`,
`--out`, `--dataset`); inside a project they default to the convention above. A run that
spans a funding settlement without a rate on record is refused, because a missing rate is
not zero; `tra data get` fetches the rates with the day, and `[run].funding` or
`data/<INSTRUMENT>-funding.json` is where they are read from.

`strategy.toml` carries every parameter of a run; nothing is a constant in the logic. A
relative `[strategy].file` is looked up next to the config first, so a run directory carries
its own core.

## The live feed

Live feeds are plugins, found by name through `trading.feeds`. `tra trade shadow` takes the
name from the venue half of `[run].venue_key` (`bybit/taker-taker` selects `bybit`) or from
`--feed NAME`, and refuses a name with no plugin before opening a connection. One feed ships,
behind the `[shadow]` extra; [Venues](/opensource/venues) says how to add another.

The bundled feed subscribes to the venue's public trade stream with no key. A subscribe that
is not acknowledged within ten seconds is not a subscription. A link carrying nothing for two
ping intervals is treated as dead and reconnected. A frame it does not recognise is counted
and shown, not dropped. The message shape was taken from the venue's own documentation and
checked against recorded frames.
