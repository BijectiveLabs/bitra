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

Each role is a package under `trading_research_agent.agent`: a JSON schema the model must
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
floor for the claimed magnitude) and the screener sees only those figures. It keeps or
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
token_budget = 200000                 # for the whole session, every role
max_calls_per_hour = 60
min_interval_s = 60
lookback_bars = 60
min_confidence = 0.6
magnitude_bps = 20                    # the claimed edge, for the cost floor

[agents.pricing.deepseek-flash]       # what the operator was quoted; never a constant
input_per_million = 0.14
output_per_million = 0.28
```

The same `[strategy].agent` core runs under `tra backtest`, `tra paper` and `tra live`, so an
agent that survived a session can be taken up the ladder like any code core.

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
| `auditor` | `audit_brief` | usage per role with tokens per closed round trip, each promotion with whether its evidence names a gate, and a run against its replay trade by trade |

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
that would be decided on stale prints. The auditor reports tokens per closed round trip, which
is the figure that says whether an agent is worth running on that instrument at all.

## What a role cannot do

`promote`, `loosen_limits`, `disarm_kill_switch`, `raise_cap`, `place_order` are not actions
on the surface; a role that asks for one is refused by name, every time, and the refusal is
what it is told. Orders are not on the surface either: they leave the runtime through the
controls, the canary caps and the ladder, or they do not leave at all. A model may decide;
code executes and refuses.
