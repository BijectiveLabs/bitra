---
description: "Execution realism: the cost identity, fees, spread, slippage and funding, and the measured assumptions that separate a backtest from a live result."
---

# Execution realism

Backtests do not survive contact with the market, and that sentence is useless as guidance
because it names no mechanism. The gap is a stack of specific, individually measurable
assumption failures. `trading_research_agents.execution` carries the ones the research this
package descends from actually paid for, each as a function with the envelope as an argument.

Every figure here is a floor that omits market impact. Nothing this was measured on ran at a
size where its own transacting moved the book.

## The cost identity

```
c_rt = 2 * fee + spread_cost + slippage + funding_accrual
```

```python
from trading_research_agents.execution import Direction, FundingSchedule, realised_cost

breakdown = realised_cost(
    channel=envelope.cheapest("bybit"),
    direction=Direction.LONG,
    entry_ts_ns=...,
    exit_ts_ns=...,
    spread_cost_bps=Decimal("3.0"),  # half-spread from the recorded book at the decision
    slippage_bps=walk.slippage_bps,  # from walking the recorded depth at the intended size
    funding=FundingSchedule(interval_s=8 * 3600, rates={...}),
)
breakdown.round_trip_bps  # the sum, and the four terms beside it
```

- One function owns the identity, every term is an explicit argument, and the terms come
  back separately. Four terms living in four places is how an absent term stays invisible; a
  whole programme once measured returns mid to mid, and its expectation changed sign once the
  costs were counted.
- Fees come from the envelope's declared channel, never from a literal. A hedged pair charges
  `fee * (1 + beta)` per touch: at a hedge ratio of 1.4 the pair transacts 2.4 legs, not 2.
- Funding is signed. A long through a positive-funding settlement pays; a short receives. A
  position that spans no settlement owes zero, and that zero is real. A settlement the position
  did span with no rate on record is refused, because that zero would be a defaulted field.
- The quoted schedule is a floor. Measured end to end, taker-to-taker on the venue this was
  built on cost 15.02 bps against a quoted 11.0.

## Walk the book

```python
from trading_research_agents.execution import Level, Side, Snapshot, walk_the_book

result = walk_the_book(asks, size=Decimal("2"), side=Side.BUY, mid=mid)
result.vwap, result.slippage_bps, result.levels_consumed
```

Consumes recorded depth level by level and reports the VWAP against mid in basis points, signed
so that paying more than mid is a positive cost on either side. When the depth cannot absorb
the size it refuses. It does not extrapolate the last level and it does not price a partial
fill as complete; that lie would be most attractive on exactly the thin instruments where a
naive backtest looks best. `Snapshot.walk` adds a staleness tolerance: a book recorded at an
event and read seconds later is not the book that was there.

The spread is normalised to mid, never to bid; a locked book is zero and a crossed book is
refused.

## Passive fills

```python
from trading_research_agents.execution import FillMode, Print, passive_fill, entry_after_latency

fill_ts = passive_fill(
    prints, limit=..., side=Side.BUY, t0_ns=..., budget_ns=..., mode=FillMode.THROUGH
)
first = entry_after_latency(prints, signal_ts_ns=..., latency_ns=...)
```

A buy rests a bid and fills on a print at or below it. A print that sweeps through the level
without printing at it is a fill: an earlier matcher that counted only discrete events at the
level missed 55.7 percent of adverse fills at one instrument, and every missed fill was
adverse. Queue position is not recorded, so two bounds are reported: `AT_OR_THROUGH` (a print at
the limit counts; upper bound on the fill rate) and `THROUGH` (only prints past it; lower bound).

Entries fill at the first print at or after the signal plus the measured latency, not at the
tick that fired the signal.

## Effective cost

```
c_eff = p_fill * c_passive + (1 - p_fill) * (c_cross + drift_unfilled)
```

The orders that fail to fill are not a random subset. They are the names moving away, which is
why they did not fill, and their drift compounds while the order rests: the longer the budget, the
further the unfilled names had moved away. A cost model that treats an unfilled order as costless
gets the wait-budget decision backwards.
`effective_cost` refuses if the drift on any unfilled order cannot be computed, because dropping
it would remove exactly the observations that make the number informative.

## Markout

```
markout(h) = side * (mid_{t+h} - fill_price_t) / fill_price_t * 1e4
```

Negative is adverse. It is not a cost term: every other cost is indifferent to how good your
price is, while this one grows with how attractive your quote is. `markout_table` reports each
horizon beside the half-spread being earned and whether the maker survives it; on the control
instrument the adverse move at sixty seconds ran to about four times the spread captured.
