# The falsification chain

A research programme fails in one of two ways. It commits capital to an idea that was never
real, or it spends months proving something is not real when a cheaper test would have said so
in an afternoon. The chain orders the ways an idea can die by what each test costs, and it
forbids skipping forward.

| Layer | Test | Dies here when | Cost | Status |
|---|---|---|---|---|
| 0 | Cost floor | plausible magnitude below the cheapest round trip | minutes | implemented |
| 1 | Market observation | the phenomenon does not recur | hours | planned |
| 2 | Mechanism memo | nobody can be named who is structurally forced to lose | minutes | 0.2 (language model writes it, code checks the form) |
| 3 | Pre-registration | the claim cannot be stated so that data could refute it | hours | implemented |
| 4 | Data requirements | no available instrument can measure the premise | hours to days | implemented |
| 5 | Statistical validation | interval spans zero, loses to a matched null, or lives in one subperiod | days | implemented |
| 6 | Strategy design | the exit is not bound to the mechanism | weeks | 0.4 |
| 7 | Shadow | net expectation negative in the live market, zero orders placed | weeks of calendar | 0.4 |
| 8 | Live capital | | capital | out of scope |

## Layer 0: the cost floor

Two numbers, before anything else: the plausible magnitude of the signal and the round trip of
the cheapest channel in the operating envelope. If the first is smaller than the second, stop.

```python
from decimal import Decimal
from trading_research_agent.chain import cost_floor
from trading_research_agent.envelope import load_envelope

envelope = load_envelope("examples/envelope.toml")
check = cost_floor(envelope, Decimal("3.0"), venue="hyperliquid")
```

- The magnitude is a bound, not a measurement. A comparable measurement already on record, the
  dispersion of the quantity the signal keys on, or the scale of the mechanism itself all give
  one. Where no bound can be given, the hypothesis is not yet specific enough to test, and the
  function refuses rather than guesses.
- A stop becomes a `CLOSED` row on the cost axis, `ENVELOPE-BOUND`, with the reversal point
  `c_rt < <magnitude> bps`. A pass records nothing; clearing the cheapest test is not a verdict.
- The decision is made against the quoted schedule, which is the declared floor. Where the
  envelope also carries a measured round trip, the check reports whether the signal clears that
  too. Quoted cost is optimistic by roughly a third on the venues this was measured on.

## Layer 3: the pre-registration

Nine elements, all required, sealed by a content hash before any data is seen.

| Element | Requirement |
|---|---|
| Mechanism | One sentence naming who is structurally forced to lose, and why |
| Claim | The falsifiable statement, in a form data could refute |
| Metric | The exact quantity to be computed |
| Horizon | Fixed. No horizon shopping afterwards |
| Universe | The instrument set, and the rule that produced it |
| Era | The research range, and the held-out eras that remain sealed |
| Cost model | All four terms of the round trip (`fee`, `spread_cost`, `slippage`, `funding_accrual`), each with its source |
| Power | The sample size required to detect the claimed effect, and how it was derived |
| Kill criteria | Numeric thresholds, each with the verdict it fires |

A kill criterion fires `FALSIFIED` or `NOT PROVEN`, never anything else. `CLOSED` is not a
kill criterion's to issue; it comes from the envelope.

```python
KillCriterion("net_return_ci95_lower_bps", "<=", Decimal("0"), Verdict.NOT_PROVEN)
KillCriterion("net_return_bps", "<", Decimal("0"), Verdict.FALSIFIED)
KillCriterion("permutation_p_value", ">=", Decimal("0.05"), Verdict.FALSIFIED)
```

### Sealing

`Preregistration.seal()` is the SHA-256 of the canonical JSON of the nine elements. The
written record carries the seal; a record whose content no longer matches its seal is refused
on read. Nothing is edited in place.

### Amendment

An amendment is a new dated registration that points at the seal of the one it amends. Both
are kept. It may:

- tighten a kill threshold (the kill region may only grow),
- add a criterion,
- correct a cost-term source or the universe after a live check found the assumption wrong.

It may not loosen or drop a criterion, and it may not touch the horizon, the metric, the claim
or the mechanism. Those changes are a new hypothesis, and they get a new registration.

### What a registration cannot do

It cannot make an underpowered test conclusive. If the declared sample is not reached, the
outcome is `NOT PROVEN` whatever the point estimate says. And it only locks the parameters
somebody thought of; a rebalance hour fixed early for an unrelated reason has the same
statistical effect as a chosen one. Layer 5 enumerates the phase space rather than trusting
memory.

