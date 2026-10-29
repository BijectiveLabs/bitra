---
title: Quick start
description: Install TRA, make a desk and run a first cycle.
---

# Quick start

A few minutes and one model key. Every setting has a working default, so a new desk runs before you
change anything; the table under step 6 says what those defaults are.

**1. Install.** You need Python 3.11 or newer. [uv](https://docs.astral.sh/uv/) is the quickest way:

```bash
uv tool install 'bitra[ccxt,openai,anthropic,google,data]'
```

That brings every model provider and every exchange in. Without uv, run
`pip install 'bitra[ccxt,openai,anthropic,google,data]'` inside a virtual
environment. To move to the newest release later:

```bash
uv tool upgrade bitra      # or: pip install -U 'bitra[ccxt,openai,anthropic,google,data]'
tra --version
```

Installed under the old name, `trading-research-agents`? The same package is still published
under it, so `uv tool upgrade trading-research-agents` keeps it current. To move to the new name,
take the old tool out first, because both provide the `tra` command; desks and settings stay as
they are:

```bash
uv tool uninstall trading-research-agents
uv tool install 'bitra[ccxt,openai,anthropic,google,data]'
```

If an upgrade to 0.10.1 left you without `tra` (the old name was published then as a package that
only depended on `bitra`, and `uv` removed the tool), the second line is all you need.

In a virtual environment the same holds: `pip uninstall trading-research-agents`, then
`pip install 'bitra[ccxt,openai,anthropic,google,data]'`. The two names hold the same files, so
removing one after installing the other takes those files with it; if that has happened,
`pip install --force-reinstall bitra` puts them back.

**2. Make a desk.** A desk is a folder holding your settings and everything the agents decide. Name
the provider you have a key for:

```bash
tra init my-desk --profile autonomous --agent groq
cd my-desk
```

| `--agent` | Key in `.env` | Model, when you name none |
|---|---|---|
| `groq` | `GROQ_API_KEY` (free tier) | `openai/gpt-oss-120b` |
| `google` | `GEMINI_API_KEY` | `gemini-flash-latest` |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-opus-5-5` |
| `openai`, `deepseek`, `xai`, `openrouter`, ... | see [Model providers](/opensource/providers) | name one with `--model`; `tra models <provider>` lists them |

Add `--venue okx` (or any ccxt id) to trade somewhere other than Bybit.

**3. Add your key.** Copy the example file and put your key in it. Every command reads the `.env`
next to `strategy.toml` on its own.

```bash
cp .env.example .env        # then set GROQ_API_KEY=... in .env
```

**4. Check.** `tra check` reads every setting against the others and names the one thing to fix
next. A desk that is ready ends with:

```
ok    provider groq: ready
ready to run `tra run`
```

**5. Run.** `tra run` decides once on live market data, in the sandbox: nothing leaves your
machine. `tra run --every 15m` keeps deciding, once per fifteen-minute bar, on the clock. Or type
`tra` on its own for the console, which takes the same commands.

```bash
tra run
tra run --every 15m
```

**6. When you want real orders.** Start on the exchange's demo or testnet API. Make the desk for
that exchange from the start, and the rest is three commands:

```bash
tra init my-desk --profile autonomous --agent groq --venue bitget
cd my-desk                       # put the exchange's keys in .env beside your model key
tra keys --probe --prepare       # reads the account; on a demo or testnet sets one-way mode
tra mandate --sign "Your Name" --venue-mode
tra check
```

`--venue` writes everything that exchange needs into `[execution]`: its demo or testnet API, 3x
isolated leverage, at most 200 per order, how its stop and take profit are placed, and orders sized
at 100 of the quote currency. Change any of it; `tra check` says what each line does. How to make
the key on each exchange is in [Exchange accounts and keys](/opensource/exchange-keys), and
[Toward real orders](#toward-real-orders) below has the whole path.

**What a new desk does before you change anything**

| | Default | Where to change it |
|---|---|---|
| Orders | none: the sandbox writes each decision down and sends nothing | `[execution].mode` |
| What it trades | BTCUSDT on Bybit, as a taker | `[run].instrument`, `[universe]`, `--venue` |
| Timeframe | decides on 15-minute bars, reads 1h and 4h as context | `[run].bar_interval_s`, `context_intervals` |
| Strategy | none fixed: the analyst picks the setup the measured regime supports, or stays flat | `[agents].mechanism` |
| Stop and target | the analyst names them; the stop is held between 0.2% and 3% and never inside one ATR, the target between 1.5x and 4x the stop | `[exit]` |
| Risk per trade | 0.5% of equity lost at the stop, whatever the stop's distance | `[controls].max_risk_per_trade_bps` |
| Bad days | no new entry after a 3% loss on the day or a 10% fall from the peak | `[controls]` |
| Time limit | a position closes after 4 hours | `[exit].timeout_bars` |
| Trades worth taking | the edge at least 2x the round trip, break even at half the trades or better, at most 6 a day | `[targets]` |
| Oversight | the risk gate on every new order; two auditors that report | `[controls]`, `[audit]` |

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
| Your thesis, if you trade one idea | `[agents].mechanism` (`tra init --agent` writes one that lets the regime choose) | the setups you favour; it never overrides the measured regime |
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
| `[run].settlement_interval_s` | `28800` | the funding clock used only when the venue does not report an instrument's own |
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
| 2. Demo or testnet API | `tra run` with `[execution].mode = "venue"` and `environment = "demo"` or `"testnet"` | real orders on the exchange's demo or testnet API, exits attached, positions looked after | a signed mandate, a leverage, `max_notional`, practice keys |
| 3. Real capital | the same, with `environment = "mainnet"` | real orders with real money | all of the above, plus the ladder at its canary tier (`tra ladder promote`, with evidence) |

Venue mode is the only mode that can lose money, so it has a door in front of it. Read the
mandate with `tra mandate`; when you accept it, `tra mandate --sign "Your Name"` keeps it in the
project as `MANDATE.md` and signs `[execution]` with your name and today's date, in one step.
Signing alone does not switch anything on; `--venue-mode` signs and turns orders on together. Read
[Capital, orders and positions](/opensource/capital) before you open it.

```toml
[execution]
mode = "venue"
environment = "demo"             # demo | testnet | mainnet
leverage = 3                     # yours to choose; it never makes a trade bigger
max_notional = 400               # the most one order may be worth, in quote currency
margin_mode = "isolated"         # isolated | cross
hold_limit = "timeout"           # close by time after [exit].timeout_bars; or horizon, or none
```

Who decides what, in one line each: **the analyst proposes the direction and where the trade is
wrong or done, your risk rule sets the size, you choose the leverage, and no model touches the
last two.**

A scripted strategy of your own has a research path before any of this: `tra data get` and
`tra backtest` replay a day of the exchange's archive net of every cost, `tra paper` runs it on
the live feed with nothing sent, and `tra live` is a small canary session behind hard caps.
[The live tier](/opensource/live) walks through it.
