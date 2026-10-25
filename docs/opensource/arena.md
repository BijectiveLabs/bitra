---
description: "tra arena: policies replayed over the market archive, every idea settled by the desk's own costs, beside baselines built to be beaten."
---

# The arena

`tra arena` replays every row over the same archive and settles every idea the same way, so a
policy is judged beside rules that cost nothing and beside chance, net of what the desk pays.

## One command

```bash
tra data bars BTCUSDT 2025-07-01 --to 2025-09-30 --venue bybit
tra data bars ETHUSDT 2025-07-01 --to 2025-09-30 --venue bybit
tra arena --instruments BTCUSDT,ETHUSDT --from 2025-07-01 --to 2025-09-30 \
  --spread-bps 1 --slippage-bps 0
```

Fees come from the venue's `taker-taker` channel in `envelope.toml` (`--channel` names another).
Spread and slippage are yours to declare: the arena refuses to run without them rather than assume
a number for you. The same settings can live in an `arena.toml`:

```toml
[arena]
instruments = ["BTCUSDT", "ETHUSDT"]
from = 2025-07-01
to = 2025-09-30
spread_bps = 1
slippage_bps = 0
```

```bash
tra arena --config arena.toml
```

## How a row is replayed

- A decision is taken at every close of the decision interval (one hour by default), with the
  four-hour view beside it and BTCUSDT as context.
- A view holds only bars that had closed and derivatives values published by then. A bar from
  after the decision is refused, and the run fails instead of reporting.
- An idea enters at the open of the first traded minute at or after its decision, never at the
  price it was decided on.
- Each row holds at most one idea per market at a time.
- Ideas are settled on one-minute bars: take, stop or time limit, whichever comes first. A minute
  that reaches both levels counts as the stop; a level is filled at the level.
- A minute nobody saw inside the hold, or a funding settlement the archive has no rate for,
  leaves the idea unsettled. The report counts every unsettled idea by its reason.

## Costs

| Component | Where it comes from | Certainty |
|---|---|---|
| Fees | the channel in `envelope.toml`, per side | declared |
| Spread | `--spread-bps`, the full quoted spread per round trip | declared |
| Slippage | `--slippage-bps`, per round trip | declared |
| Funding | the archive's settlements over the hold, by side | measured |

`--funding none` is for a market that pays no funding, such as spot. Every idea in `ideas.jsonl`
carries each component and its certainty.

## Rows

| Row | Definition |
|---|---|
| `buy_hold` | long at the window's first decision, held to its end |
| `momentum` | long when `mom_z_24` is at least 1, short when it is at most -1; stop 1.5 ATR, take 3 ATR, held 24 bars |
| `fade` | the opposite side of `momentum`, on the same triggers and exits |
| `random` | a seeded side at the times `momentum` fires, with its exits: the null every row is compared with |
| `v1_record` | the decisions the desk sent, from `runs/cycles`, held for `--v1-hold`; a live record, not a replay |

The baselines are fixed and versioned, not tuned; changing one would change every comparison, so
the version is written into every result. A package adds its own row by registering a callable
that returns a policy under the `trading_research_agents.policies` entry point group.

## Reading the table

For every row: ideas, independent bets (ideas on a market that do not overlap an earlier one), the
mean and median net in basis points, the hit rate, the mean R (net over the stop distance plus
cost), the sum, and the Sharpe of daily net (365 days). The last columns compare the row's mean net
with `random`'s: the difference and its 95 % interval, resampling whole days 4,000 times with seed
7, because one day's tape moves every idea on it together. Then the verdict:

| Verdict | Meaning |
|---|---|
| beats random | the whole interval lies above zero |
| worse than random | the whole interval lies below zero |
| no clear difference from random | the interval crosses zero |
| sample too small | fewer than 30 independent bets or fewer than 10 days |
| contaminated | the policy's knowledge cutoff falls after the window starts |

A policy that exposes a score also gets its per-date rank IC against the next bar's return, with a
t statistic. Coverage closes the report: minutes per market, the minutes not seen, and every
feature that was absent, with the reason.

## What a run writes

```text
runs/arena/<id>/arena.json    configuration, archive hashes, package version, rows, coverage
runs/arena/<id>/ideas.jsonl   every idea, its settlement and each cost component
runs/arena/<id>/run.json      when it ran
```

`<id>` is a hash of the configuration. The same archive, configuration and seed give the same
`arena.json`, byte for byte; the time lives in `run.json` only. `--fit-days` and `--test-days` add
walk-forward test windows, each after an embargo of the longest hold, to the report.

Measured on BTCUSDT and ETHUSDT, Bybit, 2025-07-05 to 2025-07-13, on one machine: the run took
4 seconds.
