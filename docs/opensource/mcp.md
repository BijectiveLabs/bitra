---
description: "The MCP server: read-only tools that let any agent host call the research gates of TRA over stdio."
---

# The MCP server

Agents are a surface. The language-model roles inside the package call the gates at layers 0,
2 and 3; the MCP server lets any agent host outside the package call the gates too. It reads.
It does not trade, promote a tier, or write a ledger row. That is a guarantee, not a gap: capital
is governed by the system that is tested and fails closed, not by a tool a model can call. Read tools are
open to contributions.

```
pip install 'bitra[mcp]'
tra mcp serve                      # stdio; what desktop hosts launch
uv run mcp dev -m trading_research_agents.mcp.server   # or point the Inspector at a file
```

A host configuration names the command and nothing else:

```json
{ "mcpServers": { "tra": { "command": "tra", "args": ["mcp", "serve"] } } }
```

## Use it from your own agent

Your agent does not need this package installed to use it. With [uv](https://docs.astral.sh/uv/)
on the machine, the host starts the server on demand, at the released version, with the MCP SDK
it needs:

```json
{
  "mcpServers": {
    "tra": {
      "command": "uvx",
      "args": ["--with", "mcp>=2,<3", "bitra", "mcp", "serve"]
    }
  }
}
```

Any host that launches a stdio server takes this, desktop chat apps and coding agents alike. Add
`"--root", "/path/to/project"` at the end to let the tools read a project other than the directory
the host starts in. The package installs a `bitra` command next to `tra`, which is what `uvx bitra` runs; the
`TRA` command of earlier releases stays as an alias for now.

What your agent gets is the same set of checks this package's own roles use, and nothing that
trades: it can ask whether a claimed edge clears the round-trip cost, whether a dataset is fit to
test on, and what a validation run decided. Every tool reads; none sends an order, writes a
ledger row or moves the ladder. The entry for the MCP Registry is `server.json` at the root of the
repository, under the name `io.github.BijectiveLabs/bitra`. Glama builds the
server from the build spec on its admin page (Python 3.12, `uv sync --locked --no-dev --extra mcp`,
then `uv run --no-sync TRA mcp serve` behind its `mcp-proxy`), which
`glama/Dockerfile` records and reproduces, and CI checks on every change that both start and list
their tools.

## Tools

Every tool is annotated read-only and closed-world; ten of them, the same functions the
agent roles reach through their briefs and the auditor through its tool loop. Paths are resolved on the machine running the server; every number in basis
points is a decimal string; every time is UTC.

| Tool | Reads | Returns |
|---|---|---|
| `check_cost_floor(envelope_path, magnitude_bps, venue="")` | `envelope.toml` | layer 0: outcome, cheapest channel, round trip, margin, whether the claim stops here |
| `observe_events(dataset_path, observation_path)` | a Parquet dataset, `observation.toml` | layer 1: events, events per subperiod, the response distribution and curve, whether the phenomenon recurs |
| `check_registration(registration_path)` | `preregistration.json` | name, seal, horizon, instruments, required sample, kill criteria, what it amends |
| `audit_dataset(archive_path, instrument, venue="" \| mapping_path="", max_gap_minutes=5)` | a `.csv`, `.csv.gz` or `.zip` | the audit verdict with every finding |
| `validate_trades(registration_path, trades_path, cost_bps, ...)` | `trades.jsonl` | the layer-5 record: interval, null, subperiods, power, the decision with every fired criterion |
| `show_ledger(ledger_path="ledger.jsonl")` | the kill ledger | counts per verdict and every row that stands |
| `show_ladder(ladder_path="ladder.json")` | the ladder | tier and history |
| `read_run_report(run_dir)` | `run.json`, `trades.jsonl` | the run summary with closed round trips counted |
| `search_ledger(ledger_path, query, limit=10)` | the kill ledger | the rows that stand whose subject, note, evidence or axis mention the query's words, best match first |
| `list_runs(runs_dir, instrument="")` | every `run.json` under a directory | past runs, newest first, with their summaries |

A refusal from a gate (a registration that does not seal, a file that is not a trade archive,
an instrument outside the declared universe) comes back as a tool error the model can read
and act on. A crash does not: the tools wrap only the exceptions the gates raise deliberately.

## What is deliberately absent

`place_order`, `promote`, `record`, `demote`, `run_live`. A host that wants a model to place
orders has to write that tool itself, outside this package, and then explain why it did.
