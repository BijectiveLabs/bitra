---
title: Quick start
description: Install trading-research-agents, make a desk and run a first cycle.
---

# Quick start


Six steps. You need Python 3.11 or newer and a key for one model provider
(Groq has a free tier). No exchange account is needed until step 6.

**1. Install.** [uv](https://docs.astral.sh/uv/) is the quickest way:

```bash
uv tool install 'trading-research-agents[ccxt,openai,data]'
```

Without uv, run `pip install 'trading-research-agents[ccxt,openai,data]'` inside a virtual
environment. The extras are `ccxt` for exchange data and orders, `openai` for every
OpenAI-compatible provider (Groq, DeepSeek, xAI, OpenRouter, a local server) and `data` for
datasets. Add `anthropic` or `google` to the list if you use those. To add an extra later, run the
same install again with the longer list; the per-feature pages say which extra they need.

**2. Make a desk.** A desk is a folder holding your settings and everything the agents decide.
Name your provider and model here:

```bash
tra init my-desk --profile autonomous --agent groq --model openai/gpt-oss-120b
cd my-desk
```

Add `--venue okx` (or any ccxt id) to trade somewhere other than Bybit. `tra models groq` lists
the models your key can use, and [Model providers](/opensource/providers) lists every provider
and the key each one reads.

**3. Add your key.** Copy the example file and put your key in it. Every command reads the `.env`
next to `strategy.toml` on its own.

```bash
cp .env.example .env        # then set GROQ_API_KEY=... in .env
```

**4. Check.** `tra check` reads every setting against the others and names the one thing to fix
next. A desk that is ready ends like this (shortened):

```
ok    execution: sandbox; nothing leaves the machine
ok    exits: the analyst names its stop and target; the stop is held between 20 and 300 bps
ok    risk per trade: 50 bps of equity at the stop, applied to every size the ladder produces
ok    provider groq: ready
ready to run `tra run`
```

**5. Run.** `tra run` decides once on live market data, in the sandbox: nothing leaves your
machine. `tra run --every 15m` keeps deciding, once per fifteen-minute bar, on the clock (:00,
:15, :30, :45). Or type `tra` on its own for the console, which takes the same commands.

```bash
tra run
tra run --every 15m
```

**6. When you want real orders.** Start on the exchange's practice account: put its keys in
`.env`, read the mandate with `tra mandate`, sign it with `tra mandate --sign "Your Name"`, and
in `[execution]` of `strategy.toml` set `mode = "venue"`, a `leverage` and `max_notional` (the
most one order may be worth). `tra check` lists whatever is still missing. [Toward real orders](#toward-real-orders) below has the whole path.

Working on the code itself? [Contributing](/opensource/contributing) has the setup from a clone.

## What you decide, and what has a default

The system measures what it can measure, uses the common standard where one exists, and asks you
only for what nobody else can decide. `tra check` says, in plain words, what every setting below
will do in your project, defaults included.

**You decide:**

| What | Where | Why it is yours |
|---|---|---|
| What to trade | `[run].instrument`, `[run].venue_key` | |
| How much per trade | `[run].size` or `[run].size_quote` | your money |
| Which model | `[agents].provider`, `[agents].model` (`tra init --agent` writes both) | your account and your bill |
| What the analyst reasons from | `[agents].mechanism` | one sentence, your thesis |
| Leverage | `[execution].leverage` | only you know what you can carry |
| The most one order may be worth | `[execution].max_notional` | the cap on a defect's cost |
| That you take the losses on | `MANDATE.md`, `acknowledged_by`, `acknowledged_on` | venue mode only |

**Already set, change any of it:**

| Setting | Default | What it does |
|---|---|---|
| `[controls].max_risk_per_trade_bps` | `50` | the most one trade may lose at its stop, in bps of equity |
| `[controls].daily_loss_limit_bps` | `300` | no new entry once equity is down this far on the UTC day |
| `[controls].max_orders_per_minute` | `6` | across the whole desk |
| `[controls].max_position` | none | a cap in units; the risk per trade already bounds every size |
| `[controls].max_drawdown_bps` | none (`tra init` writes `1000`) | no new entry once equity is this far below its peak |
| `[exit]` | the analyst's stop and target, held inside bounds | see [where the stop and take profit sit](/opensource/cycle#where-the-stop-and-the-take-profit-sit) |
| `[targets].max_break_even_win_rate` | `0.5` | refuse a trade that would need more than half its kind to win to cover the stops and the costs |
| `[exit].timeout_bars` | four hours of bars: `240` at 1m, `16` at 15m | a position closes by time after this many bars; a measured stop is sized over the same span |
| `[run].bar_interval_s` | `60`; `tra init --agent` writes `900` with 1h and 4h as context | the decision timeframe; the round trip costs the same on any of them, so a longer one leaves more of the move |
| `[run].settlement_interval_s` | `28800` | the venue's funding clock |
| `[execution].environment` | `demo` | where orders go: `demo`, `testnet` or `mainnet` |

A scripted strategy (`[strategy].file`) is research, so it declares its own exit and cost figures:
`[exit].stop_frac`, `timeout_bars` and `mechanism_bound`, and `[run].spread_cost_bps`, `latency_ms`
and `slippage_bps`. A default there would be a cost the research never measured.

What stays strict, and why: a setting nobody recognises is refused rather than ignored, because a
typo in a risk setting should stop the desk at `tra check`, not at the venue; and the few things
above that are yours are asked for, never guessed.

## Toward real orders

An agent desk goes through three steps, and you can stay at any of them as long as you like:

| Step | How | What happens | Needs |
|---|---|---|---|
| 1. Sandbox | `tra run` | the full cycle on live data; every decision written down, no order sent | a model key |
| 2. Practice account | `tra run` with `[execution].mode = "venue"` and `environment = "demo"` or `"testnet"` | real orders on the exchange's practice account, exits attached, positions looked after | a signed mandate, a leverage, `max_notional`, practice keys |
| 3. Real capital | the same, with `environment = "mainnet"` | real orders with real money | all of the above, plus the ladder at its canary tier (`tra ladder promote`, with evidence) |

Venue mode is the only mode that can lose money, so it has a door in front of it. Read the
mandate with `tra mandate`; when you accept it, `tra mandate --sign "Your Name"` keeps it in the
project as `MANDATE.md` and signs `[execution]` with your name and today's date, in one step.
Signing does not switch anything on. Read [Capital, orders and positions](/opensource/capital)
before you open it.

```toml
[execution]
mode = "venue"
environment = "demo"             # demo | testnet | mainnet
leverage = 3                     # yours to choose; it never makes a trade bigger
max_notional = 400               # the most one order may be worth, in quote currency
margin_mode = "isolated"         # isolated | cross
hold_limit = "timeout"           # close by time after [exit].timeout_bars; or horizon, or none
```

Who decides what, in one line each: **the analysts propose the direction and where the trade is
wrong or done, your risk rule sets the size, you choose the leverage, and no model touches the
last two.**

A scripted strategy of your own has a research path before any of this: `tra data get` and
`tra backtest` replay a day of the exchange's archive net of every cost, `tra paper` runs it on
the live feed with nothing sent, and `tra live` is a small canary session behind hard caps.
[The live tier](/opensource/live) walks through it.
