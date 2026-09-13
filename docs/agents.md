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
tra agent session --config strategy.toml --envelope envelope.toml \
    --dataset BTCUSDT=data/btc.parquet --dataset ETHUSDT=data/eth.parquet \
    --magnitude 20 --out runs/session-1 --env-file .env
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

[agents.pricing.deepseek-flash]       # what the operator was quoted; never a constant
input_per_million = 0.14
output_per_million = 0.28
```

The same `[strategy].agent` core runs under `tra backtest`, `tra paper` and `tra live`, so an
agent that survived a session can be taken up the ladder like any code core.

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
