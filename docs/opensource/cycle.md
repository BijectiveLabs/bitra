---
description: One cycle of trading-research-agents, from market data to an order intent or a refusal with a reason, and how to run it on a schedule with tra run.
---

# The cycle

One cycle is one pass from what the market is doing to a decision that is either an intent or a
refusal with a reason. It is the unit this project works in, and the unit an auditor replays.

```bash
tra init my-desk --profile autonomous     # the cycle laid down wired, every line commented
tra run                                   # data.toml, strategy.toml, envelope.toml and .env from the project
tra run --instrument ETHUSDT --margin 5 --mode signals
tra run --every 15m --cycles 12           # one cycle every fifteen minutes, on the boundary
```

## Running it on a schedule

`--every` runs a cycle on the interval's own boundary, so a cycle reads a bar that has finished
happening rather than one that is half built. One process, no cron, no daemon: the loop is in the
same command, and everything that should end it ends it by name.

The first pass runs as soon as the desk starts. After that, passes land on the clock: with
`--every 30m` they fall at :00 and :30 UTC, with `--every 15m` at :00, :15, :30 and :45. So the wait
after a pass is whatever is left to the next boundary, not a full interval, and a pass that takes
several minutes shortens it further. The desk says it plainly after every pass, for example
`next pass at 08:30:00 UTC, in 20m 00s`.

| Ends the loop | Why |
|---|---|
| the budget | a cycle that stopped because the token budget is spent will not be followed by one that costs more |
| the kill switch | the file named in `[controls].kill_switch` exists |
| `--cycles N` | N scheduled passes, whether or not the trigger let a cycle through |
| a keyboard interrupt | the operator stopped it; the cycle that was running is already on disk |
| SIGTERM, SIGHUP, SIGBREAK | a service manager stopping the desk, or an SSH session dropping; handled exactly like Ctrl-C |

A refusal is not a reason to stop. A cycle that refused because a gate said no is a cycle that did
its job, and the next one decides again with fresh figures. The frequency cap in `[targets]` still
applies across the whole day, so an interval shorter than the desk's own limit simply produces more
refusals, each one named.

Every start and stop of `tra run` is one line in `runs/cycles/scheduler.jsonl`: when it started,
what it watched, in which mode, and what stopped it after how many cycles. Each cycle already keeps
its own directory; this file is what says when the desk was running at all, which matters on a
server whose terminal output nobody kept.

```json
{"at": "2026-10-04T06:56:38+00:00", "event": "started", "every_s": 300.0, "mode": "venue", ...}
{"at": "2026-10-04T09:12:04+00:00", "event": "stopped", "stopped_by": "SIGTERM from outside the process", "cycles": 27}
```

**Stopping while an order rests on the book.** A maker entry waits up to
`[execution].limit_timeout_s` for a fill. If the process is told to stop during that wait, the
order is cancelled first, `placed.json` and `cycle.json` say so (`"interrupted": true`, and whether
the cancel went through), and only then does the run end. Stopping between cycles exits with the last
cycle's outcome; stopping inside one always exits as a refusal, so a supervisor can tell the two apart.

One case nothing in a process can cover: closing a Windows console window. Windows gives the
process a few seconds and ends it whatever it is doing. What protects capital then is what was
already at the venue - the stop and take profit travel with the entry, and the next cycle refuses
while any order still works on the instrument. A desk that has to survive its terminal belongs under
a supervisor, not in a window.


