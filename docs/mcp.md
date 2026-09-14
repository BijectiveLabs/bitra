# The MCP server

Agents are a surface. The language-model roles inside the package call the gates at layers 0,
2 and 3; the MCP server lets any agent host outside the package call the gates too. It reads.
It does not trade, promote a tier, or write a ledger row, and it never will: capital is
governed by code that is tested and fails closed, not by a tool a model can call.

```
pip install 'trading-research-agent[mcp]'
tra mcp serve                      # stdio; what desktop hosts launch
uv run mcp dev -m trading_research_agent.mcp.server   # or point the Inspector at a file
```

A host configuration names the command and nothing else:

```json
{ "mcpServers": { "trading-research-agent": { "command": "tra", "args": ["mcp", "serve"] } } }
```

## Tools

Every tool is annotated read-only and closed-world; nine of them, the same nine the agent
roles hold. Paths are resolved on the machine running the server; every number in basis
points is a decimal string; every time is UTC.

| Tool | Reads | Returns |
|---|---|---|
| `cost_floor_check(envelope_path, magnitude_bps, venue="")` | `envelope.toml` | layer 0: outcome, cheapest channel, round trip, margin, whether the claim stops here |
| `check_registration(registration_path)` | `preregistration.json` | name, seal, horizon, instruments, required sample, kill criteria, what it amends |
| `audit_dataset(archive_path, instrument, venue="" \| mapping_path="", max_gap_minutes=5)` | a `.csv`, `.csv.gz` or `.zip` | the audit verdict with every finding |
| `validate_trades(registration_path, trades_path, cost_bps, ...)` | `trades.jsonl` | the layer-5 record: interval, null, subperiods, power, the decision with every fired criterion |
| `ledger_show(ledger_path="ledger.jsonl")` | the kill ledger | counts per verdict and every row that stands |
| `ladder_show(ladder_path="ladder.json")` | the ladder | tier and history |
| `run_report(run_dir)` | `run.json`, `trades.jsonl` | the run summary with closed round trips counted |
| `ledger_search(ledger_path, query, limit=10)` | the kill ledger | the rows that stand whose subject, note, evidence or axis mention the query's words, best match first |
| `runs_index(runs_dir, instrument="")` | every `run.json` under a directory | past runs, newest first, with their summaries |

A refusal from a gate (a registration that does not seal, a file that is not a trade archive,
an instrument outside the declared universe) comes back as a tool error the model can read
and act on. A crash does not: the tools wrap only the exceptions the gates raise deliberately.

## What is deliberately absent

`place_order`, `promote`, `record`, `demote`, `run_live`. A host that wants a model to place
orders has to write that tool itself, outside this package, and then explain why it did.
