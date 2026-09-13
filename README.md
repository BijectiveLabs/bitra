![Bijective Labs](assets/bijective_banner.png)

# trading-research-agent

**The trading agent that shows its kill count.**

Two agents for crypto markets that share one ledger. The research agent is not allowed to
say a strategy works until the gates have measured it: the data it was tested on is sound,
the idea survived the cheap tests before the expensive ones, and the result is net of what
execution actually costs. The trading agent runs what the research agent registered, in
real time, through the same gates, with controls that fail closed and no language model on
the order path. Every outcome, including the ones that end in minutes, is written to the
kill ledger.

Status: pre-release, under active development. The API is not stable and the package is not
yet on PyPI.

---

## Why this exists

Most trading-agent frameworks are good at producing a decision and silent about whether the
decision was earned. They do not carry a cost model, they do not hold out data, and they
cannot tell you which of their past calls were wrong and why.

This project starts from the opposite end. It is built on a research programme that closed
thirty hypotheses in four months without committing capital to any of them, and wrote down
what each one died of. The agent inherits that discipline as code:

- **Cost first.** A 3.0 bps signal with a p-value of zero is dead against a 9.0 bps round trip.
  That comparison is layer 0 and it runs before anything else.
- **Net from day one.** Fees, spread, slippage and funding are in every figure. Gross is
  reported, labelled, and never carries a verdict.
- **Power before verdict.** An underpowered result is `NOT PROVEN`, not `FALSIFIED`. The two
  are enforced as different words because the difference is a closed branch nobody closed.
- **Reversal points.** A verdict blocked by fees records the fee at which it reverses, so a
  reader with a better schedule reuses the work instead of repeating it.
- **Fewer agents, more gates.** Language models write the mechanism memo, the falsifiable
  claim and the exit design. The arithmetic and the statistics are code, and cannot be argued
  with.

`numpy` is the one required dependency; everything that touches money is `Decimal`, and the
statistics run in floating point from the collapsed series on.

## What is here today

| Package | Purpose |
|---|---|
| `trading_research_agent.ledger` | The kill ledger: a closed vocabulary of verdicts and axes, an append-only store, and the consistency rules between them |
| `trading_research_agent.envelope` | The operating envelope: fees per channel, measured round trips, latency. Configuration, never constants |
| `trading_research_agent.data` | The data gate: any trade data to canonical trades, through a venue plugin or a declared column map; a one-pass audit with findings, a manifest with checksums, and exact `DECIMAL` Parquet with the `[data]` extra |
| `trading_research_agent.strategy` | The decision-core protocol, exits bound to the mechanism (layer 6, causal stops with adverse fills), the walk-forward harness that refuses precomputed parameters. No strategy of its own |
| `trading_research_agent.trading` | The trading agent: recorded feeds and live feed plugins, a simulated venue on the execution rules, controls that fail closed, the promotion ladder, one runtime for replay and shadow |
| `trading_research_agent.agent` | An idea through layers 0, 2 and 3: bound, cost floor, mechanism memo with a critic, sealed pre-registration; refusals and verdicts recorded apart. Providers as extras: `[anthropic]`, `[openai]` |
| `trading_research_agent.cli` | The `tra` command: data, chain, agent, trade, ladder, ledger |
| `trading_research_agent.chain` | The falsification chain: layer 0 (cost floor), layer 3 (sealed pre-registration), layer 4 (data requirements), layer 5 (validation: net returns, block bootstrap, matched null, subperiods, power, BH, anchor robustness), and the era seal |
| `trading_research_agent.execution` | Execution realism: the cost identity with funding, walk-the-book, passive fills with trade-through, effective cost across unfilled orders, markout |

Layer 0 in six lines:

```python
from decimal import Decimal
from trading_research_agent.envelope import load_envelope
from trading_research_agent.chain import cost_floor

envelope = load_envelope("examples/envelope.toml")
check = cost_floor(envelope, Decimal("3.0"), venue="hyperliquid")
print(check.outcome, check.margin_bps)
```

A stop at layer 0 becomes a `CLOSED` row on the cost axis, envelope-bound, with the reversal
point recorded. A pass records nothing, because clearing the cheapest test is not a verdict.

