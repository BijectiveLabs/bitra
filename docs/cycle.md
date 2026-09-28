# The cycle

One cycle is one pass from what the market is doing to a decision that is either an intent or a
refusal with a reason. It is the unit this project works in, and the unit an auditor replays.

```bash
tra init my-desk --profile autonomous     # the cycle laid down wired, every line commented
tra run --env-file .env                   # data.toml, strategy.toml, envelope.toml from the project
tra run --instrument ETHUSDT --margin 5 --mode signals
tra run --every 5m --cycles 12 --env-file .env   # one cycle every five minutes, on the boundary
```

## Running it on a schedule

`--every` runs a cycle on the interval's own boundary, so a cycle reads a bar that has finished
happening rather than one that is half built. One process, no cron, no daemon: the loop is in the
same command, and everything that should end it ends it by name.

| Ends the loop | Why |
|---|---|
| the budget | a cycle that stopped because the token budget is spent will not be followed by one that costs more |
| the kill switch | the file named in `[controls].kill_switch` exists |
| `--cycles N` | the count asked for |
| a keyboard interrupt | the operator stopped it; the cycle that was running is already on disk |

A refusal is not a reason to stop. A cycle that refused because a gate said no is a cycle that did
its job, and the next one decides again with fresh figures. The frequency cap in `[targets]` still
applies across the whole day, so an interval shorter than the desk's own limit simply produces more
refusals, each one named.



The profile is the short way in. It writes the same project as `tra init`, with four differences:
the technical analyst is the core, the confluence matrix, the conviction ladder and the targets are
live tables rather than commented ones, `[execution].mode` is `sandbox`, and the project's README
carries the three things that are yours before a cycle can decide anything - a model and its key,
the mechanism and the edge you claim, and data to decide on. `tra check` names whatever is still
missing, including the provider placeholder, and reads the three files against each other: the
instrument as the sources know it, a venue supplying data that is not the venue whose fees are
priced, and an `[execution].venue` that does not match `[run].venue_key` - that last one is a
refusal rather than a note.

The gates it writes ask for nothing until you give them something: `[confluence].required` is empty,
because a role nobody asked has agreed to nothing, and `[conviction].weights` names only the
deciding analyst, so the conviction score is that analyst's own confidence. Fill in a file under
`sources/` and its analyst joins the roster; give that analyst a weight and its agreement starts
changing the size; name it in `required` and its disagreement starts refusing the trade. Three steps,
in that order, each one visible in `tra check` before it costs a token.

```
snapshot (engine)
  -> briefs (code, each role's own sections only)
  -> technical analyst + reading analysts   (parallel)
  -> the round trip priced (code)
  -> validators                             (parallel, when configured)
  -> confluence matrix   (code: declared states and votes)   -> whether to trade at all
  -> conviction          (code: the same voices, weighted)    -> how much to trade
  -> cost gate           (code: the claim against the cost)
  -> targets             (code: net bps, reward-to-risk, round trips today)
  -> intent, or a refusal that names the figure that refused it
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
  briefs/validator_pro.json   the proposal and the figures a validator was given
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

## Every role inside its own job

A desk works because a job has edges. The analyst that labels a regime does not size the order, the
role that prices the round trip does not decide whether the idea is any good, and none of them
places anything. Four things keep a role inside its job, and only the last is words:

| Enforced by | What it prevents |
|---|---|
| the brief | A role cannot reason about what it was never given. No reading analyst receives equity, size, the controls, the cost of the round trip or the exit distances. `agents/scope.py` filters every brief to that role's declared reads, and what it removed is written in the record. |
| the schema | A closed schema with only that role's own fields. A macro analyst has nowhere to put a size, and a field outside the schema is a parse error rather than a surprise. |
| the wiring | Nothing downstream reads a sentence. The confluence matrix and the conviction score read the declared state code derived from the operator's thresholds; the cost gate reads the snapshot's own book; the size comes from the operator's ladder and caps. |
| the prompt | One line naming what is not this role's job, plus a small vocabulary code looks for in case it appears anyway. |

The last line needs care and gets the least authority. Pattern-matching prose is blunt, and "buy"
and "sell" belong in a proper sentence about what a central bank did, so the vocabulary is only the
phrases no reading analyst has reason to write - "go long", "stop loss", "position size",
"leverage". A hit is recorded as a crossing beside the answer and changes nothing, because the
answer was never what decides. We cannot stop a model from writing a sentence; we can make the
sentence carry no authority and leave a mark when it tries to.

## The confluence matrix

Deterministic, in the configuration, reading the declared state rather than the model's label:

```toml
[confluence]
required = ["macro_analyst", "onchain_analyst"]
long_states = ["bullish"]
short_states = ["bearish"]
on_missing = "refuse"          # refuse | ignore
min_support = 1                # validators that must support it
max_opposition = 0             # validators that may oppose it; 0 is the strictest there is
```

It fails closed. A required role whose declaration is `undeclared` or `mixed` has produced no state,
and a state nobody produced is not a neutral state: the cycle refuses and names the role. An
operator who means to run on one leg writes `on_missing = "ignore"`, and the record says they did.
`tra check` refuses a matrix that can never pass - a required role the roster does not ask, or more
support required than there are validators to give it.

## The validators

Three stances of one role, pro, contra and neutral, asked after the deciding analyst answers and
after the round trip is priced, in parallel with each other:

```toml
[agents]
validators = ["pro", "contra", "neutral"]
```

Each one receives the proposal, the figures it was built from, the declared states and what the
round trip costs. Each must answer with claims that cite a figure by the brief's own name, with that
figure's value:

```json
{"stance": "contra",
 "claims": [{"figure": "volume_ratio", "value": "7.971", "says": "the burst is not finished"}],
 "verdict": "oppose",
 "reason": "..."}