`tra init my-desk --profile autonomous` is the short way in. It writes the same project as
`tra init`, with these differences: the technical analyst is the core, deciding on 15-minute bars
with the hour and four hours as context and naming its own stop and target; the confluence matrix,
the conviction ladder and the targets are live tables rather than commented ones;
`[execution].mode` is `sandbox`; and the project's README opens with what is still yours before a
cycle can decide anything - a model and its key, and the mechanism the analyst reasons from. Add
`--agent PROVIDER --model MODEL` and the model is named for you. `tra check` names whatever is still
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
the universe (system: one data pull each, the trigger per instrument, ordered by movement over cost)
  -> the instrument this pass takes
  -> snapshot (engine)
  -> trigger             (system: has anything moved? nothing below here runs if not)
  -> briefs (system, each role's own sections only)
  -> technical analyst + reading analysts   (parallel)
  -> the round trip priced (system)
  -> the stop and the target (the analyst's levels, held in your [exit] bounds; break-even checked)
  -> validators                             (parallel, when configured)
  -> confluence matrix   (system: declared states and votes)   -> whether to trade at all
  -> conviction          (system: the same voices, weighted)    -> how much the desk wants on
  -> risk per trade      (system: the loss at the stop, against equity) -> how much it can afford
  -> the desk            (system: what the whole book is already carrying) -> whether it fits
  -> cost gate           (system: the claim against the cost)
  -> targets             (system: net bps, the edge over the cost, reward-to-risk, research, count)
  -> intent, or a refusal that names the figure that refused it
```

Nothing in that line is optional and nothing in it is reordered. The engine fetches; the system
computes; models read figures and answer inside a schema; the system prices the round trip and decides
whether the claim clears it. In the default mode the intent is written to the cycle's own
directory and no order leaves the machine.

## Which instrument, before any of this

A desk that watches one instrument runs it. A desk that declares a universe takes the cheap look
first, and only then spends a cycle. The work is split on purpose: **the system finds and measures, the
screener chooses.**

```toml
[universe]
instruments = ["BTCUSDT"]        # always looked at, whatever discovery finds
discover = true                  # add the venue's most traded perpetuals each pass
top = 20                         # how many discovery adds
min_quote_volume = 10000000      # 24-hour volume in quote currency
max_spread_bps = 5               # the book now, from the venue's ticker
min_listed_days = 30             # nothing listed this month
crypto_only = true               # leave out stocks, ETFs, commodities and currencies
exclude = []                     # instruments never to take
quote = "USDT"                   # the settlement currency to look in
# type_field = "symbolType"      # the venue's own market field that says what a market is,
# exclude_types = ["stock", "etf", "commodity", "forex"]   # and the values to leave out
max_per_cycle = 1                # how many one pass runs a full cycle on
max_intents_per_day = 6          # across every instrument, not per one
screener = true                  # the screener chooses and orders; without it, the system's order
```

**Discovery (system, every pass).** One market list and one ticker call cover every linear perpetual
the venue lists in `quote` (USDT by default). The system removes what cannot be traded: too little
volume, a book wider than `max_spread_bps`, listed too recently, not crypto, or a maximum leverage
below `[execution].leverage`. Bybit marks its stock, ETF, commodity and currency perpetuals, and
that is the default there; for any other venue, `type_field` and `exclude_types` name the venue's
own field and the values to leave out, and `exclude` takes out single instruments. Every one of
these is yours to set: the defaults are a starting point, not the screen.
The most traded `top` join the declared instruments. Each discovered instrument carries the venue's
own symbol and size step, so an order is never sent under the wrong market or off the step.
Discovery needs `[run].size_quote` and `[controls].max_position_quote`: one base size, or one cap
counted in units, across coins priced from cents to tens of thousands is no size and no cap at all.
With a quote cap declared, every instrument is capped at that much quote currency at its own mid,
and `[controls].max_position` (units) still applies to `[run].instrument`, the one it was written
for. `tra universe` shows what discovery finds right now and what each
filter took out.

**Measurement (system).** Each instrument gets one data pull, the book and the recent bars, no tokens.
The same `[trigger]` and desk limits apply to each. What survives is measured: the move since the
last look and its pressure against the spread, the window's move, z-score, trend strength and
regime, volume against the window, where the close sits in its range, 24-hour volume and change,
and the correlation with BTC.

**Choice (the screener).** With `screener = true` the market screener reads those rows and picks the
instruments where the mechanism has the best chance now, best first, naming the setup it expects
and citing the figures behind each pick. The system checks every pick: it must be an instrument the system
admitted, and at least one citation from that instrument's own row must hold, or the pick is
thrown away. The screener cannot add an instrument, and it says nothing about direction or size;
the technical analyst decides those later from a fuller brief. A screener that cannot answer leaves
the system's order, the move against the spread, in place.

Both orders are kept in `runs/screens/<time>.json` (`code_order` and each candidate's `pick_rank`),
so once trades have closed the audit can say whether the screener's choices did better than the system's.

Each instrument keeps its own history: its own trigger streak, its own carried research spend, its
own daily round trips. A position on an instrument discovery stops listing is still counted by the
desk and looked after by the keeper until it closes. `[universe].max_intents_per_day` is the one
figure that counts the whole desk.

## Before anyone is asked: the trigger

A five minute schedule is two hundred and eighty-eight passes a day, and a fifteen minute one ninety-six. Most of them should refuse, and
refusing is the right answer - but the tokens were spent finding that out, and a desk that spends
more on research than it can win on a trade is not a desk. The answer is not to abandon the
schedule. It is to put a check in front of it that costs nothing.

```toml
[trigger]
min_move_bps = 15                # the mid has to have moved this far since the last cycle
max_spread_bps = 8               # a book wider than this cannot be traded through
cooldown_s = 900                 # one decision per instrument in this window
widen_after = 6                  # after this many straight refusals, ask for more movement
look_valid_for = "1h"            # a last look older than this counts as a first look; 1h by default
```

Every figure it reads is free: the touch of the snapshot the cycle has already taken, the cycle
summaries on disk, the clock, and the count of intents already written today. A cycle that holds
costs one data pull and no tokens, and the hold is written down with its arithmetic - what the mid
moved, what the desk asked for, and why.

"Since the last look" is measured against the latest mid the desk recorded for that
instrument, whether in a cycle or in a pass over the universe. A pass looks at every instrument
but runs a cycle on a few, so the passes are the record that matters. A last look older than
`look_valid_for` is dropped, and the instrument is a first look: it has no move to report, and the
trigger admits it instead of measuring hours of drift against it.

`widen_after` is the part that answers a whole quiet day. After a run of refusals with nothing
changing, the movement required grows with the streak, to a hard ceiling of eight times the
declared minimum so a desk can never be talked into going blind. One real move brings it straight
back to the baseline. A cycle the trigger itself held does not count towards the streak: that is
the desk deciding not to look, not the desk looking and finding nothing.

`tra run --now` asks the roster whatever the trigger would have said. The override is written into
the cycle, so a reader can tell a forced cycle from a natural one.

## What a cycle leaves behind

```
runs/cycles/2026-09-27T14-51-23Z-BTCUSDT/
  snapshot.json          every value the engine fetched, with source, tier and age
  inputs.json.gz         the raw inputs: candles on every timeframe, the book levels, funding,
                         open interest, and the settings the briefs were computed with
  briefs/technical.json  the figures the analyst was given, computed here, not by the model
  briefs/cost.json       what a round trip would cost, per component
  briefs/validator_pro.json   the proposal and the figures a validator was given
  answers/technical.json what the model answered, as it answered it
  intents.jsonl          the intent, if the gate admitted one
  placed.json            what the venue did with it, in venue mode
  cycle.json             the whole cycle in one file, with a sha256 of every file above
runs/cycles/index/2026-09-27.jsonl   one line per cycle that day, with the hash of its cycle.json
```

A cycle that refuses writes the same files minus the intent. That is the point: a refusal is a
result, and it is as readable as a trade.

### Keeping the record honest

Everything is files: JSON, JSON lines, gzip and Parquet. There is no database and no cache server
to run, and every record can be read with a text editor.

| Concern | What the desk does |
|---|---|
| a crash in the middle of a write | appends end with a newline guard, so a torn line costs only itself; order, position and equity records wait for the disk; whole files are written beside and renamed into place |
| a brief nobody can check | `inputs.json.gz` keeps what the brief was computed from; `tra runs verify` computes the technical figures again and compares them |
| a record changed after the fact | `cycle.json` holds a sha256 of every file in the cycle, and the day's index line holds the hash of `cycle.json`; `tra runs verify` checks both |
| a desk that has run for months | the trigger, the targets, the desk limits and the position keeper read the day index, not every directory, so a cycle costs the same on day 300 as on day 1 |
| disk use | `tra runs prune --keep 90d` says what it would remove; `--yes` removes it, `--archive` zips each month first; the index and the logs are kept, so the desk's memory of its own past does not change |

A desk that ran before the index existed builds it from its cycle directories on its first cycle.
A cycle that stopped before writing its index line, a crash or an interrupt at the venue, is still
read from its directory, so an order it sent is never missing from what the desk knows.

## Briefs: the model does no arithmetic

The technical brief carries returns over several horizons, the close as a z-score of its window,
the distance from VWAP, the volume and range ratios, where the close sits in the window's range,
the current spread, the funding rate that stands, and the age of the candles it was built from.
The model receives those numbers and answers with a target, a confidence, a horizon and a
reason. It never adds anything up, so a wrong sum is a bug in the system with a test, not a sentence in
a prompt.

### The market around the decision

A desk decides on one timeframe and reads the bigger picture before it does. The brief carries
that picture too, all of it computed by the system from the snapshot:

| Figure | What it says | Where it comes from |
|---|---|---|
| `regime` | trending up, trending down, ranging or mixed, on the decision timeframe | `trend_strength` against the lines in `[regime]` |
| `trend_strength` | net move over the path walked, times the square root of the steps; a random walk sits near 1 on any window | the window's closes |
| `variance_ratio` | the variance of four-bar returns against four times that of one-bar returns: below 1 the window's moves were given back, above 1 they were followed by more of the same, near 1 is a random walk | the window's closes |
| `window_move_bps` | the price move from the first close of the window to the last; a market move, not the trade's net | the window's closes |
| `timeframes.<tf>` | the same regime, net move, z-score and position in range on each higher timeframe | `[run].context_intervals`, one candle pull each |
| `book_imbalance` | resting bids against resting asks over the first 10 levels, from -1 to 1; positive leans to the bid | the order book already in the snapshot |
| `open_interest` | the change in percent since the previous snapshot of this instrument, and how long ago that was | the open interest already pulled, and the last snapshot on disk |
| `readings.<role>` | what your own thresholds say on the macro, on-chain or sentiment series this cycle | the same declared state conviction reads, known before any model is asked |

Nothing here is required. A higher timeframe the venue cannot serve is named in the refusals and
left out, and so is an open interest with nothing earlier to compare it to.

```toml
[run]
bar_interval_s = 900               # what `tra init --agent` writes: 15m decisions
context_intervals = ["1h", "4h"]   # up to three, each a whole multiple of bar_interval_s

[regime]
trend_above = 2.0                  # trend_strength at or above this is a trend
range_below = 1.0                  # at or below this is a range; between the two is mixed
setups_must_fit = true             # false: a misfit is written down and the trade goes on
fade_below_variance_ratio = 1.0    # a range fade needs the window's variance ratio below this; 0 is off
```

**A setup has to fit what the system measured.** Each setup is a claim with a direction built into it,
and the figures that make the claim are in the brief. On the decision timeframe, the system refuses:

| Setup | Refused when |
|---|---|
| `trend_continuation`, `pullback_in_trend` | the market is not measured as trending (ranging or mixed), or the trade points against the trend |
| `range_reversion` | the market is not measured as a range (trending, or mixed with a measured trend strength), the window's moves have been carrying on (variance ratio at or above `fade_below_variance_ratio`), or the trade does not fade the stretch: a long with the close at or above the window's mean (z-score 0 or more), a short at or below it |
| `momentum_exhaustion` | the trade goes with the move it says is exhausted: with the trend, or with the window's move |
| `range_breakout` | the trade is on the other side from where the close sits: a long with the close in the lower half of the range, a short in the upper half |
| `volatility_expansion` | never on direction: it is about a regime changing, not which way |

The rule came from a live cycle on 2026-10-08: a `pullback_in_trend` short in a market measured as
mixed and rising 53 bps over the window, which the earlier rule let through because it only
checked trends it could measure. A short setup against a higher timeframe's trend is still left to
the analyst to weigh, because fading the hourly on the minute chart is sometimes the whole trade;
the reason has to say so. With `setups_must_fit = false` a misfit is written down and the trade
goes on. `tra check` prints the lines and the timeframes in use.

Why the variance ratio, and not trend strength alone: a window can net out to nothing, and read
as a range, while each move inside it is followed by more of the same. On the demo desks, the
analyst proposed 94 range fades between 2026-10-06 and 10-16 (the stretch for which the desks kept
their raw inputs), each replayed with the same exits for four hours: the 28 with a variance ratio
of 1 or above averaged -101 bps net and won 21% of the time, the rest -46 bps. A fade is paid only
where moves are given back, so that is what it is now held to. The line sits at 1, the random
walk, rather than at a value fitted to those trades.

The fade has been held to the same standard as the trend setups since 0.6.4. Before that,
`range_reversion` was refused only in a trend, so in a mixed market it was the one regime-bound
setup still allowed. Across the demo desks' record to 2026-10-13, fades in a mixed market were
right 27% of the time (n = 30), against 60% in a measured range (n = 37).

**The analyst picks the setup; the mechanism does not.** In `tra run` the technical analyst reads
the regime and the higher timeframes first and names the setup the market supports. A mixed market
takes `range_breakout`, `momentum_exhaustion`, `volatility_expansion` or flat.
`[agents].mechanism` is your thesis: the setups you favour when the market offers them. It never
turns a trending market into a fade, and the screener picks where *a* setup has the best chance,
not one mechanism. `tra init --agent` writes a mechanism that names no setup ("trade the setup the
measured regime supports: with a trend, inside a range, or flat"). Write a narrower one only if you
trade a single idea. The research core in `tra backtest` and `tra agent session` is different: it
trades one registered mechanism, by design.

The macro analyst's regime is a different thing: risk-on or risk-off is about the world outside the
chart, and it can disagree with what price is doing. Both are kept. The analyst sees the declared
macro state in `readings`, and conviction weighs the macro analyst's graded score as before.

## Who is asked, and what each one reads

| Agent | Reads | Answers | Asked when |
|---|---|---|---|
| `technical_analyst` | the closed candles of the snapshot, the spread, the funding rate, the regime on each timeframe, the book imbalance, the open interest change, and your thresholds' reading of the other series | a target, a named setup, a confidence, a horizon, a reason, and the figures it read | every cycle; it is the role that decides |
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

A file can keep itself current: a `fetch` line refreshes it from FRED, the Crypto Fear & Greed
Index or any vendor that answers JSON over https, and `tra run` does that before each pass once the
series' `refresh` interval has passed. Every series also has a `max_age`; a value older than that is
shown as `stale` and never counted. [Sources](/opensource/sources) covers both.

The three reading analysts go out together with the technical analyst, so the cycle waits for the
slowest of them and not for the sum of all four. Each one reads through the same series engine as
`tra agent session`, with the same as-of rule: a value is never used before the moment it was
published, and the operator's `publish_lag` is what decides that moment.

Two habits are enforced rather than encouraged. Nothing is scraped: an analyst reads the files its
operator declared, and there is no code path from an agent to the internet; the engine refreshes a
fetched file, never the model. And a role with no
source says so; the sentiment analyst answers `not declared` rather than `neutral`, because those
are different facts and an operator has to be able to tell them apart. A role whose files are all
unreadable is not asked at all, and the cycle records which files it could not read.

A role can run on its own model. `[agents.roles.<role>]` overrides the provider, the model, the
output cap, the timeout and the retries for that role alone, and anything it does not override it
inherits. That is how a cheap model labels a regime while a stronger one reasons over the bars.

A reading analyst whose model fails leaves a line in `cycle.json` and the cycle carries on. Its
declared state is arithmetic over your thresholds, so it still counts in the confluence matrix and in
conviction; only the model's label is lost.

## Where a direction came from

Two fields make the deciding analyst's answer reviewable a month later, and both are checked by
the system before anything downstream sees them.

**The setup.** One name from a closed list - `trend_continuation`, `pullback_in_trend`,
`range_reversion`, `range_breakout`, `momentum_exhaustion`, `volatility_expansion`, `no_setup`. A
direction with `no_setup` is refused: a desk cannot review, count or compare a trade whose shape
nobody named, and a paragraph is not a shape. The name travels into the intent, so "which setups
have paid for themselves this month" is a question the records can answer.

**The figures.** Every role that says something about numbers answers with `figures_used` - each
figure by the brief's own name, with the value it read - and the system checks each one against the brief
it was handed. A figure that is not there, or a value that does not match, is discarded and the
discard is recorded. For the reading analysts that is a mark on the record; the label was never what
decides. For the analyst that does decide it has teeth: if not one citation survives, the cycle
refuses, because a direction that cannot point at a figure in its own brief came from somewhere
this cycle cannot see.

The check is fair about names. A brief carries a row per series, so one number's full name is
`series.us10y.correlation_measured.value`, and no model writes that when it means "the correlation".
A citation resolves when its words are the tail of exactly one figure's name; where several share a
tail, the shallowest wins; and a block measured with its own sample size resolves to the number in
it. Nothing about that loosens the rule - whichever figure a citation lands on, the value still has
to match, so a citation that resolved to the wrong figure is discarded anyway.

## Every role inside its own job

A desk works because a job has edges. The analyst that labels a regime does not size the order, the
role that prices the round trip does not decide whether the idea is any good, and none of them
places anything. Four things keep a role inside its job, and only the last is words:

| Enforced by | What it prevents |
|---|---|
| the brief | A role cannot reason about what it was never given. No reading analyst receives equity, size, the controls, the cost of the round trip or the exit distances. `agents/scope.py` filters every brief to that role's declared reads, and what it removed is written in the record. |
| the schema | A closed schema with only that role's own fields. A macro analyst has nowhere to put a size, and a field outside the schema is a parse error rather than a surprise. |
| the wiring | Nothing downstream reads a sentence. The confluence matrix and the conviction score read the declared state the system derived from the operator's thresholds; the cost gate reads the snapshot's own book; the size comes from the operator's ladder and caps. |
| the prompt | One line naming what is not this role's job, plus a small vocabulary the system looks for in case it appears anyway. |

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
figure's value - and the figures it may cite are every figure in the brief it was handed, the
proposal's claimed edge and the cost included, rather than a narrower set it never saw:

```json
{"stance": "contra",
 "claims": [{"figure": "volume_ratio", "value": "7.971", "says": "the burst is not finished"}],
 "verdict": "oppose",
 "reason": "..."}
```

The system checks every claim against the brief. A figure that is not there is discarded, a value that
does not match is discarded, both are recorded, and a verdict with no surviving claim becomes
`abstain` - a verdict resting on nothing is nothing. A claim may round the figure it cites: "-1.63"
against a brief's "-1.631" is the same citation said with fewer digits, and it is kept. Then the
matrix counts the votes.

Each stance is one more call, which is why they are off until an operator asks for them.

No stance decides, neutral included. Pro argues for the proposal, contra against it, neutral weighs
both and says which side the figures favour; each answers support, oppose or abstain. Their votes
count in exactly two places, both yours to declare:

| Where | Setting | Effect |
|---|---|---|
| the confluence matrix | `[confluence].min_support`, `max_opposition` | a hard gate: too few supports or too many opposes refuses the trade |
| conviction | `[conviction].validator_weight` | each vote that says something adds or takes away that weight; the size follows the score |

With neither declared, the votes are recorded and move nothing, and `tra check` says so: three
calls a cycle that change no decision are a cost with no purpose.

Conviction weighs what a vote says beyond its stance. Pro argues for and contra against by
assignment, so a pro that supports and a contra that opposes tell conviction nothing and weigh
nothing (on the demo desk pro supported 36 times in 37 and contra opposed 34 times in 35; counted as
votes, they only diluted every score). A pro that abstains or opposes counts as opposition, a contra
that abstains or supports counts as support, and neutral counts as it votes. The confluence matrix
still counts the verdicts as given, because its numbers are the ones you declared.

## Conviction: who weighs the analysts, and how much goes behind the idea

The matrix answers whether the context agrees with the direction. `[conviction]` answers the
question a desk actually has next: how much. Analysts are allowed to disagree - crypto doesn't move
in straight lines, and a macro reading against a technical setup is ordinary. What matters is how
strong each side is, so every voice counts as strong as it actually is.

```toml
[conviction]
weights = { technical_analyst = 2.0, onchain_analyst = 1.5, macro_analyst = 1.0, sentiment_analyst = 0.5 }
validator_weight = 1.0
min_score = 0.4                  # below this the desk does not trade
max_leverage = 3.0               # notional against [run].equity; a ceiling, never a target

[[conviction.ladder]]
at_least = 0.75                  # the case is strong and the context backs it: the whole base size
size_multiple = 1.0
[[conviction.ladder]]
at_least = 0.55
size_multiple = 0.5
[[conviction.ladder]]
at_least = 0.4
size_multiple = 0.25
```

### Each analyst's score

Each reading analyst's score is computed by the system from its own figures against the lines you drew in
`sources/*.toml`, signed for the instrument and between -1 and 1:

```toml
threshold = { bullish_below = 4.05, bearish_above = 4.25 }   # US10Y: low yields are good for crypto
```

The two lines make a neutral band 0.20 wide. A yield of 4.02 is 0.03 past the bullish line, which
is 0.15 of a band: a score of +0.15, barely bullish. A yield of 3.80 is more than a full band past
it: +1.0. Anything inside the band is 0. With several series, the analyst's score is their average.
A threshold with only one line declared has no band to measure against, so it counts as a full +1
or -1 whenever it is crossed, and `tra check` says so.

The model still reads the same figures and writes its own label and reason. Both go in the record,
and when they disagree the record says so. The score the system computed is what gets weighed, because it
comes out the same every time from the same figures.

### Adding them up

| Voice | Brings | Counts toward the total by |
|---|---|---|
| the deciding analyst | `weight * confidence` | `weight` |
| a reading analyst | `weight * score`, plus when it agrees with the direction, minus when against | `weight * abs(score)` |
| a reading analyst with nothing past its lines | `0` | nothing; it is listed as silent |
| a validator that supports, after its claims were checked | `+validator_weight` | `validator_weight` |
| a validator that opposes | `-validator_weight` | `validator_weight` |

`score = max(0, what they bring) / what they count toward`, so it sits between 0 and 1. With nobody
else speaking it is the deciding analyst's own confidence. Agreement lifts it, opposition lowers it
in proportion to how strong the opposition is, and only opposition that outweighs the case takes it
to zero. Silence neither lifts nor dilutes it: a reading barely past its line moves the score
barely, and silence is the limit of that.

The first rung of the ladder the score reaches gives the size multiple, and then every cap that
already existed still applies: `[controls].max_position`, the notional ceiling implied by
`max_leverage` against `[run].equity`, the risk per trade, the desk limit, and the venue's step,
rounding down and never up.

### Worked through, with the weights above

| What the desk sees | Score | Size |
|---|---|---|
| a short at 0.70, macro barely bullish (+0.15), sentiment barely bullish (+0.057) | 1.222 / 2.178 = **0.561** | half the base |
| a long at 0.70, macro +0.6, on-chain +0.5, sentiment -0.4 | 2.55 / 3.55 = **0.718** | half the base |
| a long at 0.90, every analyst a full band in agreement, two validators for, one abstaining | 6.8 / 7.0 = **0.971** | the whole base |
| a short at 0.70, every analyst a full band bullish | below zero, so **0** | no trade |

The first row is a real cycle from 2026-10-04. Before scores were graded it read as two full
voices against and refused at 0; as figures it is a small dent in a clear case.

The size is still a rule rather than a judgement. A model asked "how big should this be" answers
differently on Tuesday than on Monday from the same figures, and a size that depends on which
sentence was written last cannot be backtested, reproduced or audited. Leverage is not a lever any
role pulls either - it is what the notional works out to against the equity, and `max_leverage` is
the ceiling on that.

The confluence matrix above is different on purpose: it reads the words, not the scores, and a
role you list in `required` vetoes when it points the other way however slightly. Use it only for
the roles you really want to have a veto; leave the rest to conviction.

When the size the ladder chooses is not the base size, the round trip is priced again at the size
that is actually going out, because impact is a function of size. The cost in the record is the cost
of the trade that was admitted.

## The base size, said once

`[run].size` is in base units, which is fine for a desk on one instrument and awkward for a desk on
several: 0.01 is eight hundred dollars of BTC and twenty-seven of ETH. A desk that watches more than
one instrument says it in quote currency instead:

```toml
[run]
size_quote = 100                                        # quote currency, per order, before conviction
size_step = { BTCUSDT = 0.001, ETHUSDT = 0.01, SOLUSDT = 0.1 }
```

- Exactly one of `size` and `size_quote` is declared. Two figures that could disagree are refused
  when the file is read.
- The cycle converts it at its own mid, after the trigger and before any model is asked, and rounds
  down to the instrument's step. A size that comes to less than one step stops the cycle there and
  costs nothing. The conversion is in `cycle.json` under `size`: the quote amount, the mid, the step
  and the base size it became.
- `size_step` may be one number or a table with one increment per instrument. Venues step
  instruments differently (Bybit: BTC 0.001, ETH 0.01, SOL 0.1), and a table must name every
  instrument the desk can trade.
- Everything after the conversion is unchanged: the conviction ladder multiplies it, and
  `[controls].max_position`, the leverage ceiling and the risk per trade cut it. `max_position` is
  still in base units, so on a quote-sized desk it caps each instrument at that many units whatever
  they are worth; `tra check` says so.
- A replay, paper or live session trades one fixed size and has no cycle mid to convert at, so it
  refuses `size_quote` by name and asks for `[run].size`.

`tra check` puts the size against the venue cap before a cycle is paid for: a `size_quote` above
`[execution].max_notional` is a refusal, and a ladder whose top rung would cross the cap is named.

In venue mode the hand also checks the size against the venue's own market before sending. ccxt
rounds an amount to the market's precision on its way out - 0.036 ETH goes out as 0.03 - which is a
different trade from the one the cycle priced and wrote down. A size off the venue's step, or under
its minimum, is refused with both figures instead.

## Risk per trade: the size the account can afford

Conviction answers how much of the base size the desk wants behind an idea. That is a different
question from how much the account can afford to be wrong about, and the second one is what a risk
manager is paid to ask: with the stop where it is, what does this position lose if it is hit?

```toml
[controls]
max_risk_per_trade_bps = 50      # of [run].equity, at the stop from [exit]
```

The arithmetic is the distance to the stop times the size, against the equity, in basis points. If
it is over the allowance, **the size is cut until it fits** - never the stop moved, because moving a
stop to make a trade fit is how a desk learns the wrong lesson from a loss. If the size that would
fit is below one step of the venue, the cycle refuses. The cut and the figures behind it go into the
record and into the intent, so `risk_bps` is there beside the size for every trade the desk took.

This is a fixed calculation, not a role. A model asked how much to risk answers differently on Tuesday than
on Monday from the same figures, and the session runtime has always enforced this limit - a cycle
that can send its own orders has to enforce it too. `tra check` refuses the combination that cannot
be applied: a declared risk limit with no stop, or with no equity to measure against.

## The desk: what the whole book may carry

Every bound before this one is about one position. A desk that watches three instruments can pass
all of them three times over and still be carrying more than anyone decided to carry, because
nothing was ever asked about the total.

```toml
[desk]
max_open_risk_bps = 150          # the sum of what every open position loses at its own stop
max_open_positions = 3           # how many instruments may be open at once
correlation_above = 0.7          # a candidate moving with something already open is one trade
```

The first figure is the one that matters. A per-trade limit of fifty basis points is a different
promise when one position is open and when six are, and only the sum says which promise is being
kept. It is applied after the per-trade cap, so what it adds is the figure that trade actually
carries rather than the one it asked for.

**What counts as open** comes from the venue in venue mode - the venue is the book, and it knows
what filled and what an attached exit closed while nobody was watching - and from the intents on
disk everywhere else. The cycle records which, in `desk.source`, so nobody has to guess how the
total was reached. A position whose risk is not on the record **refuses the next trade**: a sum that
quietly leaves something out is worse than no sum at all.

Expect this on a universe of majors. BTC, ETH and SOL move together on one-minute returns most of
the time (0.82 to 0.86 on the demo desk on 2026-10-05), so with `correlation_above = 0.8` one open
position holds the other two back, and the desk carries one position at a time whatever
`max_open_positions` says. That is the rule doing its job, and a pass that ran no cycle says so in
those words. If you want more than one position, either raise the threshold (0.9 admits most
pairs of majors) or watch instruments that do not move together. The hold applies whichever way the
new trade would point, so a hedge against the open position is held too.

`correlation_above` belongs to the pass rather than the cycle, because it decides which instrument
is worth a cycle at all. It is measured on the bars the pass already pulled - the correlation of
bar-to-bar returns over the window, and nothing at all under twenty points, because a correlation on
ten bars is a number with no claim behind it. A candidate moving with an open position at or above
the threshold is refused with the figure and the name:

```
ETHUSDT: has been moving with BTCUSDT at 0.931 over the window, at or above the declared 0.7;
one trade in two names is not two trades
```

## The targets

What the desk is trying to do, in numbers, checked before there is an intent:

```toml
[targets]
min_net_bps = 5                  # what must be left of the edge after every cost the gate priced
min_edge_multiple = 2.0          # the claimed edge, as a multiple of the round trip
min_reward_risk = 1.5            # take profit against stop, both from [exit]
max_research_bps = 25            # model spend since the last intent, against this notional
max_round_trips_per_day = 6      # intents already written today for this instrument
```

`min_edge_multiple` is the one that keeps a desk honest, and it is the difference between trading
and collecting tolls for a venue. A round trip costing 18 basis points against a claimed edge of 20
is not a two basis point trade: the fees are certain, the edge is a probability, and slippage only
ever moves one of them. Live cost cannot be known exactly in advance either - the spread the cycle
priced on is the spread it saw, not the one the order gets. Requiring the edge to be a multiple of
the cost prices that asymmetry before a model is believed rather than after the fills come in. A
real refusal reads: `the claimed edge is 1.94x the round trip and the desk declared 2.0x; the cost
is certain and the edge is not`.

`max_research_bps` prices the other real cost. It is every model call since the desk last wrote an
intent, against the notional of the trade those calls produced, so a day of refusals is paid for by
whatever the day eventually trades rather than disappearing from the track record. It needs
`[agents.pricing.<model>]` to mean anything, and `tra check` says so when the target is declared and
the price is not - including when the declared price is zero.

The round trips are counted from the intents the cycles actually wrote today, not from a counter in
memory: a process that restarts must not forget what it has already done. Every refusal names its
figure, so "targets: net 4 bps is below the declared minimum 10 bps" is the whole explanation.

The targets panel reads the trade the way a trader does, then shows what the desk checked:

```
position       short at about 86114.35
take profit    84392.06  price -2.00%, the position +200.00 bps
stop loss      86975.49  price +1.00%, the position -100.00 bps
reward : risk  2 : 1
expected edge  25.00 bps a trade on average; [agents].magnitude_bps, declared by the operator
               and not measured here
round trip     12.46 bps, paid win or lose
after cost     +12.54 bps  (the edge is 2.01x the cost; the desk asks 2.0x)
model spend    0.00 bps of this notional; not priced: google:gemini-flash-latest
trades today   0 before this one
```

Each exit shows two numbers because a short gains when price falls: the price's move, and what that
move does to the position.

### Where the stop and the take profit sit

`[exit].mode` picks one of three ways. The analyst proposes, the system bounds, and you set the bounds.

**Analyst** is what `tra init --agent` writes, and what an agent desk uses when `[exit]` is left
out. The technical analyst names two prices with its decision: `stop_price`, where its reading is
wrong (beyond the swing or the range edge the setup leans on), and `target_price`, where the
mechanism is done. It is handed the window's `range_high`, `range_low` and `atr` to name them
from. The system then:

| Check | What happens |
|---|---|
| a level on the wrong side of the price (a long's stop above it) | set aside; the measured stop below stands, and the record says why |
| the stop inside `min_stop_atr` average true ranges (1 by default) | widened to it: a stop inside one bar's ordinary range is hit by noise before the idea is wrong, and the wider stop makes the position smaller, not the loss bigger |
| the stop tighter than `min_stop_frac` or wider than `max_stop_frac` | held to that bound |
| the target closer than `min_reward_to_risk` times the stop | the trade is refused; moving the analyst's target out would be inventing one |
| the target past `max_reward_to_risk` times the stop | brought in to it; a far target makes the break even look easy and is rarely reached |
| no stop named | the volatility stop below; no target named, `reward_to_risk` times the stop |

```toml
[exit]
mode = "analyst"
min_stop_frac = 0.002        # a stop never tighter than 0.2 percent of price
max_stop_frac = 0.03         # and never wider than 3 percent
min_stop_atr = 1.0           # nor inside one bar's ordinary range; 0 turns this off
min_reward_to_risk = 1.5     # a target closer than 1.5x the stop is refused
max_reward_to_risk = 4.0     # a target past 4x the stop is brought in
stop_atr = 1.5               # with no level named: the measured stop
reward_to_risk = 2.0         # and the take profit at twice it
```

Size still comes from `[controls].max_risk_per_trade_bps` against the stop that was placed, so an
analyst who names a wide stop gets a smaller position, never a bigger loss.

**Volatility** measures the stop on every trade from the market it is in, whatever the analyst
named:

```
stop        = stop_atr x ATR(atr_bars) / price x sqrt(horizon bars), held in [min_stop_frac, max_stop_frac]
take profit = reward_to_risk x stop
```

```toml
[exit]
mode = "volatility"
stop_atr = 1.5               # the stop in average true ranges
reward_to_risk = 2.0         # take profit against stop
min_stop_frac = 0.002        # never tighter than 0.2 percent of price
max_stop_frac = 0.03         # never wider than 3 percent
atr_bars = 14                # the default
```

ATR is Wilder's average true range on the decision timeframe's closed candles, the same figure a
charting terminal shows, so any stop can be checked against one. A trade meant to last longer gets
room to match: the square root of the analyst's horizon (capped by `[exit].timeout_bars` when the
desk closes on time) is how far a market usually wanders in that many bars. Read off OKX's
one-minute candles on 2026-10-10 with the defaults and a five-bar horizon:

| Instrument | ATR | Stop | Take profit |
|---|---|---|---|
| BTCUSDT | 1.4 bps | 20 bps (the floor) | 40 bps |
| SOLUSDT, 30 bars | 4.6 bps | 37 bps | 75 bps |
| FILUSDT | 10.8 bps | 36 bps | 72 bps |
| FILUSDT, 30 bars | 10.8 bps | 88 bps | 177 bps |

A wider stop does not mean a bigger loss. `[controls].max_risk_per_trade_bps` cuts the size until
the loss at the stop fits, so a volatile coin with a wide stop puts the same share of equity at risk
as BTC with a tight one, on a smaller position. The cycle record keeps the ATR, the horizon, the
stop it measured and the bound that held it under `exits.measured`, and the cycle output says it in
one line.

When the candles cannot give a range (fewer than `atr_bars + 1` closed, or a market that did not
move), the trade is refused with the reason. A guessed stop is not a stop. Volatility mode is for
agent desks; a scripted strategy is simulated bar by bar and stays on fixed. Outside the agent cycle, where
one number is needed up front, the widest stop the rule can place stands in, which overstates the
risk rather than understating it.

**Fixed** puts every trade at the same distance:

```toml
[exit]
mode = "fixed"               # the default when mode is left out
stop_frac = 0.01             # 1 percent of price, on the adverse side
take_profit_frac = 0.02
```

It is simple and it is the same for every market, which is the problem: 1 percent is wide for BTC
over a few minutes and narrow for a coin that moves that much in an hour. The two modes do not mix;
a volatility key in a fixed table, or `stop_frac` in a volatility one, is refused rather than half
read.

Either way, a take profit closer than the round trip is refused (`exit: ... a winner would lose
money`): the best outcome the trade can have would still lose.

#### Why it is built this way

- **The analyst proposes, the system bounds.** Where an idea is wrong and where it is done is a reading
  of the chart, which is the analyst's job. How far that may be, how much it may risk and whether
  the costs leave it worth taking is arithmetic on your limits, which is the system's job. A model left to
  place stops on its own gives a different answer on the same chart; a model whose levels are
  held inside your bounds cannot hurt you by it.
- **The range, not a percentage.** The average true range is the most common way a trading desk
  measures how far a market moves in one bar, and it is the figure every charting terminal
  publishes, so a stop in this record can be checked by hand against one.
- **The square root of the horizon.** A market that wanders one ATR in a bar usually wanders about
  the square root of N ATRs in N bars. A trade the analyst expects to last 30 bars needs more room
  than one meant to last 5, or the stop is hit by ordinary noise before the idea has had its time.
- **A floor and a ceiling.** On a very quiet minute the measured stop can sit inside the spread and
  the fees; on a violent one it can be wider than anyone meant to risk. The bounds are yours, and
  the record says when one of them held.
- **The analyst's target is its claim; the losers are counted separately.** When the analyst names
  a target and it survives the bounds, that distance is the move the cost gate and the targets
  weigh: it is the analyst's own reading, already held to `max_reward_to_risk`. A target alone
  would count every trade as a winner, so it never stands alone: the break-even bound prices the
  stops and the round trip against it and refuses a trade that needs more than half its kind to
  win. A take profit the system set as a multiple of the stop claims nothing, so without a named
  target `[agents].magnitude_bps` is still the number the gate weighs. The record says which one
  was used (`edge_source`).
- **No guessed stop.** When the candles cannot give a range, the trade is refused. Falling back to
  some default would put a stop on the venue that nothing measured.
- **One mode at a time.** A table holding both `stop_frac` and `stop_atr` would be read by a person
  one way and by the system another, so it is refused, as is any key `[exit]` does not know. A
  typo in an exit setting should stop the desk at `tra check`, not at the venue.

Every intent also records `break_even_win_rate`, and the cycle output says it in one line: the
share of trades like this one that must reach the take profit for the stops and the round trip to
come out even, `(stop + round trip) / (take profit + stop)`. It is arithmetic on figures already
priced, with no guess at the odds. A 2:1 bracket needs a third of its trades to win before costs;
with a 20 bps stop and a 13 bps round trip it needs 55 percent. Compare it with the win rate your
round trips actually reach.

`[targets].max_break_even_win_rate` (0.5 by default) refuses a trade whose own stop, target and
round trip would need more than that share of winners. It is the bound on the analyst's levels and
on the measured ones alike, and it is why a one-minute desk refuses many trades a fifteen-minute
desk takes: the round trip does not shrink with the timeframe. Set it to 1 to turn it off.

The expected edge is a different thing from the take profit, on purpose. The take profit and the
stop say where this position closes; the edge is what the desk expects to earn a trade on
average, across the ones that hit the stop, the take profit or the time limit. The cost gate and
`[targets]` judge the trade on the edge less the round trip, because that is the number a desk
lives on. Model spend names the calls that had no declared price in `[agents.pricing]`, rather
than counting them as free.

## The cost gate, and what each number is worth

Before there is an intent, the system prices the round trip on the book the snapshot is holding. Every
component says how much we actually know:

| Component | Certainty | Where it comes from |
|---|---|---|
| `fee_round_trip` | declared | the channel's schedule in `envelope.toml` |
| `spread_cost` | measured | half the spread of this snapshot's book, both legs |
| `impact` | computed | walking the visible levels at the declared size; the exit leg is assumed symmetric |
| `funding_accrual` | declared | the settlements a horizon spans, at the rate that stands now |
| `latency_slippage` | estimated | `[run].slippage_bps`, an operator's bound, not a measurement |

The gate is one comparison: the claimed edge must be at least the total plus the margin the
operator declared. The claim itself is labelled too. It is the analyst's own target when one was
named and survived the [exit] bounds, and `[agents].magnitude_bps`, a declaration, otherwise;
when layer 1 or layer 5 has measured the phenomenon, the same field carries the measurement and
the label changes with it.

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

## Watching a desk you are not running

`tra watch` is the other half of the console, and the answer to "can I look at a running desk
without disturbing it":

```bash
tra watch --every 5s --limit 20
```

It opens the directory a desk writes into, reads the records that are already there, and redraws
them. It opens no socket, builds no client, holds no key and takes no lock - so opening it, closing
it and opening it again does nothing at all to the desk. A record caught halfway through being
written is skipped rather than shown wrong.

What it shows, all of it from files: the last decisions with their setup, conviction, risk and what
became of them; the last pass over the universe; the book as the newest cycle recorded it; anything
that broke; and what the models have cost across the cycles in view.

This matters for how a desk is deployed. **The console runs commands in its own process**, so a
`tra run --every 15m` typed at the prompt ends when the console does. A desk meant to keep running
belongs under a supervisor, and `tra watch` is how it is looked at from anywhere else.

## Watching it work

A cycle spends most of its wall clock waiting on sockets, so a terminal that prints only the outcome
looks like a hung process. Every step says when it starts and how it ended, and the console draws
the pipeline the project configured with each step pending, running, done, refused or held:

```
step      | state    | detail
----------+----------+---------------------
data      | done     | 4 kind(s) (1.2s)
trigger   | done     | moved 41.20 bps
technical | done     | 5.3s
macro     | running  |
cost      | pending  |
risk      | pending  |
targets   | pending  |
intent    | pending  |
```

Progress travels on its own wire and can change nothing: a watcher that throws is swallowed, because
a broken progress bar is not a reason to lose a decision. `--json`, a pipe and `--plain` all give the
lines instead, unchanged.

## Agents run together, and one failure is not the end

Analysts go out on a small pool of threads, because a model call is seconds of waiting on a
socket. Each answer is written the moment it arrives, so what a cycle already paid for is on disk
even when a later call fails. A failed agent becomes a line in `cycle.json` rather than a
traceback, and the results are always reported in the same order no matter who finished first.

A cycle is not resumed. Its answers belong to the snapshot it took, and asking the next model on a
newer book would mix two moments in one decision; the next cycle takes its own snapshot and asks
again. `tra agent session --resume` is different: a session replays recorded data, so its answers do
not go stale.

## The execution mandate

Three modes, and the distance between them is deliberate.

| Mode | What happens to an admitted intent | What it needs |
|---|---|---|
| `sandbox` | written into the cycle's own directory | nothing; this is the default |
| `signals` | written for execution somewhere else, by someone else's system | nothing |
| `venue` | sent to the venue, with real capital behind it | a mandate, a leverage, a cap per order, the keys; on mainnet the canary tier |

Venue mode does not start until all of this is true, and `tra check` names whatever is not:

1. `MANDATE.md` is in the project and carries the mandate text word for word, and
   `[execution].acknowledged_by` and `acknowledged_on` name who signed it and when. Read it with
   `tra mandate` - it says that the losses are yours - then `tra mandate --sign "Your Name"` does
   both: it writes `MANDATE.md` when the project has none (one you wrote yourself is never
   overwritten) and sets the two lines, with today's date or `--on YYYY-MM-DD`. Venue mode stays off
   until you set `[execution].mode = "venue"`.
2. The signature is yours alone: no role, model or tool on the MCP surface can sign.
3. `[execution].max_notional` caps one order, and `[execution].leverage` is chosen. Neither has
   a default: both are what a defect would cost you.
4. On mainnet, the ladder stands at `CANARY`, promoted with evidence and a signature. A demo or
   testnet account holds no capital, so it is where that evidence is earned and does not wait on
   the ladder.
5. The venue's keys are in the environment: the shell, an `--env-file` you name, or the `.env`
   beside `strategy.toml`, never `strategy.toml` itself. They are read by the execution layer alone,
   and they never appear in a brief, a prompt, a record or a log.
6. `[execution].environment` (else `[live].environment`, else `demo`) says which of the venue's environments an order goes to: `demo`, `testnet`
   or `mainnet`. These are three different places with three different accounts, and a key for one
   is rejected by the others. A cycle that cannot route to the environment it was told refuses
   rather than sending somewhere close.

`tra keys --probe` answers the question `tra check` cannot: not whether a key is set, but whether it
works - one free authenticated call per provider, and for the venue an account read through the very
client an order would go through. No prompt is sent and no order is placed. Nothing prints a key:
each one is shown as eight hex characters of its own SHA-256, which is enough to tell two keys apart
and cannot be turned back into either.

```
ok    provider groq ([agents].provider): verified [GROQ_API_KEY 8e4edee0]
ok    provider google ([agents].fallback): verified [GEMINI_API_KEY d3869001]
ok    venue bybit (demo), for `tra live`: verified [BYBIT_API_KEY da8c3e5b, BYBIT_API_SECRET 9f9bfb94]
ok    venue bybit (demo), for `tra run`: verified [BYBIT_API_KEY da8c3e5b, BYBIT_API_SECRET 9f9bfb94]
```

The last two rows are two different clients reaching the same venue: `tra live` has its own signed
REST client and `tra run` sends through ccxt. They are listed separately because a readiness check
that proves one path works proves nothing about the other, and it is the second one a cycle uses.

A desk that watches several instruments builds one hand per instrument, and a hand refuses an intent
about any other instrument. One symbol, one instrument, one order.

The order follows the channel. `[run].venue_key` names a channel in `envelope.toml`, and the name
is `<entry>-<exit>`, each `maker` or `taker`:

| Channel entry | What is sent | If it does not fill |
|---|---|---|
| `taker` | a limit IOC at most `[execution].max_slippage_bps` through the touch, crossing the book at once | what did not fill inside the band is cancelled, never chased |
| `maker` | a post-only limit at this snapshot's own touch, offset by `price_offset_bps` | cancelled after `limit_timeout_s`; nothing is chased across the spread |
| anything else | nothing | venue mode refuses: the fee you priced depends on the answer |

That is not a style preference. A channel named `maker-taker` was priced at the maker fee, and
sending a taker order under it would pay a fee the gate never priced.

Three habits make this auditable after a failure, which is the only time it matters. Every order
carries a client id derived from the cycle and the instrument, so the same cycle run twice cannot
open two positions. The account is read before anything is sent, and an order is never added to a
position pointing the other way unless `adopt_open_position` says so - a hand that cannot read the
account does not send at all. And nothing is retried: a venue that refuses is recorded as a refusal,
because a retry loop around real capital is how one defect becomes ten orders.

The size is the one the conviction rule chose, capped again by `[execution].max_notional`. What came
back is written to `placed.json` beside the intent, and the intent is written first: whatever the
venue does next, the record of what this cycle decided survives it.

### The book at the moment of sending

The snapshot a cycle priced on is as old as its slowest model: half a minute is ordinary. Before an
order goes out the venue hand reads the book again. A maker order rests at the touch as it is now,
not as it was, and the record keeps the priced mid, how far the mid moved (`drift_bps`) and where the
order rested. `[execution].max_drift_bps` refuses the order when the mid has moved further than that
against the trade, because a decision the market has already left behind is not the decision the
gates admitted. A move in the trade's favour never refuses.

## Capital, orders and positions in venue mode

Before every order the account's equity and free margin are read, and sizing uses the smaller of
the account's equity and `[run].equity`. At send time your leverage and margin mode are applied at
the venue and read back, the estimated liquidation must sit beyond the stop, and a taker entry goes
out as a limit IOC within a slippage band. At the start of every pass each open position is matched
to the order that filled it, closed by time past `[execution].hold_limit`, written up when the
venue's exits closed it, and flattened when the kill switch is armed. [Capital, orders and positions](/opensource/capital) has every
setting, every check in order, the arithmetic and the records.

### What happens after the order

The stop and the take profit go to the venue **attached to the entry**, in the same call. The system
computes both against the price the cost gate priced on, from the market's own range or from
the fixed fractions ([where they sit](#where-the-stop-and-the-take-profit-sit)), so no model is
ever asked where a stop belongs. A filled position with no
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
| `venue` | the intent is sent to the venue through ccxt, after the checks in [the execution mandate](#the-execution-mandate) |

## When a cycle refuses

| Refusal | What it means |
|---|---|
| `data <kind>: ...` | a chain was exhausted; see [The data gate](/opensource/data), and give that kind a longer `ttl` or another source |
| `no technical brief: ...` | there were not enough closed candles in the snapshot to compute features |
| `trigger: the mid has moved ...` | nothing happened worth paying a model to look at; no tokens were spent |
| `technical: flat` | the analyst saw nothing worth pricing |
| `technical: ... with no setup named` | a direction nobody could name the shape of is not a trade |
| `technical: not one figure ... survived` | the direction could not point at a figure in its own brief |
| `technical: confidence ... below` | the answer was under `[agents].min_confidence` |
| `exit: ... closed candle(s); an average true range over ... needs ...` | volatility mode could not measure a range; raise `[agents].lookback_bars` or lower `[exit].atr_bars` |
| `exit: the take profit is ... a winner would lose money` | the take profit sits inside the round trip cost |
| `exit: ...% of trades like this would have to reach the take profit ...` | the break even is above `[targets].max_break_even_win_rate` |
| `exit: the analyst's target is ...x its stop ...` | the analyst's own target is closer than `[exit].min_reward_to_risk` allows |
| `risk: ... bps of equity at the stop` | the smallest size the venue takes still risks more than `[controls].max_risk_per_trade_bps` |
| `desk: ... bps of equity is already at risk` | what the book already carries plus this trade is over `[desk].max_open_risk_bps` |
| `desk: ... has at risk is not on the record` | a position is open whose risk nobody can state, so the desk's total cannot be stated either |
| `targets: the claimed edge is ...x` | the edge was not the multiple of the cost the desk declared |
| `cost: no book in the snapshot` | nothing can be priced without a book, so nothing is |
| `cost gate: ... is below ... required` | the claim did not clear the cost plus the margin |

Every refusal on this list stops the cycle before an order exists. The last one is the gate doing
the job the research programme this package came from was built to do.

## Did the gates earn their place: `tra hindsight`

A gate that refuses is easy to defend and hard to judge. It refused a trade; nobody saw what the
trade would have done. `tra hindsight` replays every directional decision in `runs/cycles` on the
candles that followed it, then puts each gate's refusals next to what the desk let through:

```text
hindsight: 81 decisions replayed over 7 day(s), 43 of them independent, each held 4h on 5m candles, net of its round trip
  admitted                 21  mean   -41.3 bps  median   -61.6  won  14%  sum   -867.4
  refused by targets       16  mean  -281.6 bps  median   -73.7  won  12%  sum  -4505.2  refused worse trades than it let through
  refused by exit          14  mean   -47.2 bps  median   -46.6  won  21%  sum   -661.2  no clear difference yet
  refused by regime        11  mean    62.0 bps  median    65.1  won  64%  sum    681.8  refused better trades than it let through; review it
  side: the model's -79.5 bps against 36.4 on the reverse; the reverse of the model's side did better; its calls carried no edge
```

That is a real desk: an OKX demo account, decisions from 8 to 16 October 2026. Read it the way it is
meant to be read. The targets gate kept out trades that would have lost far more than the ones it admitted.
The regime gate kept out trades that would have done better, on eleven decisions, which is a reason
to watch it, not yet a reason to switch it off. And the last line is the one that matters most: on
those days the deciding model's side did worse than its exact reverse. No gate can fix that. A gate
only refuses; it cannot turn a call with no edge into one with an edge.

How each decision is replayed:

- From the open of the first candle after the cycle finished, on the side the analyst took.
- On the stop and take profit the record holds: the levels sent with the order, else the exits the
  cycle measured, else the analyst's own named levels. With none, the trade closes at the hold limit.
- A candle that touches both levels counts as the stop. Inside one candle the order is unknown, so
  the replay takes the worse reading.
- Held for `[exit].timeout_bars` on the decision bar (`--hold 4h` to change it), on 5-minute candles
  (`--bar 1m` for a finer path), net of the round trip the cycle priced.
- The same path on the opposite side gives the side check.

The verdicts are deliberately reluctant:

| Verdict | When |
|---|---|
| `too few to tell` | fewer than 10 decisions on either side, or fewer than 5 days for the side check |
| `refused worse trades than it let through` | a permutation test puts the admitted group ahead with p at or below 0.05 |
| `refused better trades than it let through; review it` | the same test, the other way |
| `no clear difference yet` | anything between |

The side check resamples whole days, because one day's tape moves every decision on it together.
"Independent" counts decisions after dropping those on an instrument inside an earlier one's hold;
the rest are the same bet asked again.

The candles come from the desk's own ohlcv chain, the venue first. A path is kept under
`runs/cycles/hindsight/` once its hold is over, so a second run asks the venue for nothing it already
has. The report goes to `runs/cycles/hindsight.json`, and the quant auditor reads it there, beside
the round trips the venue closed.

A replay is not a fill. It assumes the entry at the next candle's open and an exit exactly at the
level, with no queue, no partial fill and no gap past a stop. That is why it is reported beside the
real round trips, never in place of them.
