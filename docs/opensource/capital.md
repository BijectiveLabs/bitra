---
description: "What venue mode does with your money: the account reads, the checks before an order, position sizing, leverage and what happens after a fill, all through ccxt."
---

# Capital, orders and positions

This is what venue mode does with your money, step by step: what is read from the account, what
is checked before an order goes out, and what happens to a position after it fills. All of it is
done by the system. No model reads an account, chooses a leverage, sizes a trade or closes a position.

Everything here goes through ccxt's unified calls, so it behaves the same on any venue that answers
them. A venue that cannot answer one gets a named refusal or a note in the record, never a guess.

## Who decides what

| Decision | Who | How |
|---|---|---|
| direction, confidence, horizon | the technical analyst | inside its schema, from figures the system computed |
| how much behind the idea | the conviction rule | your weights and ladder over every voice ([The cycle](/opensource/cycle)) |
| the most it may lose at the stop | your risk rule | `[controls].max_risk_per_trade_bps` against equity |
| leverage and margin mode | you | `[execution].leverage`, `[execution].margin_mode` |
| when a position is closed by time | you | `[execution].hold_limit` |
| whether the account can carry it | the system | the checks below |

On a spot market none of the leverage, margin or liquidation settings apply: the order is paid in
full, and must fit in `max_margin_share` of the free quote balance with its fee.

