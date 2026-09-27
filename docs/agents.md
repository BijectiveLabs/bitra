# Agents

Agents in this project have full access: every role holds the whole tool surface, the raw and
audited data, every gate, the ledger, the ladder and the run reports, and the technical
analyst decides trades as the core of a run. What no role holds is the authority to loosen a
control. Limits can be tightened, never widened; the ladder can be stepped down, never up; the
kill switch can be armed, never disarmed. Those are code, tested and failing closed, and a
role acts through them, not around them. That is the difference between an agent with full
access and an agent that can empty an account.

## Seven roles

| Role | Holds | May write | Cadence |
|---|---|---|---|
| `market_screener` | the data gate, the envelope, the cost floor | nothing | once per session |
| `technical_analyst` | the recorded trades and bars, the registered mechanism | a `Signal`, as the decision core of a run | once per closed bar, within its budget |
| `macro_analyst` | series the operator mapped in (events, cross-venue funding, sentiment) | nothing | once per session, when series are given |
| `onchain_analyst` | on-chain series the operator mapped in | nothing | once per session, when series are given |
| `risk_manager` | the limits, the canary caps, the ladder, measured against declared latency, net against the registration | `tighten_limits`, `demote`, `arm_kill_switch` | after the runs |
| `executor` | the venue, the rate limits, the allocation across instruments | an allocation, fractions summing to at most one | after the risk decision |
| `auditor` | the ledger, every run, `usage.jsonl`, the ladder | `audit.json` | last |

Each role is a package under `trading_research_agents.agents`: a JSON schema the model must
fill, a parser that refuses anything outside it, and a prompt that names venues nowhere. No
role talks to another. The session hands each one what code computed plus what earlier roles
wrote, and records every answer as a numbered file in the session directory.

## A session

```
tra agent session --env-file .env       # every dataset in data/, [agents].magnitude_bps
tra agent session --config strategy.toml --envelope envelope.toml \
    --dataset BTCUSDT=data/btc.parquet --dataset ETHUSDT=data/eth.parquet \
    --magnitude 20 --out runs/session-1 --env-file .env     # the same, spelled out
```

```
figures (code) -> screener -> [macro] -> [onchain] -> one replay per kept instrument,
                  the analyst as its core -> risk manager -> executor -> auditor
```

Code measures each dataset first (rows, print rate, realised volatility, turnover, the cost
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
provider = "deepseek"                 # any registered provider; docs/providers.md
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
agent that survived a session can be taken up the ladder like any code core.

### The auditor reads before it answers

The auditor is not handed one brief; it works a bounded tool loop over the read surface:
`run_report`, `ledger_show`, `ledger_search`, `runs_index`, `ladder_show`. Each step is one
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

A role does not start from a blank prompt. Code computes the figures first and the model reads
a brief with the basis of every number in it: measured on the operator's data with a sample
size, or declared by the operator and labelled as such.

| Role | Tools (`agent/<role>/tools.py`) | What the brief carries |
|---|---|---|
| `market_screener` | `screen_figures` | per instrument: rows, print rate, realised volatility, first-lag autocorrelation, the 1:10 variance ratio, largest gap, last-hour volume against the average, turnover, the cost floor for the claimed edge, and a regime the code proposes from a written rule (`[agents.screen]` sets the two thresholds: `min_prints_per_minute`, `volatile_above_bps`) |
| `technical_analyst` | `features` | on each closed bar: returns over 1, 5 and 20 bars, the close as a z-score of the window, the gap to VWAP, the last bar's volume and range against the window's, the position between the window's high and low; then the bars themselves |
| `macro_analyst` | `macro_brief` over `data/series.py` | per series in `series.toml`: the value known at the time and when it became known (publication lag honoured), the state against the operator's declared threshold, the correlation and strongest lead-lag with the instrument's returns measured on the operator's data with n, the calendar events inside the horizon |
| `onchain_analyst` | `onchain_brief` | the same engine, plus the net over a window and the last value as a z-score of its own history |
| `risk_manager` | `risk_brief` | per run: latency from intent to fill (median, max, n, ratio to the declared), net per round trip overall and over the last window against the claimed magnitude, maximum drawdown, which control refused and how often |
| `executor` | `size_for`, `rate_budget`, `export_signals` | a size that never exceeds a cap, rounded down to the venue's step; what the minute still allows; the run's admitted intents as `signals.jsonl` or CSV for execution elsewhere (`tra signals export RUN`) |
| `auditor` | `audit_brief`, then a tool loop over `run_report`, `ledger_show`, `ledger_search`, `runs_index`, `ladder_show` | usage per role with tokens per closed round trip, each promotion with whether its evidence names a gate, and a run against its replay trade by trade; then whatever it chooses to read, up to six calls |

### Series from anywhere

`examples/series.toml` maps external series the way `mapping.toml` maps trades: the file, the
time column and its unit, the value column, the unit, and `publish_lag`, the time between an
observation and the moment it was actually available. A monthly print released two weeks after
month end is unknown for those two weeks, and the brief says so. Thresholds in the map are the
operator's declaration; a correlation is never written in the map, because it is measured on
the operator's data every session and reported with its n and window.

```
tra agent session --macro series.toml --events events.json --onchain onchain.toml
```

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
tra agent eval --model groq:openai/gpt-oss-120b --model deepseek:deepseek-flash     --model ollama:qwen3:8b --env-file .env          # the newest dataset in data/
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

## What a role cannot do

`promote`, `loosen_limits`, `disarm_kill_switch`, `raise_cap`, `place_order` are not actions
on the surface; a role that asks for one is refused by name, every time, and the refusal is
what it is told. Orders are not on the surface either: they leave the runtime through the
controls, the canary caps and the ladder, or they do not leave at all. A model may decide;
code executes and refuses.
