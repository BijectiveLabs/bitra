# What it guarantees, and what is not built yet

This page separates two things that are easy to confuse.

- **Guarantees** are rules the system enforces and the tests check. They protect capital and keys,
  and any change, including a contribution, has to keep them.
- **Not built yet** is scope: things a trading system can do that this project does not do today.
  None of them is forbidden. Each one says what a contribution would need, and [Contributing](/opensource/contributing)
  says how to propose it.

## Guarantees

| Guarantee | Held by |
|---|---|
| No language model places, cancels or resizes an order, holds a key, or opens a network connection | the import graph test (`tests/test_boundaries.py`): only `llm/providers` imports a vendor SDK, and no role reaches the venue code |
| No model can loosen a control: it may tighten a limit, step the ladder down or arm the kill switch, never the reverse | `agents/` tools expose only the tightening actions; the ladder refuses a promotion without evidence and a signature |
| One decision sends at most one order, and a venue error is recorded, not retried | `trading/dispatch.py`; a retry around real capital is how one defect becomes ten orders |
| An order goes out only after the mandate, the ladder tier, the caps, the cost gate, the risk and desk limits, and the venue checks (step, drift, margin, liquidation) have all passed | `execution/mandate.py`, `agents/cycle.py`, `trading/dispatch.py`; each refusal is named |
| Missing or stale data is refused, never treated as zero or as current | the data chains' `ttl`, `max_age` on series, the snapshot's refusals |
| Every figure an agent reads is computed by the system and kept on disk with its source and age; a citation that does not match is discarded | `agents/scope.py`, `agents/cited.py`, `inputs.json.gz` and `tra runs verify` |
| Keys live in your environment or the `.env` beside `strategy.toml`, never in a config file, a brief, a prompt or a record, and are blanked out of errors | `cli/credentials.py`, `llm/completer.py`, `data/pull.py` |
| Sandbox is the default, and there is no production tier | `execution/mandate.py`, the ladder |
| The research chain does not select among variants: a claim that only survives after many tries has not survived | `chain/`; the walk-forward harness ships, an optimiser that feeds the chain does not |

## Not built yet

| Area | Today | What a contribution would need |
|---|---|---|
| **Market types: spot, inverse and dated futures, options** | Venue mode trades linear perpetual futures and refuses any other market type on every order (`market_kind` in `trading/dispatch.py`) | A cost model for that market (spot has no funding; options have premium and greeks), exit semantics that hold there (spot cannot be shorted without margin), sizing and liquidation rules, and tests; then that type is added to what the venue hand accepts |
| **Decentralised venues** | A DEX can be priced (`[[channel]].gas_quote`) but not executed | Wallet and transaction signing kept outside every model's reach, RPC and nonce management, slippage and MEV protection, and the same mandate and caps |
| **More than one account or sub-account** | One account per project, read before every order | Account selection that is declared, never chosen by a model; per-account caps, equity and daily loss; the keeper and desk counting per account |
| **Wallet transfers, withdrawals, treasury** | None. The venue hand reads the account and sends orders, nothing else | Separate keys with separate permissions, a human approval step, and records of every movement; this is the area where the guarantees above matter most |
| **Routing across venues, execution algorithms (TWAP, VWAP, iceberg)** | One venue per project; one order per decision, taker IOC or maker post-only | A long-lived order manager with its own records, so `--mode signals` plus your own executor is the route today |
| **Portfolio accounting, P&L history, tax lots, statements** | `positions.jsonl` and `round_trips.jsonl` per trade, with fees and funding as the venue reports them | Reconciliation against venue statements over time, built on the records the desk already writes |
| **Streaming data inside a cycle** | REST snapshots with a declared freshness window; `tra live` uses a websocket feed | A streaming feed in the cycle's data layer, keeping the snapshot as the record of what a decision saw |
| **A database or message bus** | Files are the record; `tra run --every` is one in-process loop | A store behind the same record writer (`records/`), with the files or an export kept as the auditable source |
| **High frequency** | Bars of a minute or more; decisions seconds apart | A different architecture: models are not in the loop at that speed |

A contribution in any of these areas is welcome. What it cannot do is weaken a guarantee to get
there; where the two meet, the guarantee wins and the feature finds another shape.

## Data, models and research, in one place

- The engine fetches and the agents read. No agent fetches anything, and there is no code path from
  a role to the internet.
- A model may read one class of data and declare a state, propose a direction with a confidence and
  a horizon, argue for or against a proposal citing its figures, or write an audit finding for a
  person to apply. It never sizes a trade, changes a configuration file, promotes a tier, loosens a
  control, or touches an order.
- If a model writes the language of an order or a size in its prose, that is recorded as a crossing
  beside its answer and changes nothing (`agents/scope.py`).

## Safety and responsibility

Venue mode needs a mandate the operator wrote and signed in their own project, a leverage and a
cap per order, keys in the environment, and on mainnet the ladder at its canary tier. Nothing here is advice: the licence is
MIT with no warranty, and a verdict from a gate is a statement about a dataset and a registration,
not a recommendation to trade. Compliance, reporting, jurisdiction, and whether you may run this at
all are the operator's to settle. [Liability](/opensource/liability) says where responsibility sits, in plain words.
