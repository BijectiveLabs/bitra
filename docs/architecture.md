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
  data/        any trade data to Parquet, dataset audit, manifest; venue
               archive plugins in data/venues, column maps for the rest      -> ledger, envelope
  chain/       falsification chain, layers 0 to 5                               -> ledger, envelope, data
  execution/   cost identity, fills after latency, trade-through matching,
               fill probability, markout                                        -> ledger, envelope, data, chain
  strategy/    the core protocol, exits bound to the mechanism, the walk-forward
               harness; no strategy of its own                                 -> everything above
  trading/     feeds (live plugins in trading/feeds), simulated venue,
               controls, ladder, the runtime                                   -> everything above
  agent/       language-model roles at layers 2 and 3; orchestration            -> ledger, envelope, data, chain, execution
  mcp/         the gates as MCP tools                                            -> everything except agent
  cli/         the `tra` command                                                 -> everything
```

Rules, each enforced by a test in `tests/test_boundaries.py`:

1. `ledger` and `envelope` import nothing else from this project. They are vocabulary.
2. Arrows point only downward through the list. `chain` does not know `execution` exists; layer 5
   takes the cost of a trade as a function argument, and `execution` supplies one.
3. Only `agent` imports a language-model client. Layers 0, 1, 4, 5 and 7 are arithmetic and
   statistics and have no path to a model.
4. Source files are ASCII.
5. No class in the package implements `Strategy` or `FittedCore`. Decision cores live in
   `examples/strategies/` or in the operator's own package, and the package never imports
   an example.
6. No module outside `data/venues/` and `trading/feeds/` names a venue.

## Adoption rules

Four rules decide what goes into the package and what stays outside it. They exist so the
software can be adopted without inheriting the particular strategies, venues or vendors of
the research it descends from.

1. **No strategy in the core.** The package ships the mechanism (the protocol, the exits, the
   walk-forward harness) and no strategy. What `examples/strategies/` holds is an example of
   the mechanism, not a recommendation, and a run points at it by file like it would at any
   other core. Rule 5 above enforces it.
2. **Venues are plugins.** Archive layouts and live feeds are found through a registry and
   an entry point group; the two that ship are plugins like any other. The gates, the
   chain, the runtime and the agent do not know which venue a run is on. Rule 6 enforces it;
   `docs/venues.md` says how to add one.
3. **The data gate takes any data.** A venue archive through its plugin, or any CSV through a
   declared column map (`examples/mapping.toml`), ends in the same audit, the same manifest
   and the same Parquet. Nothing about where a row came from changes what the gate demands
   of it.
4. **Agents are a surface.** The language-model roles and the MCP tools call the gates; they
   never are the gates. Rule 3 above and the absence of a model on the order path enforce
   it.

Not every package exists yet. The list is the plan; a package is added when its first gate
lands with tests. Today: `ledger`, `envelope`, `data`, `chain`, `execution`, `strategy`,
`trading`, `agent`, `cli`. Outside the package: `examples/` (an envelope, a run config, a
column map, two example cores), never imported by it.

## The chain

| Layer | Test | Dies here when | Cost to run | Status |
|---|---|---|---|---|
| 0 | Cost floor | plausible magnitude below the cheapest round trip | minutes | implemented |
| 1 | Market observation | the phenomenon does not recur | hours | planned |
| 2 | Mechanism memo | nobody can be named who is structurally forced to lose | minutes | implemented (model writes, code checks the form, a critic objects once) |
| 3 | Pre-registration | the claim cannot be stated so data could refute it | hours | implemented |
| 4 | Data requirements | no available instrument can measure the premise | hours to days | implemented |
| 5 | Statistical validation | interval spans zero, loses to a matched null, or lives in one subperiod | days | implemented |
| 6 | Strategy design | the exit is not bound to the mechanism | weeks | implemented |
| 7 | Shadow | net expectation negative in the live market, zero orders placed | weeks of calendar | implemented (`tra trade shadow`, then layer 5 on its trades) |
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
| `trades.jsonl` | JSON Lines | layer 5 input: `instrument`, `entry_ts_ns`, `exit_ts_ns`, `direction`, `weight`, `gross_return_bps`, numbers as decimal strings |
| `validation.json` | JSON | layer 5 output: interval, null report, subperiods, power, metrics, the decision with every fired criterion, the registration seal and the trades hash |
| `seal.json` | JSON, written once | era, opened_at, specification hash, gates |
| `strategy.toml` | TOML | a run of the trading agent: the core by file or import, exit rule, run settings, controls; see `examples/strategy.toml` |
| `mapping.toml` | TOML | a column map that takes any CSV through the data gate; see `examples/mapping.toml` |
| `intents.jsonl`, `fills.jsonl` | JSON Lines | every intent with its admission decision; every simulated fill |
| `ladder.json` | JSON | current tier and the history of promotions (with evidence and signature) and demotions (with reason) |

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
