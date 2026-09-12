# Architecture

The product is one chain of gates. A claim about a strategy passes the data gate, the
falsification chain and the execution gate, in that order, before the agent may say it. This
page fixes the package boundaries, the direction of dependency, and the files that cross
those boundaries. The Python API is allowed to change before 1.0; the file formats are the
contract.

## Packages

```
trading_research_agent/
  ledger/      verdict vocabulary, axes, transferability, kill-ledger rows      depends on nothing
  envelope/    operator parameters: fees per channel, latency, venue access     depends on nothing
  data/        public-archive ingestion to Parquet, dataset audit, manifest     -> ledger, envelope
  chain/       falsification chain, layers 0 to 5                               -> ledger, envelope, data
  execution/   cost identity, fills after latency, trade-through matching,
               fill probability, versioned universe, shadow harness             -> ledger, envelope, data, chain
  agent/       language-model roles at layers 2, 3 and 6; orchestration          -> everything above
  mcp/         the gates as MCP tools                                            -> everything except agent
  cli/         the `tra` command                                                 -> everything
```

Rules, each enforced by a test in `tests/test_boundaries.py`:

1. `ledger` and `envelope` import nothing else from this project. They are vocabulary.
2. Arrows point only downward through the list. `chain` does not know `execution` exists.
3. Only `agent` imports a language-model client. Layers 0, 1, 4, 5 and 7 are arithmetic and
   statistics and have no path to a model.
4. Source files are ASCII.

Not every package exists yet. The list is the plan; a package is added when its first gate
lands with tests. Today: `ledger`, `envelope`, `data`, `chain`, `agent`, `cli`.

## The chain

| Layer | Test | Dies here when | Cost to run | Status |
|---|---|---|---|---|
| 0 | Cost floor | plausible magnitude below the cheapest round trip | minutes | implemented |
| 1 | Market observation | the phenomenon does not recur | hours | planned |
| 2 | Mechanism memo | nobody can be named who is structurally forced to lose | minutes | implemented (model writes, code checks the form, a critic objects once) |
| 3 | Pre-registration | the claim cannot be stated so data could refute it | hours | implemented |
| 4 | Data requirements | no available instrument can measure the premise | hours to days | 0.3 |
| 5 | Statistical validation | interval spans zero, loses to a matched null, or lives in one subperiod | days | 0.3 |
| 6 | Strategy design | the exit is not bound to the mechanism | weeks | 0.3 |
| 7 | Shadow | net expectation negative in the live market, zero orders placed | weeks of calendar | 0.4 |
| 8 | Live capital | | capital | out of scope |

The chain is strictly ordered by cost. A hypothesis does not reach layer 5 because somebody is
confident about it.

## Files at the boundaries

| Artefact | Format | Minimum content |
|---|---|---|
| Dataset | Parquet; price and quantity as `DECIMAL(38, s)` with `s` the instrument's scale; timestamps UTC nanoseconds | trades: `ts`, `venue`, `instrument`, `price`, `qty`, `side`, `trade_id`, `seq` |
| `manifest.json` | JSON | schema version, venue, instrument, UTC range, source files with SHA-256, normaliser version, audit verdict, creation time |
| `preregistration.json` | JSON, sealed by content hash | mechanism, claim, metric, horizon, universe, era, cost model, power, kill criteria; amendments are separate dated files referencing the original hash and may never loosen a threshold or move a horizon |
| `ledger.jsonl` | JSON Lines, append-only | one row per entry; nothing is rewritten; a retraction is a new `WITHDRAWN` row naming the row it retracts |
| `envelope.toml` | TOML | one `[[channel]]` per venue and execution channel; see `examples/envelope.toml` |

## Numbers

- Basis points to one decimal place, leading minus for negatives, never parentheses.
- Times in UTC, always stated as such. A naive datetime is refused.
- Prices, quantities, fees and costs are `Decimal`. Floats appear only in statistics.
- Every quantity that carries a verdict is reported as a triple: point estimate, interval,
  sample size.

## What the platform boundary looks like

This project is consumed by other systems through the files above and nothing else. It does
not import from, link to, or know about any proprietary trading platform. A system that wants
its research tier gated by these rules reads the Parquet, the manifest and the ledger.
