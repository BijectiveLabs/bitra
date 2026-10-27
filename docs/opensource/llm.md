---
description: "How TRA works with language models without an orchestration framework: schemas, retries, caching, budgets and provider failover."
---

# Working with language models

This project asks models to do one narrow thing: read figures that the system computed and answer inside
a closed schema. Everything else people usually reach for a framework to get - orchestration,
memory, caching, retries, continuity when a provider fails - is either already handled by that
shape or is listed here with what we do instead.

## No orchestration framework, and what would change that

There is no LangChain, LangGraph, CrewAI or agent framework in the dependency tree. The fan-out is
forty lines: a thread pool, an answer written to a file the moment it arrives, and `--resume` to
reuse the answers already on disk. A cycle is at most three rounds - the technical analyst, then the
two advocating validators together, then the judge - and the order is in one function you can read
top to bottom.

The criteria that would make us adopt a graph framework were written down before the decision and
still hold: a cycle that needs conditional re-entry into earlier steps, a run that must survive a
process restart mid-graph, or more than a handful of nodes whose order is data-dependent. None of
those describe a cycle that decides once. What a framework would add today is an indirection layer
between an operator and the reason their trade was refused.

## Memory

| What people mean by memory | What this project uses |
|---|---|
| conversation history in a thread | Nothing. Each role call is stateless: one system prompt, one brief built by the system. |
| what it said last time | The technical analyst's brief carries its own last reading of the instrument, when there is one inside the window it reads (`previous`): how long ago, the target, setup, confidence and levels it gave, the close then and how far it has moved since. Code builds it from the record. What became of that reading (traded, refused, won, lost) is left out, since that would tell a proposing role the desk's rules. |
| what happened in earlier cycles | The record on disk: `runs/cycles/*/cycle.json`, the intents, `usage.jsonl`, the ledger. The auditors read exactly that. |
| one store the roles share | The cycle directory. Every brief and every answer is a file in it, written atomically (a temporary file, then a rename), and an index per day finds them again. |
| a vector store of past decisions | Not used. A decision is retrieved by reading the cycle it belongs to, which is a file with a timestamp in its name. |
| caching a fetched value | `store/cache.py`: a TTL-bounded file cache, and the data chain's own freshness window. |

That is a deliberate trade. Stateless roles mean the same brief produces the same question no matter
which model answers it, which is what makes a model swap, a retry or an audit meaningful. A
conversation would make each of those a different thing. The last reading is no exception: it is in
the brief, so it is on disk with everything else the model was shown.

Why the analyst sees its last reading at all: on two demo desks, between 2026-09-28 and 2026-10-16,
8 of 36 and 4 of 37 pairs of answers on the same instrument less than 90 minutes apart pointed
opposite ways, long then short or the reverse. Some of those are the market turning. The point of
showing the earlier reading is that a reversal is now a decision with its reason written down
("what changed in the figures"), not two unrelated answers.

## When an answer does not fit, or points at nothing

Three checks stand between a model's text and anything that reads it.

1. **The schema is enforced at the vendor where the vendor can**: strict JSON schema output on
   Anthropic, Google and the OpenAI-compatible servers that support it, JSON mode with the schema in
   the prompt on the rest. Code then parses the answer again and refuses any field outside it, a
   wrong value in a closed list, or a number out of range.
2. **A malformed answer is asked once more, with the reason.** The second call carries the same
   brief, then one line saying what was wrong ("response lacks verdict, reason"), so the model does
   not have to guess and the provider's cached prefix still holds. A second malformed answer fails
   the call and the cycle records it.
3. **Every figure a role cites is checked against its brief**, by name and value. A citation that
   names a figure the brief does not carry, or a different value, is discarded, and a deciding
   answer with none left is refused. On the OKX demo desk, from 2026-10-12 to 2026-10-16, no
   citation was discarded across 44 technical analyst answers, 132 answers of the reading analysts
   that existed until 0.11, and 17 validator answers. Between 2026-10-08 and 2026-10-11 those
   reading analysts had 27 discarded, all of them names the brief did not use, such as
   `fear_greed_zscore` for `series.fear_greed.zscore_vs_recent`.

