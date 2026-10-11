# The kill ledger

"It did not work" is not a research finding. It is the absence of one. The ledger records
what was tested, how it ended, and on which axis, so that a death on cost does not close a
branch that was never tested on its idea.

## Verdicts

| Verdict | Meaning |
|---|---|
| `FALSIFIED` | A pre-registered kill criterion was met, with the statistical power to support it |
| `CLOSED` | The mechanism measured real, and a declared envelope parameter blocked it |
| `NOT PROVEN` | The interval spans the threshold, or declared power was not achieved. Not a kill |
| `OPEN` | Under test, or awaiting a measurement not yet taken |
| `CONFIRMED` | A fact established about a venue. Not a tradeable claim, carries no axis |
| `VALIDATED` | A component that passed its acceptance criteria and is running. Carries no axis |
| `WITHDRAWN` | A previously issued verdict, retracted. Names the row it retracts and says why |

`NOT PROVEN` and `FALSIFIED` are enforced as different words. A hypothesis is only killed when
the measurement had the power to kill it. Without that power the result is an absence of
evidence, and labelling it a falsification poisons every decision that inherits the label.

## Axes

| Axis | The hypothesis died because |
|---|---|
| `idea` | the proposed mechanism is not present in the data |
| `cost` | the mechanism is present and smaller than the execution cost |
| `data` | the instrument could not measure the premise |
| `cadence` | the measurement was too slow to observe the phenomenon |
| `power` | the sample was too small to distinguish the effect from zero. **Not a death**; produces `NOT PROVEN` |
| `condition` | the result exists only in one regime or one stretch of the sample |
| `method` | the test itself was constructed wrongly. A death of the test, not of the idea |
| `capital` | the mechanism needs more working capital than was available |
| `venue` | the mechanism is priced in an instrument that could not be reached |
| `adverse selection` | passive execution is picked off faster than it earns |
| `mechanism inverted` | the effect held in research and reversed sign out of sample |

More than one axis may be recorded, in order. `data, then idea` means a defect in the
instrument exposed the result first, and the hypothesis then died on its merits once the
defect was fixed. Transferability follows the final axis.

## Transferability

| Classification | Meaning | Axes |
|---|---|---|
| `UNIVERSAL` | The mechanism itself failed. No change to fees, latency, cadence, capital or venue rescues it | idea, condition, method, mechanism inverted |
| `ENVELOPE-BOUND` | The mechanism held and a declared parameter blocked it. A reader with different parameters should expect a different answer | cost, cadence, capital, venue, adverse selection |

`data` is resolved case by case: a recorder that sampled too coarsely is envelope-bound
because finer data exists; a field the venue does not publish is universal until the venue
changes.

An `ENVELOPE-BOUND` verdict records its **reversal point**: the parameter and the value at
which the verdict reverses. A cost-axis verdict closed at an 11.0 bps round trip carries
`c_rt < <signal magnitude> bps`, and is a live question for any operator below that.

## Consistency rules

These are enforced when an entry is constructed; an entry that breaks them does not exist.

| Verdict | Axis of death | Blocking axis | Transferability | Reversal point |
|---|---|---|---|---|
| `FALSIFIED` | required; never `power` | none | required; must match the final axis | only if envelope-bound |
| `CLOSED` | required; never `power` | none | must be `ENVELOPE-BOUND` | required |
| `NOT PROVEN`, `OPEN` | none | required | none | none |
| `CONFIRMED`, `VALIDATED` | none | none | none | none |
| `WITHDRAWN` | none | none | none | none; must name `retracts` and give a reason |

All timestamps are UTC and timezone-aware. Entry identifiers are permanent and never reused.

## The file

`ledger.jsonl`, one JSON object per line, schema version 1. Rows are appended and fsynced.
Nothing is edited in place. A row that cannot be parsed stops the reader with its line
number; a row that breaks a rule above is refused on read as well as on write, because the
file may have been edited by hand.

The kill count is computed over rows that still stand: everything except retractions and the
rows they retracted.
