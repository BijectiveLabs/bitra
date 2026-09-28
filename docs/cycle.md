# The cycle

One cycle is one pass from what the market is doing to a decision that is either an intent or a
refusal with a reason. It is the unit this project works in, and the unit an auditor replays.

```bash
tra run --env-file .env          # data.toml, strategy.toml, envelope.toml from the project
tra run --instrument ETHUSDT --margin 5 --mode signals
```

```
snapshot (engine)  ->  briefs (code)  ->  agents (parallel)  ->  cost gate (code)  ->  intent or refusal
```

Nothing in that line is optional and nothing in it is reordered. The engine fetches; code
computes; models read figures and answer inside a schema; code prices the round trip and decides
whether the claim clears it. In the default mode the intent is written to the cycle's own
directory and no order leaves the machine.

## What a cycle leaves behind

```
runs/cycles/2026-09-27T14-51-23Z/
  snapshot.json          every value the engine fetched, with source, tier and age
  briefs/technical.json  the figures the analyst was given, computed here, not by the model
  briefs/cost.json       what a round trip would cost, per component
  answers/technical.json what the model answered, as it answered it
  intents.jsonl          the intent, if the gate admitted one
  cycle.json             the whole cycle in one file: data, agents, cost, verdict, refusals
```

A cycle that refuses writes the same files minus the intent. That is the point: a refusal is a
result, and it is as readable as a trade.

## Briefs: the model does no arithmetic

The technical brief carries returns over several horizons, the close as a z-score of its window,
the distance from VWAP, the volume and range ratios, where the close sits in the window's range,
the current spread, the funding rate that stands, and the age of the candles it was built from.
The model receives those numbers and answers with a target, a confidence, a horizon and a
reason. It never adds anything up, so a wrong sum is a bug in code with a test, not a sentence in
a prompt.

## Who is asked, and what each one reads

| Agent | Reads | Answers | Asked when |
|---|---|---|---|
| `technical_analyst` | the closed candles of the snapshot, the spread, the funding rate | a target, a confidence, a horizon, a reason | every cycle; it is the role that decides |
| `macro_analyst` | macro series and the dated text of `news.toml` | a regime, the events it saw, a reason | when `sources/macro.toml` or `sources/news.toml` declares something, or `--macro` / `--news` names a file |
| `onchain_analyst` | chain figures and derivative figures, read side by side | accumulation, distribution or neutral, and why | when `sources/onchain.toml` or `sources/derivatives.toml` declares something, or `--onchain` / `--derivatives` |
| `sentiment_analyst` | dated scores, plus the same dated text as notes | negative, neutral, positive, or not declared | when `sources/sentiment.toml` declares something, or `--sentiment` |

### The shelf

A desk does not have one data source. It has a shelf of them, and the shelf changes when a
subscription starts or a free endpoint goes away. So the declarations are one file per class of
data, never one file per vendor:

```
my-run/
  strategy.toml
  data.toml               live values for the cycle: candles, book, funding, open interest
  sources/
    macro.toml            dated levels and rates
    news.toml             dated text: releases, statements, the desk's own notes
    sentiment.toml        dated scores: a fear index, a positioning score
    onchain.toml          dated chain figures: venue flows, supply held, active addresses
    derivatives.toml      dated funding, open interest, basis
```

A vendor is a line inside one of those files, so replacing one with another is an edit in one place
and nothing else in the project moves. `tra init` writes all five as forms with every entry
commented out, and a form declares nothing: an untouched project asks only the technical analyst
and pays for nothing else. Fill one in and its analyst joins the roster on the next run. Two
classes can feed one role, which is why the on-chain analyst reads chain and derivative figures
together.

`tra check` reads the shelf without reading the data: it names what each file declares, names an
export that is declared but not on disk, and prints the roster the next `tra run` will ask. The
older layout (`series.toml`, `onchain.toml`, `sentiment.toml` beside the config) is still read, so
a project written before the shelf existed keeps working.

Each file goes through the same series engine as `tra agent session`: `publish_lag` decides when a
figure became knowable, and a free CSV export and a paid vendor's dump are read the same way. The
live numbers a cycle decides on are a different thing, and they come from `data.toml` with their
age attached. Keeping them apart is what stops a daily file from standing in for a number that has
to be fresh.

The three reading analysts go out together with the technical analyst, so the cycle waits for the
slowest of them and not for the sum of all four. Each one reads through the same series engine as
`tra agent session`, with the same as-of rule: a value is never used before the moment it was
published, and the operator's `publish_lag` is what decides that moment.