## How much a role is given

Measured on the same desk (342 calls, `openai/gpt-oss-120b` and `-20b` on Groq, before 0.11): a
call carries a median of about 1,950 tokens for the technical analyst, 1,390 for a validator and
1,540 for the screener, system prompt included; the macro, on-chain and sentiment analysts of the
time took 1,950, 1,730 and 1,350, and since 0.11 they are readings that ask no model. The
largest was 2,915. Each call starts from a fresh context, so nothing accumulates across a cycle or a
day, and no role comes near any current model's window. The one place a context grows is the
research auditor's tool loop, which is capped at six calls with each observation cut at 6,000
characters.

## When a provider is rate limited or down

The vendor SDK retries first: `max_retries` (default 2) covers 429 and 5xx with backoff, and
`call_timeout_s` bounds each attempt. What was missing until now was the next step, and it is the
same answer the data layer gives - a chain:

```toml
[agents]
provider = "groq"
model = "openai/gpt-oss-120b"
fallback = ["google:gemini-flash-latest", "anthropic:claude-opus-5-5"]

[agents.roles.validator]
model = "openai/gpt-oss-20b"
fallback = ["groq:openai/gpt-oss-120b"]     # this role's own order, instead of the one above
```

The first link that answers wins. A link that answers "try again in N seconds" with N inside
`[agents].max_retry_wait_s` (15 by default) is waited for once before the next link is tried, so a
short burst over a per-minute limit does not throw away the model you chose. A link whose quota
is spent (the vendor says `RESOURCE_EXHAUSTED` or quota), or that asks for a longer wait than that,
rests for 15 minutes, or for as long as the vendor asked, across every role in the process: the
next cycles go straight to the next link instead of paying the same failure in seconds the market
does not wait for. When every link rests, the one waking soonest is asked anyway. Usage records the
provider that actually answered. A link that cannot be reached is recorded with the vendor's own
error class and the next one is tried **with the same brief** - there is no conversation to carry
over, because the brief was built by the system from the snapshot. A link is built the first time it is
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
| technical analyst | the cycle stops; no trade | it is the role that proposes a direction |
| technical peer | no trade | the team trades only when both reads agree |
| flow analyst | its voice is absent; the cycle goes on | a voice that said nothing neither helps nor hurts |
| validator | the trade is refused (`[agents].on_missing_validator = "refuse"`, the default); with `"continue"` it goes on with the votes that came back | a check you declared that did not happen is not a check |
| market screener | the instruments the system admitted go on unfiltered | the screener can only remove, never add |
| auditor | a line in the report | auditors never act |

