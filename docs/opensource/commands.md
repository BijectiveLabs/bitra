---
title: Command line
description: Every tra command and what it does.
---

# Command line

Every command says what it did. Exit code 0 means cleared, 1 means a gate said no, 2 is a usage
error. `--json` gives machine-readable output and `--plain` gives text instead of panels.
`tra --help` lists every command and `tra <command> --help` lists its flags.

## Getting started

| Command | What it does |
|---|---|
| `tra` | opens the console |
| `tra init my-desk --profile autonomous` | a project wired for the agents, every file commented |
| `tra check` | every setting read against the others, with what to fix |
| `tra keys --probe` | proves every key works; no key is printed, no prompt or order sent |
| `tra models groq` | the models your key can use on a provider, read from the vendor; no prompt sent |
| `tra init my-desk --profile autonomous --venue okx` | a desk wired for one exchange: data, orders, practice account, keys |
| `tra venues` | every venue the docs describe, and what each can do |
| `tra venues okx --setup` | what a venue can do, and the blocks to paste to trade on it |
| `tra mandate` | the text venue mode requires; `--sign "Your Name"` keeps it as `MANDATE.md` and signs `[execution]` in one step |

## Running

| Command | What it does |
|---|---|
| `tra run` | one autonomous cycle |
| `tra run --every 15m --cycles 12` | twelve scheduled passes: the first now, then one on every fifteenth minute of the clock |
| `tra run --now` | ask the roster even if the trigger would have held a quiet market |
| `tra watch --every 5s` | a read-only view of a running desk |
| `tra audit` | both auditors over the records; nothing is applied |
| `tra hindsight` | what the trades each gate refused would have done, beside the ones it let through |
| `tra backtest`, `tra paper`, `tra live` | replay, shadow, canary |

## Data

| Command | What it does |
|---|---|
| `tra data get BTCUSDT 2025-01-01` | download a day, verify its checksum, audit it, write Parquet and funding |
| `tra data audit export.csv --mapping mapping.toml` | your own CSV, through the same audit |
| `tra data pull BTCUSDT` | one live snapshot through the chains in `data.toml` |
| `tra sources show`, `tra sources pull` | where the reading analysts' series come from and how old they are; refresh them now |
| `tra sources add macro us10y --from fred --id DGS10 --high bearish` | a series onto the shelf in one step: pulled now, its lines drawn from its own history |
| `tra universe` | which perpetuals discovery finds on the venue now, and what each filter took out |
| `tra runs verify` | every cycle's files against their hashes, and its briefs against its raw inputs |
| `tra runs prune --keep 90d` | what old cycle directories would be removed; add `--yes` (and `--archive`) to do it |

## The ladder, research and hosts

| Command | What it does |
|---|---|
| `tra ladder show`, `promote`, `demote` | which tier the desk stands at; one step up with evidence, or down |
| `tra chain floor`, `observe`, `check`, `validate` | the research chain, layer by layer ([The falsification chain](/opensource/chain)) |
| `tra ledger show` | every idea that died, and what killed it |
| `tra agent session`, `eval`, `trace` | the research session, model comparison, a run read back |
| `tra mcp serve` | the read-only gates as tools for any MCP host |