## Command line

The same gates from a shell. Exit code 0 means the gate was cleared, 1 means it said no, 2 is
a usage error; add `--json` to any command for machine-readable output.

```bash
tra data fetch --venue binance --instrument BTCUSDT --month 2025-01 --out data/
tra data audit data/BTCUSDT-trades-2025-01.zip --venue binance --instrument BTCUSDT \
    --max-gap 5m --manifest data/manifest.json
tra data audit export.csv --mapping examples/mapping.toml --instrument BTCUSDT --max-gap 5m
tra chain floor --envelope examples/envelope.toml --magnitude 3.0 --venue hyperliquid     --subject "informed-flow burst drift" --ledger ledger.jsonl --id H-001
tra chain check preregistration.json
tra ledger show
tra agent propose --idea "Longs paying high funding get squeezed into settlement"     --envelope examples/envelope.toml --provider anthropic
tra agent refusals
tra chain requirements --registration runs/<id>/preregistration.json --manifest data/manifest.json
tra chain validate --registration runs/<id>/preregistration.json --trades trades.jsonl \
    --cost-bps 15.0 --block-length 7 --ledger ledger.jsonl --id V-001
tra trade replay --config examples/strategy.toml --envelope examples/envelope.toml \
    --parquet data/btc.parquet --out runs/replay-1
tra trade shadow --config examples/strategy.toml --envelope examples/envelope.toml \
    --out runs/shadow-1
tra ladder promote --evidence "layer 5 OPEN on replay-1" --signed-by operator
```

`data fetch` streams the archive to a `.part` file, hashes it as it arrives, and keeps it only
if the size and, where the venue publishes one, the checksum agree. `data audit` refuses a
dataset the way the gate would, and writes the manifest with the finding either way. A venue
is a plugin (`docs/venues.md`); data from anywhere else comes through a column map.

## What the package does not contain

- **A strategy.** The mechanism ships (the protocol, the exits, the walk-forward harness); the
  cores in `examples/strategies/` are examples of it, measured and closed in the research it
  descends from, and a run points at them by file like it would at your own.
- **A venue.** The two that ship are plugins found through the same registry as a
  third-party one. Nothing in the gates names a venue, and a test fails the build if it does.
- **A data vendor.** Any CSV with a declared column map goes through the same gate.
- **A model on the order path.** Language models draft and critique at layers 2 and 3; code
  decides everywhere, and nothing that places an order can reach a model.

## Roadmap

| Release | Adds |
|---|---|
| 0.1 | Ledger, envelope, layer 0, pre-registration with numeric kill criteria, public-archive ingestion and dataset audit for two venues, `tra` CLI |
| 0.2 | The agent: bound, mechanism memo with a critic, and pre-registration drafted by a language model, validated by the existing gates, recorded to the ledger; bounded revision loop; Anthropic and OpenAI adapters |
| 0.3 | Execution realism: cost identity, latency-shifted fills, trade-through matching, fill probability, markout; layers 4 and 5 with the era seal |
| 0.4 | The trading agent: layer 6 exits, walk-forward harness, replay and shadow on one code path, controls, ladder, a live public feed (`[shadow]`) |
| 0.4.1 | Adoption rules: no strategy in the core, venues as plugins with entry points, any data through a column map |
| 0.5 | Live venue adapter behind a security review (canary and production tiers); MCP server exposing the gates to any agent host |

Crypto perpetuals first. The core vocabulary is market-agnostic (`venue`, `instrument`,
`session`), but every number in the gates was measured on crypto venues and nothing is promised
for other markets until it has been measured there too.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

See `CONTRIBUTING.md` for the branch flow and the standards the code is held to,
`docs/architecture.md` for the package boundaries and the file formats at them, `docs/data.md`
for the data gate, `docs/chain.md` for the chain, `docs/execution.md` for execution realism,
`docs/agent.md` for the research agent, `docs/trading.md` for the trading agent, and
`docs/ledger.md` for the verdict vocabulary.

## License

Apache License 2.0. See `LICENSE` and `NOTICE`.

Built by [Bijective Labs](https://github.com/BijectiveLabs).