Every failure is a refusal line in `cycle.json` with the vendor's own message, and a provider
failure wakes the technical auditor (`[audit].on_alarm`). The way to make all of this rarer is
capacity, not code: a provider tier whose tokens-per-minute covers a whole cycle (the screener,
the analysts and two validators measured 9,200 to 10,200 tokens inside one minute on the demo
desk before 0.11, against a free tier's 8,000; the readings no longer ask a model), roles split across vendors with `[agents.roles.<role>]`, and a
fallback from a different vendor.
A pass also stops asking anyone when the desk is already at `[desk].max_open_positions`, so a full
book does not spend a provider's rate on cycles that could only be refused.

## Prompt caching

The Anthropic adapter marks the system prompt as cacheable and reads back what the provider says it
cached; the OpenAI-compatible adapter reads `prompt_tokens_details.cached_tokens` where a server
reports it. Groq reported a cached prefix on 14 of the 342 calls above (12 of them the screener's).
Both figures are written to `usage.jsonl` per call:

```json
{"role": "technical_analyst", "input_tokens": 1180, "cached_tokens": 0, "cache_write_tokens": 0}
```

Read that column before believing in the saving. Caching pays when the stable prefix is large, and
the minimum cacheable prefix is between 512 and 4096 tokens depending on the model. The figures
live in the brief rather than in the prompt, so most role prompts are a few hundred words; the
deciding analyst's is the longest, about 1,400 words, which is above some vendors' minimum. The
brief follows the prompt as sorted JSON, so the stable part comes first. Whether a vendor caches it
is the vendor's decision, and the honest thing is to measure it rather than to claim it.

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

Roles that read a table the system already computed (the screener and the validators) take `low`
when `[agents].effort` is declared and their own role table does not say otherwise; the technical
analyst keeps `[agents].effort`. With nothing declared, no
role is sent an effort at all, for the reason above.

## Not paying twice for the same reading

The macro, on-chain and sentiment readings ask no model. Their figures are read by code against
the thresholds you declared, and that declared state is what confluence and conviction read; until
0.11 a model also labelled each of them, and no decision ever rested on the label, so the calls
were retired.

Two more things keep calls from being wasted. The validators are not asked when the system is certain to
refuse whatever they say: the desk with nothing added, the day's round trips, the declared reward to
risk, or an edge that fails against the part of the round trip that does not grow with size. And
briefs are sent as JSON without spaces, which are tokens that carry nothing.

## The typed judge

A reasoning model writes sentences. A typed judge answers closed questions about a piece of text
with a probability for each answer it was offered, and writes nothing else. TRA uses one for the
narrow job it is built for: reading the headlines the recorder archived into typed events the
features can weigh. It never decides a direction from numbers, sizes a trade or reaches an order.

```toml
# data.toml
[judge]
provider = "typesafe"           # TypeSafe's Jev, over its HTTP API
model = "jev-1.14.0"            # a version; an alias that moves is refused
key_env = "TYPESAFE_API_KEY"    # the key comes from the environment
min_confidence = 0.5            # below this the kind of event is absent, not a guess
cache = "record"                # record | replay | off
```

Each headline is asked four things in one request: which kind of event it reports (a listing, a
delisting, a hack or exploit, an outage, a regulatory action, a macro release, or none of these),
whether it concerns each asset the desk records, whether it concerns the market as a whole, and
whether it carries instructions to a machine rather than news. A headline that speaks to a machine
is dropped and counted. Only the headline and its publisher are sent: never a position, a balance,
an order or a key, and any credential that appears in the text is replaced before it leaves.

| Failure | What it means | What happens |
|---|---|---|
| no answer in time (3 s per read, 6 s in all) | nothing was answered | that headline is absent, with the reason |
| 429 or 529 | the vendor asks to slow down | waited for, honouring `Retry-After`, inside the same 6 s; then absent |
| 400 or 422 | the request was wrong | absent with the vendor's message; never retried |
| 401 or 403, or no key | the key is missing or refused | the run stops; `tra check` names the variable |
| a confidence below `min_confidence` | the judge does not know | the kind is absent, not "none" |
| a replay with no recorded verdict | the run would not be the one it claims to replay | the run stops |

Every verdict carries the versioned model that gave it and goes through a cache under
`data/judge/`, keyed by the model, the headline and the questions. `cache = "replay"` answers from
the cache alone and needs no key, which is how a published result is reproduced without an
account. The vendor's own page of known weaknesses is the one to read before trusting it with
more: it is not a calculator, it compares dates and times unreliably, and adversarial text can move
its answer. So no number and no date is asked of it, its probabilities enter as features rather
than as decisions, and the arena measures them before anything leans on them.

`tra text judge --from 2026-10-25` reads the archived headlines of a range of days and prints what
the features would read; `tra check` names the judge, its pinned model and a missing key.

## Choosing a model per role

Cost per decision, not cost per call. `usage.jsonl` gives tokens and latency per role, and the
technology auditor divides them by the intents the desk actually produced. The pattern that has held
so far: a stronger model for the role that decides, a cheaper one for the roles that label, and
enough output headroom for whatever schema the role fills - a role that returns several cited claims
needs roughly twice the cap a one-line answer does, which is measured, not guessed.
