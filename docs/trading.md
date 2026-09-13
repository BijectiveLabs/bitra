# The trading agent

Two agents, one ledger. The research agent turns an idea into a sealed pre-registration and a
layer-5 verdict. The trading agent takes a strategy that registration describes and runs it in
real time, through the same gates, and its closed round trips go back to layer 5 as
`trades.jsonl`. Neither can be used without the other: layer 6 demands an exit bound to the
mechanism the memo named, and the runtime demands controls that the research never needed.

```
feed  ->  strategy.on_bar  ->  controls.admit  ->  venue.submit  ->  exits (layer 6)  ->  trades.jsonl
```

No language model is anywhere on the order path. Not to decide, not to assess risk, not to
narrate an incident mid-run. Capital is governed by code that is tested and fails closed.

## What this release runs, and what it does not

| Tier | Feed | Venue | In this release |
|---|---|---|---|
| T0 research | recorded (Parquet from `tra data audit`) | simulated | `tra trade replay` |
| T1 paper | live public trades | simulated | `tra trade shadow` |
| T2 shadow | live public trades | simulated, intents recorded, zero orders | `tra trade shadow` |
| T3 canary, T4 production | live | live venue with keys | **not in this release** |

Real orders are deliberately absent. Code that holds a trade key needs its own security
review (key segmentation, storage, rotation, idempotent submission) before it ships, and the
research this descends from never reached the live tier because its funnel worked. The
`Venue` protocol that a live adapter will satisfy exists now, and the controls run today in
simulation, so the live tier is an adapter, not a redesign.

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

`examples/strategies/` holds two cores so the runtime and the harness can be exercised end
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

## Controls

The per-strategy tier of the circuit-breaker hierarchy, independent of the strategy:

| Control | Behaviour |
|---|---|
| `max_risk_per_trade_bps` | stop distance times notional over equity; unknown equity refuses |
| `max_position` | size cap per order |
| `max_orders_per_minute` | rate limit at the venue layer, so an order storm from a strategy bug is refused there |
| `daily_loss_limit_bps` | once tripped, no new entries that UTC day |
| kill switch | a file or a call; once armed it stays armed, the position is flattened, and the run stops |

Flattening is always admitted, kill switch included. A control that blocks the exit is a trap.
A clock that goes backwards refuses rather than evaluating.

## The ladder

`tra ladder promote --evidence "..." --signed-by "..."` moves one tier up and needs both. `tra
ladder demote --reason "..."` moves down on a reason alone. The asymmetry is deliberate: up
slowly with evidence, down instantly. The two live tiers are refused by this release, and the
ladder says so.

## Running it

```bash
tra data audit BTCUSDT2025-01-01.csv.gz --venue bybit --instrument BTCUSDT \
    --max-gap 5m --parquet data/btc.parquet
tra trade replay --config examples/strategy.toml --envelope examples/envelope.toml \
    --parquet data/btc.parquet --out runs/replay-1
tra chain validate --registration runs/<id>/preregistration.json \
    --trades runs/replay-1/trades.jsonl --cost-bps 15 --block-length 7
pip install 'trading-research-agent[shadow]'
tra trade shadow --config examples/strategy.toml --envelope examples/envelope.toml \
    --out runs/shadow-1        # live public trades; touch KILL to flatten and stop
```

`examples/strategy.toml` carries every parameter of a run; nothing is a constant in the logic.

## The live feed

Live feeds are plugins, found by name through `trading.feeds`. `tra trade shadow` takes the
name from the venue half of `[run].venue_key` (`bybit/taker-taker` selects `bybit`) or from
`--feed NAME`, and refuses a name with no plugin before opening a connection. One feed ships,
behind the `[shadow]` extra; `docs/venues.md` says how to add another.

The bundled feed subscribes to the venue's public trade stream with no key. A subscribe that
is not acknowledged within ten seconds is not a subscription. A link carrying nothing for two
ping intervals is treated as dead and reconnected. A frame it does not recognise is counted
and shown, not dropped. The message shape was taken from the venue's own documentation and
checked against recorded frames.
