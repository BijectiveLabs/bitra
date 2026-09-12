![Bijective Labs](assets/bijective_banner.png)

# trading-research-agent

**The trading agent that shows its kill count.**

A research agent for crypto markets that is not allowed to say a strategy works until three
gates have measured it: the data it was tested on is sound, the idea survived the cheap tests
before the expensive ones, and the result is net of what execution actually costs. Every
outcome, including the ones that end in minutes, is written to a kill ledger.

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

## What is here today

| Package | Purpose |
|---|---|
| `trading_research_agent.ledger` | The kill ledger: a closed vocabulary of verdicts and axes, an append-only store, and the consistency rules between them |
| `trading_research_agent.envelope` | The operating envelope: fees per channel, measured round trips, latency. Configuration, never constants |
| `trading_research_agent.data` | The data gate: Bybit and Binance public archives to canonical trades, a one-pass audit with findings, and a manifest with checksums |
| `trading_research_agent.cli` | The `tra` command: fetch, audit, floor, check, show |
| `trading_research_agent.chain` | The falsification chain. Layer 0 (cost floor) and layer 3 (sealed pre-registration with numeric kill criteria and tighten-only amendments) are implemented |

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
tra data audit data/BTCUSDT-trades-2025-01.zip --venue binance --instrument BTCUSDT     --max-gap 5m --manifest data/manifest.json
tra chain floor --envelope examples/envelope.toml --magnitude 3.0 --venue hyperliquid     --subject "informed-flow burst drift" --ledger ledger.jsonl --id H-001
tra chain check preregistration.json
tra ledger show
```

`data fetch` streams the archive to a `.part` file, hashes it as it arrives, and keeps it only
if the size and, where the venue publishes one, the checksum agree. `data audit` refuses a
dataset the way the gate would, and writes the manifest with the finding either way.

## Roadmap

| Release | Adds |
|---|---|
| 0.1 | Ledger, envelope, layer 0, pre-registration with numeric kill criteria, public-archive ingestion and dataset audit for two venues, `tra` CLI |
| 0.2 | The agent: mechanism memo and falsifiable claim written by a language model, validated by code, recorded to the ledger; bounded revision loop |
| 0.3 | Execution realism: cost identity, latency-shifted fills, trade-through matching, fill probability as its own quantity; statistical validation with matched nulls |
| 0.4 | Shadow harness on one code path; MCP server exposing the gates to any agent host |

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
for the data gate, `docs/chain.md` for the chain, and `docs/ledger.md` for the verdict
vocabulary.

## License

Apache License 2.0. See `LICENSE` and `NOTICE`.

Built by [Bijective Labs](https://github.com/BijectiveLabs).