Leverage never makes a trade bigger. The size comes from the risk at the stop. Leverage only
decides how much margin that size ties up and how far away liquidation sits.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `[execution].leverage` | none: required in venue mode | applied at the venue before the first order on a symbol, then read back |
| `[execution].margin_mode` | `isolated` | `isolated` or `cross`; isolated keeps one position from drawing on the rest of the account |
| `[execution].max_margin_share` | `0.5` | the most of the free margin one order may use |
| `[execution].liquidation_buffer_bps` | `50` | how far beyond the stop the estimated liquidation must sit |
| `[execution].max_slippage_bps` | `20` | how far through the touch a taker entry may fill |
| `[execution].max_drift_bps` | none | refuse when the mid moved this far against the trade since it was priced |
| `[execution].max_decision_age_s` | `120` | refuse a decision older than this, counted from the start of its cycle to the moment of sending |
| `[execution].hold_limit` | `timeout` | `timeout`, `horizon` or `none`: when time closes a position |
| `[execution].equity_session` | `regular` | `regular`: an entry on an equity underlying only while the US regular session trades, and no hold past its close; `any`: every hour ([Venues](/opensource/venues#stocks-on-a-crypto-venue)) |
| `[execution].equity_closed` | none | exchange holidays, as dates; the package carries no calendar |
| `[execution].equities` | none | instruments to treat as equities on a venue that does not mark them |
| `[execution].markets` | `["linear perpetual"]` | the market types this desk may send to; `linear perpetual`, `spot` and `prediction` are built ([Venues](/opensource/venues#where-an-order-goes-and-what-an-instrument-is)) |
| `[execution].max_notional` | none: required | the most one order may be worth |
| `[run].equity` | none | the most equity you allow this desk; sizing uses the smaller of this and the account |
| `[controls].daily_loss_limit_bps` | `300` | no new entry once equity has fallen this far on the day |
| `[controls].max_drawdown_bps` | none; `tra init` writes `1000` | no new entry once equity has fallen this far from its highest reading, across days |
| `[controls].on_drawdown` | `stop` | `flatten` also arms the kill switch, which flattens every position and stops the run |
| `[controls].max_orders_per_minute` | `6` | counted across every order the desk sends |

`tra check` prints what each of these will do in your project, in plain words, before anything
runs. `tra venues NAME --setup` writes the leverage lines for a new venue.

## Before an order: the account

At the start of every cycle in venue mode, before any model is asked:

- **Equity** is the account's wallet balance in the settlement currency plus the unrealised result
  of its open positions.
- **Sizing equity** is the smaller of that and `[run].equity`. The declared figure is what you
  allow this desk; the account figure is what is actually there. Sizing against the larger would size
  against money that is either not allowed or not present.
- **Daily loss.** The first equity reading of each UTC day is written to `equity.jsonl`. Every later
  reading is measured against it, and once the drop reaches `daily_loss_limit_bps` the cycle stops
  before any model is asked. Open positions keep their exits. Deposits and withdrawals move equity
  too, so they count as gains and losses here.

- **Drawdown.** The highest equity reading is kept in `equity_peak.json` and only ever rises.
  Once equity sits `max_drawdown_bps` below it, the cycle stops before any model is asked, every
  cycle after it too, across days. Open positions keep their exits. To start again, look at what
  happened, then delete `runs/cycles/equity_peak.json`: the next reading is the new peak. With
  `on_drawdown = "flatten"` the cycle also writes the kill switch file, and the next pass flattens
  every position and stops the run. Stop is the default because flattening in a fast market
  locks in the worst price of the move; flatten is there for a desk that would rather be out.

The daily loss limit and the drawdown limit each wake the quant auditor once per UTC day.

The figures are in `cycle.json` under `equity`: declared, venue, free, used, the day's loss, and the
drawdown with its peak.

## When it is sent: the order

The venue hand runs these checks in this order, and the first one that fails refuses the order
with a sentence naming the figure:

1. the intent is about this hand's instrument, and this hand has not already sent its one order;
2. the size is positive, and the notional is inside `max_notional`;
3. this machine's clock is within a second of the venue's;
4. the account holds no position on the symbol (unless `adopt_open_position`), and no order is
   working on it;
5. the size is on the venue's step and above its minimum;
6. the decision is younger than `max_decision_age_s`, and the mid has not drifted against the trade
   past `max_drift_bps`, when that is declared: the book is read again just before sending;
7. **leverage and margin mode** are set at the venue and read back. A venue that answers "not
   modified" is fine, because success is judged by reading the setting back. A venue holding any
   other value refuses the order;
8. **margin**: `notional / leverage + notional * taker fee` must fit inside
   `max_margin_share * free margin`;
9. **liquidation**: the isolated estimate `entry * (1 - (1/leverage - maintenance))` for a long,
   `entry * (1 + (1/leverage - maintenance))` for a short, must sit beyond the stop by
   `liquidation_buffer_bps` of the stop price. The maintenance rate is the venue's first tier, or
   a deliberately high 1% when the venue will not say. The isolated figure is the conservative one:
   a cross position draws on the whole account and is liquidated later;
10. the desk's order rate is inside `max_orders_per_minute`;
11. the stop and take profit can be attached to the entry.

Then the order goes out. A taker entry is a **limit IOC** at most `max_slippage_bps` through the
touch, so a thin book cannot fill it at any price it likes; what it filled is read back from the
venue and recorded. A maker entry rests post-only at the touch as the book stands at send time.

What the account looked like is in `placed.json` under `capital`: margin needed, free margin,
equity, leverage, margin mode and the liquidation estimate.

Two examples of a refusal, as the record writes them:

```
this order needs 333.88 USDT of margin at 3x and 0.5 of the free 500.00 is 250.00; nothing was sent
at 80x liquidation is estimated at 99250.00, inside the stop at 99000 plus 50 bps; a stop past
liquidation is not a stop, so lower [execution].leverage
```

## While it is open: the position

At the start of every pass of `tra run` in venue mode, before the market is looked at, each
instrument the desk trades is read from the venue and matched to the newest order this desk sent
that the venue filled. Then exactly one of these happens:

| What the venue shows | What happens | Recorded as |
|---|---|---|
| the kill switch file exists | every position is closed with a reduce-only market order, every working order is cancelled, and the run stops | `flattened` |
| a position older than its hold limit | it is closed with a reduce-only market order | `closed_by_time` |
| no position, though our record says one was open | the venue's stop or take profit closed it; the result is written up | `closed_by_venue` |
| a position inside its hold limit | nothing; new entries on it wait for its exits | - |

Hold limits:

| `hold_limit` | A position is closed after |
|---|---|
| `timeout` | `[exit].timeout_bars` bars |
| `horizon` | the analyst's own horizon, capped by `[exit].timeout_bars` |
| `none` | never by time; only its stop or take profit closes it |

Exits are never price-limited: a close that fails is worse than one that slips. Reduce-only means
the order can only shrink the position, never open or flip one.

A position this desk did not open - one placed by hand at the venue, say - has no order of ours to
match, so the keeper leaves it alone; the venue hand still refuses to add to it.

An entry the venue never confirmed as filled is looked after while the venue shows a position for
it, so its risk stays on the book and its hold limit applies. It is never written up as a finished
trade on its own word.

## The records

| File | One line per | Carries |
|---|---|---|
| `runs/cycles/positions.jsonl` | close, flatten, or venue exit | instrument, event, the entry's order id and cycle, the close order's id, the reason |
| `runs/cycles/round_trips.jsonl` | finished trade | entry, exit, size, gross, fees, net, funding, how it closed |
| `runs/cycles/equity.jsonl` | UTC day | the first equity reading of the day |
| `runs/cycles/<id>/placed.json` | order | what was sent, what filled, and the account figures at the time |

A round trip is built from the venue's own fills: this position's entry side before its first
exit, and exits up to its size. A later entry on the same instrument belongs to the next position.
Fees are what the venue charged. Funding is reported as the venue signs it and kept out of `net`,
which is price and fees, because venues differ on the sign. Exit fills can take a few seconds to
appear after a close; the keeper waits a little, and a trip written before its exit shows up is
finished on a later pass, as one more line and never a copy.

The quant auditor reads `round_trips.jsonl`. It is the only place a desk's results are measured
from what actually filled rather than from what was intended.

## Limits worth knowing

- **The keeper acts only while a desk runs.** Time exits and the kill switch happen at the start
  of each pass of `tra run --every`. With no desk process, a position has its venue-side stop and
  take profit and nothing else. A desk meant to run unattended belongs under a supervisor.
- **One desk per project.** In venue mode `tra run` takes `runs/cycles/desk.lock` and refuses to
  start while another live process holds it. Two desks sending from the same project would read
  the same account in the same second and could both decide an instrument is free. A lock left by a
  process that died is taken over. Two different projects pointed at the same account are not
  covered by this lock; give each desk its own account or sub-account.
- **Margin mode can be account-wide.** On some venues setting it changes every symbol on the
  account, not only the one being traded. Choose it with the whole account in mind.
- **One desk, one venue.** Netting across venues, transfers between accounts and a book managed
  across a portfolio belong to a platform, not to this package ([Boundaries](/opensource/boundaries)).

## What each order records about its timing

Every sent order writes three figures into `placed.json`, measured live rather than declared:
`decision_age_s` (from the start of the cycle to the moment of sending), `send_ms` (how long the
venue took to answer the order), and `slippage_bps` (the average fill against the mid the trade was
priced at; positive means it paid more). The cycle output prints them on a `timing:` line. The
latency figures in `envelope.toml` and `[run]` are declarations used to price a trade before it
exists; these are what actually happened.
