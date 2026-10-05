# Working with language models

This project asks models to do one narrow thing: read figures that code computed and answer inside
a closed schema. Everything else people usually reach for a framework to get - orchestration,
memory, caching, retries, continuity when a provider fails - is either already handled by that
shape or is listed here with what we do instead.

## No orchestration framework, and what would change that

There is no LangChain, LangGraph, CrewAI or agent framework in the dependency tree. The fan-out is
forty lines: a thread pool, an answer written to a file the moment it arrives, and `--resume` to
reuse the answers already on disk. A cycle is two rounds - the analysts together, then the
validators - and the order is in one function you can read top to bottom.

The criteria that would make us adopt a graph framework were written down before the decision and
still hold: a cycle that needs conditional re-entry into earlier steps, a run that must survive a
process restart mid-graph, or more than a handful of nodes whose order is data-dependent. None of
those describe a cycle that decides once. What a framework would add today is an indirection layer
between an operator and the reason their trade was refused.

## Memory

| What people mean by memory | What this project uses |
|---|---|
| conversation history in a thread | Nothing. Each role call is stateless: one system prompt, one brief built by code. |
| what happened in earlier cycles | The record on disk: `runs/cycles/*/cycle.json`, the intents, `usage.jsonl`, the ledger. The auditors read exactly that. |
| a vector store of past decisions | Not used. A decision is retrieved by reading the cycle it belongs to, which is a file with a timestamp in its name. |
| caching a fetched value | `store/cache.py`: a TTL-bounded file cache, and the data chain's own freshness window. |

That is a deliberate trade. Stateless roles mean the same brief produces the same question no matter
which model answers it, which is what makes a model swap, a retry or an audit meaningful. A
conversation would make each of those a different thing.

## When a provider is rate limited or down

The vendor SDK retries first: `max_retries` (default 2) covers 429 and 5xx with backoff, and
`call_timeout_s` bounds each attempt. What was missing until now was the next step, and it is the
same answer the data layer gives - a chain:

```toml
[agents]
provider = "groq"
model = "openai/gpt-oss-120b"
fallback = ["google:gemini-flash-latest", "anthropic:claude-opus-5-5"]

[agents.roles.sentiment_analyst]
model = "openai/gpt-oss-20b"
fallback = ["groq:openai/gpt-oss-120b"]     # this role's own order, instead of the one above
```

The first link that answers wins. A link that answers "try again in N seconds" with N inside
`[agents].max_retry_wait_s` (15 by default) is waited for once before the next link is tried, so a
short burst over a per-minute limit does not throw away the model you chose. A link that cannot be reached is recorded with the vendor's own
error class and the next one is tried **with the same brief** - there is no conversation to carry
over, because the brief was built by code from the snapshot. A link is built the first time it is
needed, so naming a spare you have no key for costs nothing until the day you need it.

One failure does not fall through: a spent budget. `token_budget`, `max_calls_per_hour` and
`min_interval_s` are the operator's own limits, and no provider is allowed past them. The chain
stops where it stands and the cycle records why.

`tra check` reads the fallback list against the registered providers, so a typo is a line in the
check rather than a surprise at the moment the primary provider goes down.

### When every link fails

What happens next depends on the role, and none of it can make a trade larger:

| Role | If no model answers | Why |
|---|---|---|
| technical analyst | the cycle stops; no trade | it is the only role that proposes a direction |
| reading analyst (macro, on-chain, sentiment) | its label is missing; its score still counts | the score is code over your thresholds and never needed the model |
| validator | the trade is refused (`[agents].on_missing_validator = "refuse"`, the default); with `"continue"` it goes on with the votes that came back | a check you declared that did not happen is not a check |
| market screener | the instruments code admitted go on unfiltered | the screener can only remove, never add |
| auditor | a line in the report | auditors never act |

