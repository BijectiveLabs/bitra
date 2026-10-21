---
description: "How a desk's own finished trades lower its risk per trade: the Kelly fraction from the record, a seeded Monte Carlo of the drawdown ahead, and the excursion of every trade, all computed by code and repeatable to the digit."
---

# Sizing from the record

Every trade here has a stop and a target, and every finished trade is written up from the venue's own
fills. That makes two things possible that a trading agent usually cannot do honestly: measure what
the desk's trades actually return, in the same units every time, and let that measurement lower the
risk the next trade takes. No model is involved. The arithmetic below is all of it, and `tra sizing`
prints it for your desk.

It is off until you turn it on, and once on it can only lower the risk you declared in
`[controls].max_risk_per_trade_bps`, never raise it.

```toml
[sizing]
kelly_fraction = 0.25            # a quarter of the Kelly fraction; turning this on is what enables it
# min_round_trips = 30           # finished trades before the record decides anything
# lookback_round_trips = 100     # only the newest trades count
# simulate_trades = 100          # how far ahead the drawdown is simulated, 10 to 300
# simulate_paths = 2000          # 100 to 5000, so a cycle waits seconds at most
# seed = 20261021                # the same seed gives the same figures on any machine
# max_drawdown_probability = 0.05
```

## The unit: what the trade planned to lose

When a cycle writes an intent, it also writes `loss_at_stop_bps`: the distance from the price to the
stop plus the round trip it pays, in basis points of the notional. That is what the trade loses if the
stop is hit as priced. When the trade finishes, its net result is divided by that figure and written
on the round trip as `r`. A stop hit as priced is about -1. A target twice as far as the stop is
about +2 less the cost. A time exit lands wherever it lands.

`r` is price and fees, like `net`; funding is recorded beside it as the venue reports it, because
venues sign it differently.

Reading results in planned losses makes trades of different sizes, instruments and stop distances
comparable, and it is the unit the risk limit is already in: risking 50 bps at the stop and getting
`r = -1` costs the account 50 bps.

## Kelly from the record

From the newest `lookback_round_trips` trades with an `r`, three figures: the share that won (p), the
share that lost (q), and the average win and loss in planned losses (W and L). A trade that closed
flat counts in neither. The fraction of equity to lose on a losing trade that maximises long-run
growth is then:

```
f* = (p * W - q * L) / ((p + q) * W * L)
```

With every trade a win or a loss this is the familiar `p / L - q / W`. As arithmetic only: half the
trades winning 2 and half losing 1 gives `f* = (0.5 * 2 - 0.5 * 1) / 2 = 0.25`, a quarter of equity
at the stop. Nobody should bet that, and the record does not ask you to:

- **Only `kelly_fraction` of it is applied.** p, W and L are estimates from a few dozen trades, and full
  Kelly on an overstated edge loses accounts. A quarter is a common choice; 0.25 of 0.25 is 625 bps.
- **It is a ceiling.** The risk a cycle uses is the smaller of this and your declared
  `max_risk_per_trade_bps`. With 50 bps declared, the ceiling above changes nothing.
- **Too few trades decide nothing.** Below `min_round_trips` the declared risk stands, and the cycle
  records how many trades the record holds.
- **No edge refuses.** With enough trades, an f* at or below zero means the record supports no bet,
  and the cycle refuses new entries with the figures behind it. The same when none of the trades won.
  A person decides what happens next; lowering or removing `[sizing]` is a configuration change.

Beside f*, the record also gives the win rate it supports at 95% confidence (the lower Wilson bound),
so you can see how much of the win rate is the sample.

## The drawdown ahead: a seeded Monte Carlo

Kelly is about the average. The path matters too, and `[controls].max_drawdown_bps` is where the desk
stops taking entries. So the cycle also asks: at this risk per trade, how likely is the desk to fall
through the room it has left under that limit in the next `simulate_trades` trades?

It answers by resampling. It draws `simulate_trades` results, with replacement, from the same trades
the Kelly figures came from, compounds them at the risk being tested, and repeats that
`simulate_paths` times. The share of paths whose drawdown reaches the room left is the chance it
reports. If the Kelly ceiling keeps that chance within `max_drawdown_probability`, it stands;
otherwise the risk is lowered, by bisection to a hundredth of a basis point, to the largest value that
does. The room is your limit minus the drawdown the account already has from its highest equity,
read at the start of the cycle, in whole basis points rounded down.

It is repeatable to the digit:

- the draws come from a generator seeded with `seed`, and the same draws are reused at every risk
  level tried;
- the draws are integers taken from the generator's own numbers, so nothing depends on how a Python
  version implements random choice;
- percentiles are nearest-rank in integer arithmetic.

The same record, settings and seed give the same answer on any machine, and the cycle writes the
seed, the paths, the risk and the figures into its record, so anyone can run it again. Each answer
is also kept in memory, so a desk asking the same question every cycle does not simulate it again.

What it is not: a forecast. Resampling assumes the next trades look like the last ones. It is a
check on how hard the recent record could push the account at a given risk, which is the question a
drawdown limit is there to answer.

## Excursions, measured by the system

Every round trip that closes in venue mode now also carries how far it went for and against the
position before it closed, read from the venue's own candles over the hold. The finest candles that
cover the hold in at most 1000 bars are used, one minute for most holds.

| Field | What it is |
|---|---|
| `mfe_bps` | the best the position stood at, in bps of the entry, positive when it was in front |
| `mae_bps` | the worst, negative when it was under water |
| `bars_to_mfe`, `bars_to_mae` | on which bar each happened |
| `mae_of_stop` | the worst as a share of the stop distance: 0.9 is a stop that nearly fired |
| `mfe_of_take` | the best as a share of the target distance: over 1 is a target that was reached |
| `timeframe`, `complete` | the candles read, and whether they covered the whole hold |

The first and the last bar can include prices from just before the entry and just after the exit,
which is why the finest candles are used. The quant auditor and the risk manager read these figures;
they no longer have to infer the shape of a trade from its result.

## Where it shows

| Where | What |
|---|---|
| the cycle record, `sizing` in `cycle.json` | the rule, the measured figures, f*, the ceiling, the simulation and why |
| `intents.jsonl` | `loss_at_stop_bps` on every intent |
| `round_trips.jsonl` | `loss_at_stop_bps`, `r`, `stop`, `take` and `excursion` on every finished trade |
| `tra sizing` | the same calculation the cycle makes, from the records, without changing anything; and the fills against their prices: the median entry slippage against the touch, and the median fees against what the gate counted |

Trades written before this version have no `loss_at_stop_bps`, so they are not counted: the record
starts with the first trade that knows what it planned to lose.
