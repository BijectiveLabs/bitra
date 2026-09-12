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
| 4 | Data requirements | no available instrument can measure the premise | hours to days | 0.3 |
| 5 | Statistical validation | interval spans zero, loses to a matched null, or lives in one subperiod | days | 0.3 |
| 6 | Strategy design | the exit is not bound to the mechanism | weeks | 0.3 |
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
