---
title: Quick start
description: Install trading-research-agents, check a desk and run a first cycle.
---

# Quick start

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/BijectiveLabs/trading-research-agents && cd trading-research-agents
uv tool install --editable . --with 'ccxt>=4.5' --with 'openai>=1' --with 'pyarrow>=18'

tra init my-desk --profile autonomous && cd my-desk
cp .env.example .env        # put your model key in here; no venue key is needed yet
tra check                   # what's ready, and the one thing to fix next
tra run                     # one autonomous cycle, in the sandbox: nothing leaves your machine
```

The `--with` extras are what the project talks through: `ccxt` for venue data and orders, `openai`
for every OpenAI-compatible provider (Groq, DeepSeek, xAI, OpenRouter, a local server), `pyarrow`
for datasets. Add `--with 'anthropic>=1'` or `--with 'google-genai>=1'` for those providers.
Without uv, `pip install -e '.[ccxt,openai,data]'` in a virtual environment does the same.

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
ok    conviction: floor 0.4; ladder 0.75+ -> 1.0x, 0.55+ -> 0.5x, 0.4+ -> 0.25x
ok    targets: at least 5 bps net; at least 2.0x the round trip; at most 6 round trips a day
ok    risk per trade: 50 bps of equity at the stop, applied to every size the ladder produces
ok    size: 0.01 in base units, step 0.001
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

`tra keys --probe` proves the key works without sending a prompt. [Model providers](/opensource/providers)
lists every provider, the key each one reads and how well it has been tested.

## Keep it running

`tra run` decides once. `tra run --every 5m` keeps deciding: the first pass runs now, then one every
five minutes on the clock (:00, :05, :10 and so on). The wait after a pass is whatever is left to the
next mark, and the desk tells you the time it ends at, for example `next pass at 14:30:00 UTC, in
10m 57s`. [The cycle](/opensource/cycle) explains why it lands on the clock.

Type `tra` on its own inside the desk and you get the console: the cockpit, then a prompt that takes
every command. [What you see](/opensource/what-you-see) walks through the screens in the order they
appear.

## From a replay to real orders

You can stop at any of these steps. Each is one command.

| Step | Command | What happens | Needs |
|---|---|---|---|
| 1. Replay | `tra data get BTCUSDT 2025-01-01`, then `tra backtest` | a day of the venue's own archive, checksum-verified and audited, replayed net of every cost | nothing |
| 2. Shadow | `tra paper --minutes 30` | the live public feed, decisions recorded, nothing sent | nothing |
| 3. Canary | `tra live` | a small live session behind hard caps and the ladder | a venue key, a promotion with evidence |
| 4. Agents | `tra run` | the autonomous cycle instead of a code core; sandbox until you say otherwise | a model key |
| 5. Venue mode | `tra run` with `[execution].mode = "venue"` | one order per decision, exits attached, positions looked after | a signed `MANDATE.md`, the canary tier, caps, a leverage, venue keys |

Venue mode is the only mode that can lose money, so it has a door in front of it. `tra mandate`
prints the text you have to keep in your project word for word, and `tra check` names every missing
piece. Read [Capital, orders and positions](/opensource/capital) before you open it.

```toml
[execution]
mode = "venue"
leverage = 3                     # the one setting you must choose; everything else has a default
margin_mode = "isolated"         # isolated | cross
hold_limit = "timeout"           # close by time after [exit].timeout_bars; or horizon, or none
```

Who decides what, in one line each: **the analysts propose the direction, your risk rule sets the
size, you choose the leverage, and no model touches the last two.**
