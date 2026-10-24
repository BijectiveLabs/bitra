---
description: "The agent roster in TRA: what each role reads, the closed schema it answers in, and why no agent can loosen a limit, size a trade or send an order."
---

# Agents

Agents here read a lot and decide very little. In a cycle each role gets the part of the brief
its job needs and nothing else, answers once inside a closed schema, and holds no tools. The
technical analyst proposes a direction; the system prices it, sizes it, checks it against the book and
decides whether it goes out. In a session the auditor also gets a small set of read tools, and the
files those tools can open are fenced to the project.

What no role holds is the authority to loosen a control. Limits can be tightened, never widened;
the ladder can be stepped down, never up; the kill switch can be armed, never disarmed. Those are
the system's own rules, tested and failing closed, and a role acts through them, not around them.

## The roster

| Role | Holds | May write | Cadence |
|---|---|---|---|
| `market_screener` | in a cycle, one live snapshot per instrument in the universe; in a session, the data gate, the envelope and the cost floor | nothing; it may only take an instrument out of a list the system already admitted | once per pass, or once per session |
| `technical_analyst` | the recorded trades and bars; in a research run the registered mechanism, in `tra run` the regime and higher timeframes, with your mechanism as a preference | a `Signal`, as the decision core of a run: a direction, a setup named from a closed list, the figures it read, and in `tra run` the stop and target prices that bound the idea, held by the system inside your `[exit]` bounds | once per closed bar, within its budget |
| `macro_analyst` | series the operator mapped in (events, cross-venue funding, sentiment) | nothing | every cycle in `tra run`; once per session in a session, when series are given |
| `onchain_analyst` | on-chain series the operator mapped in | nothing | every cycle in `tra run`; once per session in a session, when series are given |
| `sentiment_analyst` | dated scores and dated notes the operator curates, through the same series engine | `negative`, `neutral`, `positive`, or `not declared` when nothing was mapped in | reads only; nothing is scraped |
| `validator` | `figures_of`, `checked` over the brief the deciding analyst was given | one stance (pro, contra, neutral): the proposal as the analyst gave it (side, setup, levels, confidence) and the figures it was built from, never another analyst's reading, the cost, the claimed edge or the desk's rules; every claim it makes has to cite one of those figures with its value, and the system discards the ones that do not. With all three stances, pro and contra build their cases apart and neutral judges both ([the validators](/opensource/cycle#the-validators)) | once per cycle, when `[agents].validators` is set |
| `risk_manager` | in `tra run`: the client's limits, what is in force, the alarms, the round trips by instrument; in a session: the limits, the canary caps, the ladder, latency, net against the registration | in `tra run`: `reduce` (size multiple below 1), `pause` (instruments), `halt` (the kill switch), each for `hold_hours`; in a session: `tighten_limits`, `demote`, `arm_kill_switch` | in `tra run`, between cycles whenever the quant auditor is asked; in a session, after the runs |
| `executor` | the venue, the rate limits, the allocation across instruments | an allocation, fractions summing to at most one | after the risk decision |
| `auditor` | the ledger, every run, `usage.jsonl`, the ladder | `audit.json` | last |

Each role is a package under `trading_research_agents.agents`: a JSON schema the model must
fill, a parser that refuses anything outside it, and a prompt that names venues nowhere. No
role talks to another. The session hands each one what the system computed plus what earlier roles
wrote, and records every answer as a numbered file in the session directory.

## A session

```
tra agent session                      # every dataset in data/, [agents].magnitude_bps
tra agent session --config strategy.toml --envelope envelope.toml \
    --dataset BTCUSDT=data/btc.parquet --dataset ETHUSDT=data/eth.parquet \
    --magnitude 20 --out runs/session-1     # the same, spelled out
```

```
figures (system) -> screener -> [macro] -> [onchain] -> one replay per kept instrument,
                  the analyst as its core -> risk manager -> executor -> auditor
```

The system measures each dataset first (rows, print rate, realised volatility, turnover, the cost
floor for the claimed magnitude, and, when `observation.toml` sits beside the config or
`--observation` names one, layer 1 on the same prints: whether the declared event recurs on
that instrument, its counts per subperiod and its response) and the screener sees only those
figures. It keeps or
excludes; it cannot add an instrument it was not given, and a candidate it does not mention is
excluded, not silently kept. Each kept instrument then runs through the same replay loop as
`tra backtest`, with the analyst deciding on each closed bar and the controls admitting or
refusing as they always do. The risk manager reads the run reports and decides: continue,
tighten (applied only where the figure is smaller), demote (the ladder steps down, on the
reason), or halt (the kill switch file is written and nothing more is asked). The executor
allocates fractions across the kept instruments; it sends nothing. The auditor closes with a
verdict on the whole session, including what every call cost.

`strategy.toml` declares the analyst as the core and the budget:

```toml
[strategy]
agent = "technical_analyst"

[agents]
provider = "deepseek"                 # any registered provider; see Model providers
model = "deepseek-flash"
mechanism = "positions forced to close into the settlement window"
token_budget = 200000                 # the whole session, every role; keep it under the vendor's daily quota
max_calls_per_hour = 60
min_interval_s = 60
lookback_bars = 60
min_confidence = 0.6
magnitude_bps = 20                    # the claimed edge, for the cost floor
decide_every_bars = 5                 # ask the model every fifth closed bar; exits run on every bar
max_output_tokens = 512               # a reasoning model that thinks for 20 s is not a bar-cadence core
call_timeout_s = 60                   # per request, enforced by the SDK client; max_retries = 2 by default

[agents.pricing.deepseek-flash]       # what the operator was quoted; never a constant
input_per_million = 0.14
output_per_million = 0.28
```

The same `[strategy].agent` core runs under `tra backtest`, `tra paper` and `tra live`, so an
agent that survived a session can be taken up the ladder like any scripted strategy.

### The auditor reads before it answers

The auditor is not handed one brief; it works a bounded tool loop over the read surface:
`read_run_report`, `show_ledger`, `search_ledger`, `list_runs`, `show_ladder`. Each step is one
model call that either names a tool with its arguments or carries the final answer. The loop
is closed at its edges: a tool outside the catalog is refused by name, arguments are checked
against the function before it runs, an observation is cut at 6000 characters, the budget
applies to every step, and the loop ends without a verdict after six calls
(`SessionConfig.auditor_steps`). Every step, with its thought, observation or error, is in
`NN-auditor.json`.

### Resuming a session

```
tra agent session --resume --out runs/session-1
```

A session interrupted by a vendor rate limit, a spent budget or a lost connection does not
start again from nothing. With `--resume`, an answer already recorded in `--out` for a step
is reused instead of asked again, and a kept instrument whose run reached the end of its
data is reported from disk instead of replayed. A run that stopped early (a spent budget,
five failed calls in a row, a kill switch) is not a checkpoint: it runs again. Either way the
decision is written down in the session's refusals, so a report never hides that a model was
not called. `usage.jsonl` keeps the calls of the first
pass; the second adds only what it actually asked.

## What each role holds before it is asked

A role does not start from a blank prompt. The system computes the figures first and the model reads
a brief with the basis of every number in it: measured on the operator's data with a sample
size, or declared by the operator and labelled as such.

| Role | Tools (`agents/<role>/tools.py`) | What the brief carries |
|---|---|---|
| `market_screener` | `screen_figures` | per instrument: rows, print rate, realised volatility, first-lag autocorrelation, the 1:10 variance ratio, largest gap, last-hour volume against the average, turnover, the cost floor for the claimed edge, and a regime the system proposes from a written rule (`[agents.screen]` sets the two thresholds: `min_prints_per_minute`, `volatile_above_bps`) |
| `technical_analyst` | `features` | on each closed bar: returns over 1, 5 and 20 bars, the close as a z-score of the window, the gap to VWAP, the last bar's volume and range against the window's, the position between the window's high and low, the net move and trend strength with the regime they give, the variance ratio, and the market structure (swings labelled HH, HL, LH, LL, the trend they make, the last break of structure or change of character, invalidation, support and resistance); the same regime and structure on each higher timeframe in `[run].context_intervals`; the book imbalance and the open interest change since the last snapshot (see [the cycle](/opensource/cycle#the-market-around-the-decision)), and in `tra run` its own last reading of the instrument with the move since (`previous`). Nothing another analyst reads or says |
| `macro_analyst` | `macro_brief` over `data/series.py` | per series in `series.toml`: the value known at the time and when it became known (publication lag honoured), the state against the operator's declared threshold, the correlation and strongest lead-lag with the instrument's returns measured on the operator's data with n, the calendar events inside the horizon |
| `onchain_analyst` | `onchain_brief` | the same engine, plus the net over a window and the last value as a z-score of its own history |
| `risk_manager` | `risk_brief` | per run: latency from intent to fill (median, max, n, ratio to the declared), net per round trip overall and over the last window against the claimed magnitude, maximum drawdown, the favourable and adverse excursions of the closed round trips with what the winners gave back, which control refused and how often |
| `executor` | `size_for`, `rate_budget`, `export_signals` | a size that never exceeds a cap, rounded down to the venue's step; what the minute still allows; the run's admitted intents as `signals.jsonl` or CSV for execution elsewhere (`tra signals export RUN`) |
| `auditor` | `audit_brief`, then a tool loop over `read_run_report`, `show_ledger`, `search_ledger`, `list_runs`, `show_ladder` | usage per role with tokens per closed round trip, each promotion with whether its evidence names a gate, and a run against its replay trade by trade; then whatever it chooses to read, up to six calls |

### In a cycle: what goes in, what comes out

Every role in a cycle is told three things in its prompt and held to them in code: what its brief
carries, that every figure in it was computed before it was asked (so it has nothing to call), and
the schema its answer fills.

| Role | Reads | Answers | Code then checks |
|---|---|---|---|
| `technical_analyst` | the features, structure, regime and higher timeframes of its instrument, the book, open interest, its own last reading | `target`, `setup`, `confidence`, `horizon_bars`, `stop_price`, `target_price`, `reason`, `figures_used` | the schema, the setup against the measured regime, the levels on the right side of the price, every citation |
| `macro_analyst` | its series with what was known when, the operator's thresholds, measured correlations with n, the calendar | `regime`, `events`, `reason`, `figures_used`, `data_as_of` | the schema, every citation, words that belong to a later step (recorded) |
| `onchain_analyst` | its series, their net over a window and z-scores | `signal`, `reason`, `figures_used`, `data_as_of` | the same |
| `sentiment_analyst` | its scores and dated notes | `state`, `figures_used`, `reason`, `data_as_of` | the same |
| validators: pro, contra | the proposal and the figures it was built from; neither sees the other's case | `stance`, `claims`, `verdict`, `reason` | the schema; a claim that does not cite a figure it was given is dropped |
| validator: neutral, as judge | the same, and both cases as code checked them, without the advocates' verdicts | `stance`, `claims`, `verdict`, `reason` | the same; its verdict is the one that counts, and it is not asked when only one side has a case |
| `event_analyst` | the question, the outcome's book and recent prices, the calendar and your notes | `probability`, `confidence`, `reason`, `figures_used` | the schema, every citation |
| `market_screener` | one live snapshot per admitted instrument | `kept`, `excluded`, `picks` | it can only drop an instrument, never add one |

An answer that does not fit its schema is asked for once more with the reason, and then refused.

No analyst sees another's data or another's answer. The technical analyst reads its own market and
not the macro, on-chain or sentiment readings; each reading analyst reads its own shelf and nothing
the others were given; the validators argue from the proposal's own figures. A view that knows the
other views can lean toward them, and then two analysts agreeing says less than it seems to. The
readings are combined once, by code, in confluence and conviction, after every analyst has
answered on its own.
How that and the citation check have done on a demo desk is in
[Working with language models](/opensource/llm#when-an-answer-does-not-fit-or-points-at-nothing).

### Reading, not calculating

In a cycle no role calculates. The system pulls the data, computes every figure, checks its age, and
hands each role a brief; the role reads it and answers in a fixed schema: a side, a label, levels, a
confidence, a reason, and the figures it rested on, by name and value. Code then checks every cited
figure against the brief and throws away the ones that do not hold. This is deliberate: a model
adding up bars gets the arithmetic wrong at a price per token, two runs of the same model would
compute two different numbers, and a decision built on numbers nobody can recompute cannot be
audited. Every figure a role saw is in its brief on disk, and `tra runs verify` recomputes them from
the raw inputs.

The one role that calls tools is the research session's auditor, which may read up to six records
(`read_run_report`, `search_ledger` and the rest) before it answers. The MCP server exposes the same
read-only tools to any host.

What a role's answer does differs:

| Role | What its answer decides |
|---|---|
| `technical_analyst` | the side, the setup and the levels; every gate after it works on these |
| `macro_analyst`, `onchain_analyst`, `sentiment_analyst` | nothing directly: confluence and conviction read the state the system derives from your thresholds on the same figures, and the label is recorded beside it. With `[conviction].reader_labels` above 0, a label counts where nothing is declared, as with a file of headlines |
| validators | one vote each in conviction, after their claims are checked |
| `market_screener` | which instruments, from code's own order, get a cycle; it may drop, never add |
| `risk_manager` | between cycles: size cuts, pauses, a halt; never a loosening |

### Proposals made without the desk's rules

The analyst that proposes a trade and the validators that argue about it are never told what a
trade has to clear. They do not see the round trip, the edge multiple, the reward-to-risk floor, the
stop bounds, the risk per trade, the size, the equity or the leverage, and their prompts do not
describe those rules. A role that knows the bar can aim at it: a target stretched until the trade
passes the cost gate, a confidence raised until it reaches a size. Kept out, the analyst names the
side, the setup, the confidence and two levels (where the reading is wrong, where it is done) from
the market alone, and code applies your settings afterwards.

| Who | Knows | Does not know |
|---|---|---|
| `technical_analyst` | the market figures, the structure, the regime, the higher timeframes, your mechanism as a preference, its own last reading | the cost of the round trip, every `[targets]`, `[exit]`, `[run]` sizing and `[controls]` setting, what became of its last reading, and the other analysts' readings |
| validators | the analyst's proposal as given and the figures it was built from | the same, and the claimed edge |
| `event_analyst` (prediction markets) | the question, the outcome's bid, ask and mid, its recent prices, your dated notes and calendar | the fee, the margin an estimate has to clear and the size |
| code | everything you declared | nothing it has to guess |

What code changes is written beside what was proposed: each cycle's `exits.measured` record keeps
`proposed_stop_bps` and `proposed_take_bps` (the analyst's own levels) next to `stop_bps` and
`take_bps` (the ones applied) and the note that moved them. `tra hindsight` replays both on the same
path and says which did better, in a line like this one:

```
  levels: code's <net> bps against the analyst's own <net>; <which did better, or no clear difference yet>
```

So a result can be traced to one of three things: the data the desk was given, how good the model
is at reading it, and the settings you chose. A test keeps the rules out of these roles' briefs and
their prompts.

The spread and the funding rate stay in the technical analyst's brief. They are the market's own
figures, on every trading screen, not settings of yours.

### Risk: one role after the runs, one step inside each decision

Two different jobs share the word, and keeping them apart is deliberate.

**The step.** How much this trade may lose, and the size that follows, is arithmetic over the
operator's own declarations: the distance to the stop plus the round trip, times the size, against
equity, against `[controls].max_risk_per_trade_bps`, lowered by the desk's own record when `[sizing]`
is declared ([Sizing from the record](/opensource/sizing)). It runs inside every cycle, between the conviction ladder and
the cost gate, and it cuts the size to fit or refuses. No model is asked, because a model asked how
much to risk answers differently on Tuesday than on Monday from the same figures, and a size that
depends on which sentence was written last cannot be backtested or audited. See [The cycle](/opensource/cycle).

**The role.** Whether the limits themselves are still the right limits is a judgement over what
the desk has been doing: losses in a row, net per round trip against what was claimed, drawdown,
which control keeps refusing. In `tra run` the risk manager is asked whenever the quant auditor is,
and it may cut the size of new entries, pause instruments or halt the desk, each for a stated
number of hours (below). In a research session it reads the finished runs and may tighten a
limit, step the ladder down or arm the kill switch. Either way it cannot widen anything, and it
never decides a single trade.

So "where is the risk manager" has two answers: the per-trade limit is arithmetic the system does
in every cycle, and the judgement over a run of trades is the role's, made between cycles.

### The two auditors

`tra audit` asks both, in parallel, over records this project wrote itself: `runs/cycles/*/cycle.json`
and the usage ledger. Neither one can reach a venue, a key or a live figure, and neither one is part
of a cycle.

| Auditor | Reads | Looks for |
|---|---|---|
| `tech_auditor` | calls answered and failed per role, median seconds, where each kind of data came from and how old it was, what was refused or stale, tokens and latency per role, tokens per decision, installed versions | what is wearing out, what is wasteful, what is fragile |
| `quant_auditor` | what the deciding analyst said, intents and their size, the conviction behind them, the claimed edge, the cost it was priced at, what was left after cost, which gate refused the rest, the round trips a session or the venue actually closed with their excursions, and the last `tra hindsight` report: what each gate's refusals would have done |  whether the desk's own declarations are earning what they cost |

Both answer in the same shape: findings, each tied to figures from the brief with their values, each
carrying a proposal, and at most one proposed configuration change with the value it has now and the
value proposed. The system checks every citation against the brief, and every proposed key against
the project's own tables and the settings each table accepts, and discards what fails, keeping the
reason. An auditor cannot propose a setting the software does not have. A report with nothing left says
so rather than implying a finding.

**Nothing is applied.** The audit writes `audits/<time>/`: the briefs as they were given, the answers
as they came back, `audit.json` with what was discarded and why, and `proposals.md` for a person to
read. Applying a proposal is that person editing their own configuration.

### When they wake, and why it is not the same answer for both

An operator running this on a server is not watching it. The failure that matters to them is the one
that stops the desk quietly - a provider rate limits the roster halfway through, the run keeps going
through the motions, and nobody finds out until someone reads a log. So the two auditors wake
differently, because they answer different questions:

```toml
[audit]
on_alarm = true                  # the technical auditor, when a cycle reports something broken
alarm_cooldown_s = 1800          # per kind of alarm, not overall
quant_every_cycles = 20          # the quant auditor, on a count of cycles
quant_every = "24h"              # and/or on a clock, kept across restarts (at least 5m)
losing_streak = 3                # losing round trips in a row that wake the quant auditor; 0 is off
```

Something breaking is an event. `agents/alarms.py` reads a cycle's own record for four kinds -
`stopped` (the cycle did not finish), `provider` (a model call never came back), `data` (a chain ran
out or was used stale) and `venue` (the order was refused) - and the technical auditor is asked once
per kind, no more often than the cooldown. A gate refusing is **not** an alarm: a cost gate saying no
is the system working, and waking an auditor for it would be paying to be told the desk is fine.

Whether the desk is earning what it costs is a question about a stretch of time, so the quant auditor
runs on a count of cycles or a clock instead, and also wakes when the account itself says something:
the daily loss limit was reached, or several round trips in a row lost money.

| Situation | Who is asked | Notes |
|---|---|---|
| A model call failed after the vendor's retries and every fallback (a rate limit that did not clear, a timeout, a key refused) | technical auditor | a rate limit a retry or a fallback absorbed is not an alarm, because the cycle got its answer |
| A cycle stopped before it finished, a spent budget included | technical auditor | |
| `[controls].daily_loss_limit_bps` reached | quant auditor | once per UTC day; the limit working is not a breakage, so the technical auditor is not asked |
| `losing_streak` losing round trips in a row (3 by default) | quant auditor | once for each new loss while the run lasts; a winner ends it; a trip with no result is skipped |
| A data chain ran out, or a kind was used stale | technical auditor | |
| The venue refused or failed an order | technical auditor | |
| The same kind fires again inside `alarm_cooldown_s` | nobody | one look per kind per cooldown; a different kind still wakes it |
| Every `quant_every_cycles` cycles | quant auditor | a count of cycles run: at `--every 15m`, 4 cycles is about an hour |
| Every `quant_every` (for example `"24h"`) | quant auditor | a clock: the first cycle starts it, any quant look restarts it |
| Both on the same cycle | both, in one audit | one report, one ledger entry per call |
| The quant auditor is asked, for any reason | the risk manager as well, first | it may cut size, pause instruments or halt, never loosen; `[audit].risk_manager = false` leaves the account to your limits alone |
| A gate refused (cost, risk, regime, trigger) | nobody | the system working as declared |
| Any time you want | whoever you name | `tra audit` |

The defaults: `on_alarm` is on even with no `[audit]` table, and with it the daily loss and the
losing streak alarms; the scheduled quant look is off until you give it a count or a clock. Auditors
are asked between the cycles of `tra run`, single or `--every`.

### The risk manager in `tra run`

The auditors report; the risk manager is the one role that can act on the same figures, and only
toward caution. It is handed what the system measured from the run's own files (round trips over the last
twenty, wins, the losing streak, the result per instrument), the alarms that woke it, your limits
and anything it already put in force, and it answers one of:

| Decision | What the system does |
|---|---|
| `continue` | nothing |
| `reduce` | every new entry trades `size_multiple` (above 0, below 1) of its size for `hold_hours` |
| `pause` | the named instruments take no new entry for `hold_hours` |
| `halt` | writes the kill switch file, so the next pass flattens and stops; with no kill switch declared, every instrument is paused instead |

Each decision lapses after `hold_hours` (at most 72) back to your own settings, and two reductions
compound downward: a milder one never undoes a deeper cut. What is in force is in
`runs/cycles/risk.json`, every decision with its reason and triggers in
`runs/cycles/risk_decisions.jsonl`; delete `risk.json` to lift it early. An answer that does not
fit the schema, a size multiple of 1 or more, or a model that does not answer at all leaves your
limits exactly where they were. Your limits in `[controls]` are applied after the risk manager's,
whatever it decides.

What survives a restart is kept in `audits/reported.json`: the clock's last look, the day whose loss
limit was reported, and the round trip that last extended a losing streak, so restarting the desk
does not pay for the same look twice. The cycle count and the per-kind cooldowns live in the running
process and start again.

Neither one gains anything by waking this way. They read the same records, answer in the same shape,
cite the same way, and change nothing; the only difference is that a person no longer has to be the
one who notices. An auditor that fails - the same rate limit, most likely - is a line in the run's
output, never the end of the run. Their calls are counted in their own `audits/usage.jsonl`, so what
the desk spent deciding stays separable from what it spent looking at itself.

### Who weighs whom

A question worth answering plainly, because it decides where the token bill goes:

| Step | Who does it | What it may do |
|---|---|---|
| read one class of data and label it | a reading analyst | say what it sees, in its own vocabulary; the score it is weighed by is graded by the system from the same figures against the operator's lines |
| propose a direction and a horizon | the technical analyst | one target, one confidence, one reason |
| argue for or against that proposal | pro and contra, apart | claims that cite figures from the brief; the system discards the rest |
| judge the two cases | neutral | one verdict from the claims that survived, weighed on the premise of the setup |
| decide whether the context agrees | the confluence matrix, in the system | admit or refuse, from declared states and vote counts |
| decide how much goes behind it | the conviction rule, in the system | weigh every voice by its strength (confidence, graded scores, votes) and pick a size multiple off the operator's ladder; it may only shrink the base size |
| price the round trip | the cost gate, in the system | refuse a claim that does not clear its own cost |
| hold the desk's shape | the targets, in the system | refuse a trade that is not worth what the operator said it must be worth |
| watch what actually happened | the risk manager | in `tra run`: continue, cut size, pause instruments or halt, from measured behaviour; it never enlarges anything |
| read the machinery afterwards | the technology auditor | write findings tied to figures, and propose changes a person applies |
| read the desk's own results afterwards | the quant auditor | the same, over decisions and round trips |
| turn a decision into an order | the executor | caps, the venue's step, the rate budget, the signal file; no discretion |

No model sizes a trade and no model is asked whether the analysts agree. Both are rules over the
operator's own declarations, which is what makes a result reproducible tomorrow.

### Series from anywhere

[`examples/series.toml`](https://github.com/BijectiveLabs/bitra/blob/master/examples/series.toml) maps external series the way `mapping.toml` maps trades: the file, the
time column and its unit, the value column, the unit, and `publish_lag`, the time between an
observation and the moment it was actually available. A monthly print released two weeks after
month end is unknown for those two weeks, and the brief says so. Thresholds in the map are the
operator's declaration; a correlation is never written in the map, because it is measured on
the operator's data every session and reported with its n and window.

```
tra agent session --macro series.toml --events events.json --onchain onchain.toml
```

A session takes its maps as paths, one flag at a time. A cycle (`tra run`) reads the shelf instead:
`sources/<class>.toml` beside the config, one file per class of data, described in
[the cycle](/opensource/cycle). Same engine, same as-of rule, different way of saying what the desk has.
Fetching those files, and the `max_age` that keeps an old value from counting, are in
[sources](/opensource/sources).

## Cost and latency as numbers

Every model call is one line in `usage.jsonl`: role, provider, model, tokens in and out,
latency, the cost at the declared price, and the outcome. The budget refuses before a call,
never after; a spent budget ends the session flat with `stopped_by` saying so. The analyst is
asked only on bars of a minute or longer, because a call takes seconds and a bar shorter than
that would be decided on stale prints; `decide_every_bars` spaces the calls further apart while
the stops and timeouts keep running on every bar, and `max_output_tokens` caps what a model
may spend thinking, which is most of the latency and the cost of a reasoning model. The
auditor reports tokens per closed round trip, which is the figure that says whether an agent
is worth running on that instrument at all.

### Comparing models on the same bars

```
tra agent eval --model groq:openai/gpt-oss-120b --model deepseek:deepseek-flash     --model ollama:qwen3:8b          # the newest dataset in data/
```

An eval runs the analyst once per model over one dataset, under the same controls and in the
same order, and prints one row per model: net basis points after costs, round trips, calls,
failed calls, tokens, latency at the median and the 90th percentile, tokens per basis point
earned, and the cost at the declared price. Each model has its own budget and its own
`usage.jsonl` under `runs/eval-<time>/<provider>-<model>/`, so one that runs out does not
starve the next; `eval.json` holds every row. A model that earned nothing has no
tokens-per-bps figure. This is not a benchmark score: it is the same replay a session runs,
reduced to what a trader compares before choosing which model to pay for.

### Reading a run back

```
tra agent trace runs/session-1              # or a single run directory
```

A trace is the run's own record put in order: the role answers as they were asked, the
auditor's tool steps with what each returned or refused, every model call on the wall clock
with running tokens and cost, and the intents, fills and round trips on the bar clock. The
two clocks are never mixed; a replay of last January runs today. `--json` returns the same
events for anything that wants to draw them.

## What an agent can reach

The short version for anyone reviewing this before putting money behind it:

| Concern | How it is handled | Where |
|---|---|---|
| Memory | None between calls. Each call is one system prompt plus one brief built by the system; the deciding analyst's brief includes its own last reading, built by code from the record. Earlier cycles are files on disk, and only the auditors read them. | [Working with language models](/opensource/llm), `store/cycles.py` |
| What a role sees | Its own sections of the brief. Anything else is removed and the removal is recorded. | `agents/scope.py` |
| What a role may say | A closed JSON schema per role. Extra fields, a wrong enum, or a number out of range are refused before anyone reads the answer. | `agents/<role>/role.py`, `llm/form.py` |
| Whether its figures are real | Every cited figure is checked against the brief, value included. Citations that do not hold are discarded, and a deciding answer with nothing left is refused. | `agents/cited.py` |
| Tools | None in a cycle. In a session the auditor gets five read tools in a loop capped at six steps; every path a model writes has to resolve inside the project, the session or the ladder's directory, and hidden files (`.env`, `.git`, `.tra-history`) are never readable. The MCP server applies the same fence to whatever host calls it (`tra mcp serve --root`). | `agents/react.py`, `surface/confine.py` |
| Orchestration | A fixed pipeline: one parallel round (analysts), then, when validators are declared, the advocates together and the judge after them. Code passes the checked claims from one to the other. No graph to configure, no role calling another, no loop that a model controls. | `agents/cycle.py`, `agents/parallel.py` |
| Spend | Every call is a line in `usage.jsonl`. Declared ceilings (`token_budget`, `max_calls_per_hour`, `min_interval_s`) apply to the whole desk in one run, not to each role. | `llm/usage.py` |
| Credentials | Keys come from the environment: the shell, an `--env-file` you name, or the `.env` beside `strategy.toml`; never from a config file. A vendor or venue error is scrubbed of any credential this process holds before it goes into a record or a brief. | `llm/completer.py`, `trading/dispatch.py` |
| Text from outside | Headlines, notes, vendor messages and another role's reason reach some briefs as material to read. The prompts say so, and more to the point, none of those roles decides: the gates read the operator's declared states and measured figures. The role that proposes a direction reads numbers only. | `agents/prompts/` |
| Orders | No role can place, cancel or modify one. Only the venue hand sends, after every gate, under a signed mandate. | `trading/dispatch.py`, [The cycle](/opensource/cycle) |

A cycle is never resumed. Its answers belong to the snapshot it took, and reusing them against a
newer book would mix two moments in one decision. The next cycle takes its own snapshot.

## What a role cannot do

`promote`, `loosen_limits`, `disarm_kill_switch`, `raise_cap`, `place_order` are not actions
on the surface; a role that asks for one is refused by name, every time, and the refusal is
what it is told. Orders are not on the surface either: they leave the runtime through the
mandate, the controls and the caps (and on mainnet the ladder), or they do not leave at all. A model may decide;
the system executes and refuses.