## Layer 4: data requirements

```python
from trading_research_agent.chain import check_requirements

requirements = check_requirements(registration, manifest)
requirements.passed, requirements.blocking_axis
```

Three checks from the dataset manifest alone: coverage (the data spans the declared research
era), resolution (the horizon is at least ten times the mean spacing between trades, so the
instrument can see what is claimed), and power feasibility (enough non-overlapping horizons fit
inside the era to reach the declared sample). A failure here is never a death. It is
`NOT PROVEN` with a blocking axis of `data`, `cadence` or `power`: a null from the wrong
instrument is indistinguishable from a falsification, so it is reported as coverage-limited
and the hypothesis stays open behind it.

## The era seal

```python
from trading_research_agent.chain import Seal, research_only

research_only(timestamps, registration.era, who="my instrument")
seal = Seal("held-out B", specification_hash=registration.seal(), gates=("net > 0",), path=...)
seal.open(registration.seal(), opened_at=now)  # once
```

Research never touches sealed data, and a seal is opened once. `research_only` raises on a
single timestamp outside the research era rather than clipping the request, because a silent
clip returns a result nobody can interpret. `Seal` is single-shot bookkeeping persisted in a
file written once: the specification hash and the acceptance gates are fixed at construction,
a second opening is refused even with a different specification, and a request whose hash
differs from the pre-registered one is refused even on a fresh seal. The seal it descends from
caught a mechanism that had inverted out of sample after passing every gate before it, and
could only catch it once.

## Layer 5: validation

```python
from trading_research_agent.chain import TradeRecord, validate

result = validate(registration, trades, cost_of, block_length=..., n_resamples=5000, n_perm=1000)
result.decision.verdict, result.decision.axis, result.metrics
```

Layer 5 assesses results; it does not simulate strategies. Trades arrive as records
(`instrument, entry_ts_ns, exit_ts_ns, direction, weight, gross_return_bps`) and `cost_of`
returns each trade's round trip, normally from `execution.realised_cost`. Eight requirements
run, each of which replaced a specific way the research programme had fooled itself:

| Requirement | What runs | Prevents |
|---|---|---|
| Net from day one | every trade net of its own cost; gross reported beside it and never a verdict | a programme measured mid to mid, corrected to -5.6 bps |
| Correlation-aware inference | collapse to one portfolio return per timestamp, then a moving-block bootstrap interval | fifteen instruments carrying the information of 1.33 treated as fifteen; a sign flip from +0.86 to -8.74 |
| Live-path sizing | the declared weights, not equal weights | a sign flip between aggregation conventions |
| Subperiod stability | the sample cut into three sequential blocks | +338 bps at p = 0.001 that lived entirely in the final third |
| Declared power | timestamps against the declared sample; a kill without power is `NOT PROVEN` | underpowered results recorded as kills |
| Matched null | sign permutation (direction flipped, cost kept) or cross-sectional shuffle (weights permuted, turnover guarded) | a time-shuffle control that manufactured 2,200 bps of edge |
| Multiple-testing correction | `benjamini_hochberg` for a family of tests | a best-of-N result reported as a single finding |
| No post-hoc selection | `anchor_robustness` over every rebalance phase, two-sided, terciles | a +14.11 bps headline that was the best of 24 undisclosed phases |

The null is costed: the control pays the same round trip, so its mean sits near minus the cost,
and a null centred at zero is the sign that costs were not applied. Its p-value never reaches
exactly zero. Where the published specification and the reference implementation disagree,
the specification is followed and the difference is stated in the docstring: a block that does
not fit the sample is refused rather than clamped, and a missing value is refused rather than
dropped.

The verdict applies the registration's own kill criteria to the computed metrics
(`net_return_bps`, `net_return_ci95_lower_bps`, `permutation_p_value`,
`subperiod_min_net_bps`, ...). A fired `FALSIFIED` criterion becomes `NOT PROVEN` when the
declared power was not achieved. A death on the sample's own merit is `idea`; one that lives in
a single subperiod is `condition`. An idea that survives is `OPEN`, awaiting shadow at layer 7.
Every result carries the registration seal and the SHA-256 of the trades it was computed from.

What layer 5 does not deliver: live results. On the one strategy the research programme
measured in all three environments, the walk-forward figure missed live by thirty to forty
basis points in the conservative direction. The number is on the right side of zero, which is
the decision it is used for.
