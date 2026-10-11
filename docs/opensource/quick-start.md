---
title: Quick start
description: Install trading-research-agents, check a desk and run a first cycle.
---

# Quick start

You need Python 3.11 or newer. [uv](https://docs.astral.sh/uv/) is the quickest way in.

```bash
uv tool install 'trading-research-agents[ccxt,openai,data]'

tra init my-desk --profile autonomous && cd my-desk
cp .env.example .env        # put your model key in here; no venue key is needed yet
tra check                   # what's ready, and the one thing to fix next
tra run                     # one autonomous cycle, in the sandbox: nothing leaves your machine
```

The extras are what the project talks through: `ccxt` for venue data and orders, `openai` for
every OpenAI-compatible provider (Groq, DeepSeek, xAI, OpenRouter, a local server), `data` for
datasets. Add `anthropic` or `google` to the list for those providers. Without uv,
`pip install 'trading-research-agents[ccxt,openai,data]'` in a virtual environment does the same.

To work on the code itself, install from a clone instead:

```bash
git clone https://github.com/BijectiveLabs/trading-research-agents && cd trading-research-agents
uv tool install --editable '.[ccxt,openai,data]'
```

Trading on another exchange: add `--venue okx` (or any ccxt id) to `tra init`, and the data, the
orders, the practice environment and the keys are wired for that venue. See
[Venues](/opensource/venues).

## What a fresh desk says

A project made by `tra init --profile autonomous` checks out like this before you touch it
(shortened):

```
ok    strategy.toml: valid; core 'agent-pending'; instrument BTCUSDT
ok    roster: technical_analyst; `tra run` asks these
ok    channel: bybit/taker-taker enters as a taker, priced at that fee; an order crosses the book at once
ok    execution: sandbox; nothing leaves the machine
ok    regime: measured on 15m with 1h, 4h as context; trending at 2.0+, ranging at 1.0 or less
ok    conviction: floor 0.4; ladder 0.75+ -> 1.0x, 0.55+ -> 0.5x, 0.4+ -> 0.25x
ok    targets: at least 5 bps net; at least 2.0x the round trip; at most 6 round trips a day
ok    exits: the analyst names its stop and target; the stop is held between 20 and 300 bps, the
      target between 1.5x and 4.0x the stop
ok    risk per trade: 50 bps of equity at the stop, applied to every size the ladder produces
ok    size: 0.01 in base units, at the step 0.001
ok    data.toml: chains for ohlcv, orderbook, funding, open_interest; `tra run` uses them
todo  [agents].provider is still the placeholder; name the provider you have a key for, and its model
1 thing(s) to fix first
```

## Name your model

Open `strategy.toml` and name the provider you hold a key for:

```toml
[agents]
provider = "groq"
model = "openai/gpt-oss-120b"
effort = "medium"
fallback = ["google:gemini-flash-latest"]
```

`tra models groq` lists the models your key can use, read from the vendor, so the name is
copied from there rather than from a web page. `tra keys --probe` proves the key works without
sending a prompt. [Model providers](/opensource/providers)
lists every provider, the key each one reads and how well it has been tested.

## Keep it running

`tra run` decides once. `tra run --every 15m` keeps deciding: the first pass runs now, then one
every fifteen minutes on the clock (:00, :15, :30 and so on), which is the decision timeframe
`tra init --agent` writes. The wait after a pass is whatever is left to the
next mark, and the desk tells you the time it ends at, for example `next pass at 14:30:00 UTC, in
10m 57s`. [The cycle](/opensource/cycle) explains why it lands on the clock.

Type `tra` on its own inside the desk and you get the console: what is ready and what to do next, then a prompt that takes
every command. [What you see](/opensource/what-you-see) walks through the screens in the order they
appear.

## What you decide, and what has a default

The software measures what it can measure, uses the common standard where one exists, and asks you
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

## From a replay to real orders

You can stop at any of these steps. Each is one command.

| Step | Command | What happens | Needs |
|---|---|---|---|
| 1. Replay | `tra data get BTCUSDT 2025-01-01`, then `tra backtest` | a day of the venue's own archive, checksum-verified and audited, replayed net of every cost | nothing |
| 2. Shadow | `tra paper --minutes 30` | the live public feed, decisions recorded, nothing sent | nothing |
| 3. Canary | `tra live` | a small live session behind hard caps and the ladder | a venue key, a promotion with evidence |
| 4. Agents | `tra run` | the autonomous cycle instead of a scripted strategy; sandbox until you say otherwise | a model key |
| 5. Venue mode | `tra run` with `[execution].mode = "venue"` | one order per decision, exits attached, positions looked after | a signed `MANDATE.md`, a leverage, a cap per order, venue keys; on mainnet also the canary tier |

Venue mode is the only mode that can lose money, so it has a door in front of it. Read the
mandate with `tra mandate`; when you accept it, `tra mandate --sign "Your Name"` keeps it in the
project as `MANDATE.md` and signs `[execution]` with your name and today's date, in one step.
Signing does not switch anything on. `tra check` names whatever else is missing. Read [Capital, orders and positions](/opensource/capital) before you open it.

```toml
[execution]
mode = "venue"
leverage = 3                     # the one setting you must choose; everything else has a default
margin_mode = "isolated"         # isolated | cross
hold_limit = "timeout"           # close by time after [exit].timeout_bars; or horizon, or none
```

Who decides what, in one line each: **the analysts propose the direction, your risk rule sets the
size, you choose the leverage, and no model touches the last two.**