Two habits are enforced rather than encouraged. Nothing is scraped: an analyst reads the files its
operator declared, and there is no code path from an agent to the internet. And a role with no
source says so; the sentiment analyst answers `not declared` rather than `neutral`, because those
are different facts and an operator has to be able to tell them apart. A role whose files are all
unreadable is not asked at all, and the cycle records which files it could not read.

A role can run on its own model. `[agents.roles.<role>]` overrides the provider, the model, the
output cap, the timeout and the retries for that role alone, and anything it does not override it
inherits. That is how a cheap model labels a regime while a stronger one reasons over the bars.

In this build the context is recorded and reported; it does not yet gate anything, so a reading
analyst that fails leaves a line in `cycle.json` and the cycle carries on. The confluence matrix
that makes the context binding is the next unit, and when it lands a missing state will refuse
rather than be treated as neutral: a state nobody produced is not a neutral state.

## The cost gate, and what each number is worth

Before there is an intent, code prices the round trip on the book the snapshot is holding. Every
component says how much we actually know:

| Component | Certainty | Where it comes from |
|---|---|---|
| `fee_round_trip` | declared | the channel's schedule in `envelope.toml` |
| `spread_cost` | measured | half the spread of this snapshot's book, both legs |
| `impact` | computed | walking the visible levels at the declared size; the exit leg is assumed symmetric |
| `funding_accrual` | declared | the settlements a horizon spans, at the rate that stands now |
| `latency_slippage` | estimated | `[run].slippage_bps`, an operator's bound, not a measurement |

The gate is one comparison: the claimed edge must be at least the total plus the margin the
operator declared. The claim itself is labelled too. Today it comes from
`[agents].magnitude_bps`, which is a declaration; when layer 1 or layer 5 has measured the
phenomenon, the same field carries the measurement and the label changes with it.

A real cycle, live data and a real model, printed as the console shows it:

```
cycle 2026-09-27T14-51-23Z BTCUSDT: 8.46s of agents
  funding        ccxt:bybit         age 0.764s
  ohlcv          ccxt:bybit         age 35.705s
  orderbook      ccxt:bybit         age 5.303s
technical: long at 0.68 over 3 bars: short-horizon reversion of the close against its window
           triggered by a zscore of -2.197, vwap_gap_bps of -24.21 and volume_ratio 2.392
cost: 13.48 bps round trip, 2.00 of it estimated
gate: 25.0000 bps claimed clears 13.4800 bps cost plus 0.0000 bps margin
the edge is declared: [agents].magnitude_bps, declared by the operator and not measured here
  fee_round_trip        11.0000 bps  declared
  spread_cost            0.0118 bps  measured
  impact                 0.0000 bps  computed
  funding_accrual        0.4682 bps  declared
  latency_slippage       2.0000 bps  estimated
intent: long 0.01 (sandbox)
```

Read the fee line again: eleven of the thirteen and a half basis points are the venue's own
schedule. That is the number most agent frameworks never show, and it is usually the number that
decides whether the whole idea is worth running.

## Agents run together, and one failure is not the end

Analysts go out on a small pool of threads, because a model call is seconds of waiting on a
socket. Each answer is written the moment it arrives, so a cycle interrupted by a rate limit
keeps what it already paid for; `--resume` then reuses those answers instead of asking again. A
failed agent becomes a line in `cycle.json` rather than a traceback, and the results are always
reported in the same order no matter who finished first.

## Modes

| Mode | What happens |
|---|---|
| `sandbox` (default) | the intent is written into the cycle; nothing leaves the machine |
| `signals` | the intent is written for your own executor to act on |

Sending orders to a venue is a separate mode with its own requirements: a signed mandate, keys in
your environment, a promoted ladder, and an acknowledged disclaimer. It is not in this build.

## When a cycle refuses

| Refusal | What it means |
|---|---|
| `data <kind>: ...` | a chain was exhausted; see `docs/data.md`, and give that kind a longer `ttl` or another source |
| `no technical brief: ...` | there were not enough closed candles in the snapshot to compute features |
| `technical: flat` | the analyst saw nothing worth pricing |
| `technical: confidence ... below` | the answer was under `[agents].min_confidence` |
| `cost: no book in the snapshot` | nothing can be priced without a book, so nothing is |
| `cost gate: ... is below ... required` | the claim did not clear the cost plus the margin |

The first five stop before any order could exist. The last one is the gate doing the job the
research programme this package came from was built to do.