```

Code checks every claim against the brief. A figure that is not there is discarded, a value that
does not match is discarded, both are recorded, and a verdict with no surviving claim becomes
`abstain` - a verdict resting on nothing is nothing. A claim may round the figure it cites: "-1.63"
against a brief's "-1.631" is the same citation said with fewer digits, and it is kept. Then the
matrix counts the votes.

Each stance is one more call, which is why they are off until an operator asks for them.

## Conviction: who weighs the analysts, and how much goes behind the idea

The matrix answers whether the context agrees with the direction. `[conviction]` answers the
question a desk actually has next: how much. It is the same declarations, weighted.

```toml
[conviction]
weights = { technical_analyst = 2.0, onchain_analyst = 1.5, macro_analyst = 1.0, sentiment_analyst = 0.5 }
validator_weight = 1.0
min_score = 0.4                  # below this the desk does not trade, whatever the matrix said
max_leverage = 3.0               # notional against [run].equity; a ceiling, never a target

[[conviction.ladder]]
at_least = 0.75                  # everything agrees: the whole base size
size_multiple = 1.0
[[conviction.ladder]]
at_least = 0.55
size_multiple = 0.5
[[conviction.ladder]]
at_least = 0.4
size_multiple = 0.25
```

The score is arithmetic anyone can check by hand:

| Voice | Contributes |
|---|---|
| a reading analyst whose declared state agrees with the direction | `+weight` |
| a reading analyst whose declared state points the other way | `-weight` |
| a reading analyst that declared nothing | `0`, and it is listed as silent |
| the deciding analyst | `weight * its own confidence` |
| each validator that supports after its claims were checked | `+validator_weight` |
| each validator that opposes | `-validator_weight` |

`score = max(0, the sum) / everything that could have agreed`, so it sits between 0 and 1. The first
rung of the ladder the score reaches gives the size multiple, and then every cap that already
existed still applies: `[controls].max_position`, the notional ceiling implied by `max_leverage`
against `[run].equity`, and the venue's `size_step`, rounding down and never up.

The desk's own example, with the weights above: good macro news and a bullish press, while the
chain figures say whales are sending coins to the venues, and the technical analyst wants a long at
0.7 confidence. That is `+1 (macro) +0.5 (sentiment) -1.5 (on-chain) +1.4 (technical)` over a
possible 8, which is 0.175 - below the floor, so the desk does not trade it. Change the on-chain
declaration to agreement and it becomes 0.85, which is the top rung and the whole base size. The
analysts are worth their token bill because that number moved, not because they all said yes.

Two rules behind the design. Silence is never agreement: a role that declared nothing contributes
nothing, because counting silence as assent is how a desk talks itself into a trade. And the size
is a rule rather than a judgement: a model asked "how big should this be" answers differently on
Tuesday than on Monday from the same figures, and a size that depends on which sentence was written
last cannot be backtested, reproduced or audited. Leverage is not a lever any role pulls either - it
is what the notional works out to against the equity, and `max_leverage` is the ceiling on that.

When the size the ladder chooses is not the base size, the round trip is priced again at the size
that is actually going out, because impact is a function of size. The cost in the record is the cost
of the trade that was admitted.

## The targets

What the desk is trying to do, in numbers, checked before there is an intent:

```toml
[targets]
min_net_bps = 5                  # what must be left of the edge after every cost the gate priced
min_reward_risk = 1.5            # take profit against stop, both from [exit]
max_round_trips_per_day = 6      # intents already written today for this instrument
```

The round trips are counted from the intents the cycles actually wrote today, not from a counter in
memory: a process that restarts must not forget what it has already done. Every refusal names its
figure, so "targets: net 4 bps is below the declared minimum 10 bps" is the whole explanation.

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

## The execution mandate

Three modes, and the distance between them is deliberate.

| Mode | What happens to an admitted intent | What it needs |
|---|---|---|
| `sandbox` | written into the cycle's own directory | nothing; this is the default |
| `signals` | written for execution somewhere else, by someone else's system | nothing |
| `venue` | sent to the venue, with real capital behind it | a mandate, the canary tier, the caps, the keys |

Venue mode does not start until all of this is true, and `tra check` names whatever is not:

1. `MANDATE.md` is in the project and carries the mandate text word for word. Print it with
   `tra mandate > MANDATE.md`, and read it before you do - it says that the losses are yours.
2. `[execution].acknowledged_by` and `acknowledged_on` name who signed it and when.
3. `[execution].max_notional` caps one order. There is no default.
4. The ladder stands at `CANARY`, promoted with evidence and a signature.
5. `[canary]` limits are declared.
6. The venue's keys are in the environment. They are read by the execution layer alone, never from
   a file, and they never appear in a brief, a prompt, a record or a log.

The order follows the channel. `[run].venue_key` names a channel in `envelope.toml`, and the name
is `<entry>-<exit>`, each `maker` or `taker`:

| Channel entry | What is sent | If it does not fill |
|---|---|---|
| `taker` | a market order, crossing the book at once | it does |
| `maker` | a post-only limit at this snapshot's own touch, offset by `price_offset_bps` | cancelled after `limit_timeout_s`; nothing is chased across the spread |
| anything else | nothing | venue mode refuses: the fee you priced depends on the answer |

That is not a style preference. A channel named `maker-taker` was priced at the maker fee, and
sending a market order under it would pay a fee the gate never priced.

Three habits make this auditable after a failure, which is the only time it matters. Every order
carries a client id derived from the cycle and the instrument, so the same cycle run twice cannot
open two positions. The account is read before anything is sent, and an order is never added to a
position pointing the other way unless `adopt_open_position` says so - a hand that cannot read the
account does not send at all. And nothing is retried: a venue that refuses is recorded as a refusal,
because a retry loop around real capital is how one defect becomes ten orders.

The size is the one the conviction rule chose, capped again by `[execution].max_notional`. What came
back is written to `placed.json` beside the intent, and the intent is written first: whatever the
venue does next, the record of what this cycle decided survives it.

### What happens after the order

The stop and the take profit go to the venue **attached to the entry**, in the same call. Code
computes both from `[exit].stop_sigma_frac` and `[exit].take_profit_sigma_frac` against the price
the cost gate priced on, so no model is ever asked where a stop belongs. A filled position with no
exit is the one state this software must never create, because it is the state that survives our
process dying - and if the venue cannot take them with the entry, the order is not sent at all. An
operator who carries the exits in their own system says so with `[execution].exits = "none"`, and
`tra check` lists that as something to fix rather than a setting to admire.

The same read that protects against a wrong-way position protects against a second one: any open
position on the instrument refuses a new entry unless `adopt_open_position` says otherwise, because
adding to a position is a larger position than the conviction rule sized. An order still working on
the instrument refuses too - a second one would be a second decision on the same position - and the
client order id is derived from the cycle and the instrument, so the same cycle run twice cannot
open two.

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