Every failure is a refusal line in `cycle.json` with the vendor's own message, and a provider
failure wakes the technical auditor (`[audit].on_alarm`). The way to make all of this rarer is
capacity, not code: a provider tier whose tokens-per-minute covers a whole cycle (the screener,
three analysts and two validators measured 9,200 to 10,200 tokens inside one minute on the demo
desk, against a free tier's 8,000), roles split across vendors with `[agents.roles.<role>]`, and a
fallback from a different vendor.
A pass also stops asking anyone when the desk is already at `[desk].max_open_positions`, so a full
book does not spend a provider's rate on cycles that could only be refused.

## Prompt caching

The Anthropic adapter marks the system prompt as cacheable and reads back what the provider says it
cached. Both figures are now written to `usage.jsonl` per call:

```json
{"role": "technical_analyst", "input_tokens": 1180, "cached_tokens": 0, "cache_write_tokens": 0}
```

Read that column before believing in the saving. Caching pays when the stable prefix is large, and
the minimum cacheable prefix is between 512 and 4096 tokens depending on the model. This project's
role prompts are deliberately short - a few hundred tokens, because the figures live in the brief
rather than in the prompt - so on a cycle the cache often will not engage at all. That is a
consequence of a design choice we would make again, and the honest thing is to measure it rather
than to claim it.

Where it does pay: a long tool loop (`tra agent session`), where the same system prompt and a
growing history are sent every turn. The silent invalidators to avoid are the usual ones - a
timestamp or a cycle id inside the system prompt, an unsorted JSON dump, a tool list that changes
per call. The briefs in this project are sorted JSON for exactly that reason.

## Batching

Not used for a cycle, and it should not be: a batch is an asynchronous queue measured in hours, and
a cycle decides on a snapshot that is seconds old. The place a batch belongs is the eval path -
scoring a role against many recorded briefs at 50 percent of the price - and that is where it will
go when the eval set is large enough to be worth the plumbing. Until then, `tra agent eval` runs the
calls straight.

## Fine-tuning

Not planned, and this is a position rather than a gap.

- **A weight is not a record.** The claim this project makes is that nothing decides without leaving
  a figure and a reason that anyone can re-read. A fine-tuned model moves part of the decision into
  weights nobody can audit, including us.
- **It bakes in look-ahead.** Fine-tuning on a desk's own past decisions teaches the model what
  worked, which is a statement about a period that has already happened. The falsification chain
  exists to stop exactly that kind of borrowed knowledge from looking like edge.
- **It is vendor lock with a hosting bill.** A tuned model lives at one provider, which is the
  opposite of the chain above.

The levers we use instead: a smaller model per role where the job is labelling rather than
reasoning (`[agents.roles.<role>]`), the output cap tuned per role, the brief carrying every figure
so the model never computes, and `tra agent eval` to measure a model on recorded briefs before it
decides anything with money behind it.

## What a role may spend

Nothing, by default, is capped. That is a decision rather than an oversight: an answer cut off
halfway is a wasted call and a worse decision, not a saving, and a role that stops mid-thought
because of a ceiling this software chose leaves an operator with neither the answer nor the money.

What happens instead is counting. Every call is a line in `usage.jsonl` with its role, provider,
model, tokens, cached tokens, latency and - when `[agents.pricing.<model>]` says what you were
quoted - its price. Every cycle reports what it spent. `[targets].max_research_bps` measures that
spend against the notional of the trade it produced, and the auditors read the same records and
propose where to spend less. Optimising a bill is a decision with figures behind it, which is an
operator's job and an auditor's, not a default.

An operator who wants a hard ceiling anyway declares one, and it does what it says:

| Declaration | Effect | Undeclared |
|---|---|---|
| `token_budget` | the process stops asking once the ledger passes it | no ceiling |
| `max_calls_per_hour` | a role refuses its own call past that rate | no ceiling |
| `min_interval_s` | a role waits that long between its own calls | no wait |
| `max_output_tokens` | the cap sent to the vendor | the model's own maximum |

Two notes on the last one. The Anthropic API requires `max_tokens` on every request, so an
undeclared cap becomes a generous default there rather than nothing - a field the vendor insists on,
not a budget. And an audit needs room a one-line answer does not: when a declared cap is below what
a report with findings in it takes, the audit raises it for that call and says so.

## How hard a role thinks

A role that labels a series does not need what a role reasoning over sixty bars needs, and on a
reasoning model that difference is most of the latency and most of the bill. `effort` is one
vocabulary - `minimal`, `low`, `medium`, `high`, `xhigh`, `max` - declared in `[agents]` and
overridden per role, and each adapter maps it onto what its own vendor takes:

| Vendor | Field | Takes | Mapping |
|---|---|---|---|
| Anthropic | `output_config.effort` | low, medium, high, xhigh, max | `minimal` lands on `low`; the rest pass through |
| Google | `thinking_config.thinking_level` | MINIMAL, LOW, MEDIUM, HIGH | `xhigh` and `max` land on `HIGH` |
| OpenAI and compatible | `reasoning_effort` | minimal, low, medium, high | `xhigh` and `max` land on `high` |

Undeclared, the field is left out of the request entirely, which matters: most servers behind a
`base_url` reject `reasoning_effort` outright, and a model with no reasoning to configure should be
sent nothing. A level a vendor cannot take is mapped rather than refused, because a fallback chain
that dies on a spelling is a chain that does not help on the day it is needed.

## Choosing a model per role

Cost per decision, not cost per call. `usage.jsonl` gives tokens and latency per role, and the
technology auditor divides them by the intents the desk actually produced. The pattern that has held
so far: a stronger model for the role that decides, a cheaper one for the roles that label, and
enough output headroom for whatever schema the role fills - a role that returns several cited claims
needs roughly twice the cap a one-line answer does, which is measured, not guessed.
