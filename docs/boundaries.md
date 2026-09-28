# Where this software stops

Every boundary on this page is a decision. None of them is an oversight, and none of them is
waiting for someone to notice. They are written down because a trader deciding whether to run this
against their own capital deserves to know what it will not do before they find out at an awkward
moment, and because a contributor deserves to know which pull request will be turned down and why.

One sentence explains most of them:

> **This project executes one decision. A trading platform manages a book.**

A cycle looks at a market once, decides once, prices what that decision would cost, sizes it against
what the desk agreed on, and either writes an intent or refuses with a reason. Everything that
follows from holding many positions across many venues over many days - routing, netting, inventory,
transfers, reconciliation over time - is a different job with a different shape, and doing it badly
inside this one would make both harder to audit.

## Execution

| Not here | Why | What to do instead |
|---|---|---|
| Order routing across venues | A router picks a venue from live liquidity; a cycle is told its channel by the operator and prices that channel's fee | Run one project per venue, or route above this |
| Execution algorithms (TWAP, VWAP, iceberg, slicing) | They need a long-lived order manager and a view of the book over minutes; a cycle decides once | `--mode signals`, and let your own executor work the order |
| Parent and child orders | Same reason | As above |
| Retries after a venue error | A retry around real capital is how one defect becomes ten orders | The refusal is recorded; the next cycle decides again with fresh figures |
| Chasing a resting order across the spread | The channel was priced at the maker fee; crossing pays the taker fee the gate never priced | `limit_timeout_s` cancels it, or declare a `taker-taker` channel |
| More than one order per cycle | One decision, one order. `[execution].max_orders_per_cycle` exists to make that explicit, not to raise it | Run more cycles |

Enforced in `trading/dispatch.py`, which is the only code in the project that can place an order
from a cycle, and in the mandate that has to clear before it runs at all (`docs/cycle.md`).

## Accounts and money movement

The venue hand reads the account before it sends - a position pointing the other way refuses, and an
account it cannot read refuses too - and that is the whole of its relationship with your account.

Not here: transfers between wallets or sub-accounts, changing leverage or margin mode, creating or
managing sub-accounts, closing positions this project did not open, funding withdrawals, balance
reconciliation over time, P&L accounting, tax lots, statements. Those are decisions about your
account rather than about a trade, and software that decides once should not be the thing holding
them.

## Venues

Centralised perpetual futures, through the venue plugins that ship or any plugin registered through
the same entry point. A decentralised venue can be **priced** - `[[channel]].gas_quote` is a
declared cost per round trip and the gate treats it like any other component - but it cannot be
**executed**: there is no wallet, no RPC endpoint, no transaction signing and no nonce management in
this project, and adding them behind a flag would be the wrong way to ship a different product.

## Data

The engine pulls and the agents read. No agent fetches anything, and there is no code path from a
role to the internet; that is checked by a test over the import graph, not by review.

Inside a cycle, values come from REST endpoints with a freshness window the operator declares, and a
value older than that window is refused rather than used. There is no tick-by-tick execution feed
inside a cycle: a cycle that asks a language model is working in seconds, and a figure that has to
be fresher than that belongs to a different kind of system. Streaming (`ccxt.pro`, which ships
inside `ccxt` and adds websockets rather than a paid tier) belongs in the feed layer that a live
session uses, not in the cycle's own pricing.

## What a model may do

| A model may | A model may never |
|---|---|
| Read one class of data and declare a state in its own vocabulary | Size a trade |
| Propose a direction, a confidence and a horizon | Change a configuration file |
| Argue for or against a proposal, citing figures it was given | Promote a ladder tier |
| Write an audit finding with a proposed change for a person to apply | Loosen a control, a cap or a gate |
| | Place, cancel or modify an order |

The size comes from the operator's conviction ladder, the gates read declared states and measured
figures rather than sentences, and a claim that cites a figure the brief did not carry is discarded
by code. If a model writes the language of an order or a size in its prose, that is recorded as a
crossing beside its answer and changes nothing (`agents/scope.py`).

## Research

The falsification chain is not an optimiser. There is no parameter search, no grid, no automatic
selection of the best variant, and no fitting of a core to a dataset. The chain's job is to say
whether a claim survives the layers, and a claim that only survives after twenty variants were tried
has not survived anything. A walk-forward harness ships; a walk-forward *optimiser* does not.

## Scale and shape

Files are the record: cycles, snapshots, briefs, answers, intents, audits. There is no database, no
message bus and no scheduler in this project. One process runs one instrument's cycle at a time, and
the model calls inside it go out together on a small pool of threads. That is a deliberate size: it
keeps the failure modes countable and the record replayable by anyone with a text editor.

This is not a high-frequency system. Bars are a minute or longer, decisions are seconds apart at
best, and nothing here is written for microsecond latency.

## Safety and responsibility

Sandbox is the default. Venue mode needs a mandate the operator wrote and signed in their own
project, the ladder at its canary tier, the caps declared, and keys in the environment - never in a
file, a brief, a prompt or a record. There is no production tier in this project: the ladder refuses
it outright.

Nothing here is advice. The licence is MIT and carries no warranty, and a verdict from a gate is a
statement about a dataset and a registration, not a recommendation to trade. `docs/liability.md`
says where the responsibility sits, in plain words.

Compliance, reporting, jurisdiction and the question of whether you may run this at all are the
operator's, and this project neither checks nor claims them.

## How these stay true

A boundary that lives only in a document drifts. These are held by code:

- `tests/test_boundaries.py` checks the import graph, that no gate names a venue, that no module
  outside the provider directory imports a model vendor, and that the shipped templates are the
  examples.
- `agents/scope.py` filters every brief to the sections a role's job needs, so a role cannot reason
  about a figure it was never given.
- `execution/mandate.py` names every missing piece before venue mode can start, and `tra check`
  prints them.
- The prompts forbid outcome talk, and a test reads them to make sure the words only appear where
  they are being forbidden.

## If you need more than this

The boundaries above are where this project ends, not where the work ends. A desk that needs a
portfolio manager across venues, an execution desk with algorithms, account and treasury operations,
or a production tier with an operational runbook needs a trading platform, and that is a different
piece of software with different tests and a different audit surface. Keeping the two apart is what
lets this one stay small enough to read.
