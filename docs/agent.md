# The agent

An idea in plain language goes in. One of four recorded outcomes comes out. Language models
are used at exactly three points, and every decision between them is made by code against the
gates that already exist.

```
idea
  -> [model: bound]      plausible magnitude, with its basis
  -> [code: floor]       cost_floor(envelope, magnitude)            stop -> CLOSED in the ledger
  -> [model: memo]       one sentence: who is forced to lose, and why
  -> [code: form]        one sentence, one line, long enough to name a party
  -> [model: critique]   does it name a forced loser, or describe a pattern?
  -> [model: draft]      the nine elements of a pre-registration, as JSON
  -> [code: build]       Preregistration(...) with the validation that already exists
  -> [code: seal]        seal, write preregistration.json, OPEN in the ledger
```

| Outcome | Meaning | Recorded where |
|---|---|---|
| `stopped_at_floor` | the declared magnitude is below the cheapest round trip | kill ledger: `CLOSED`, cost axis, reversal point |
| `refused_layer_0` | no basis for a magnitude could be given | `refusals.jsonl` |
| `refused_layer_2` | after the allowed revisions, no forced loser was named | `refusals.jsonl` |
| `refused_layer_3` | after the allowed revisions, no valid pre-registration could be built | `refusals.jsonl` |
| `registered` | sealed pre-registration; awaiting measurement | kill ledger: `OPEN`, blocking axis `power`; `preregistration.json` |

Refusals are not verdicts. An idea that cannot name its mechanism was never a hypothesis, and
putting it in the kill ledger would mix rows that never carried a hypothesis into the
falsification count. They are counted and shown separately: `tra agent refusals`.

## Two kinds of rejection

A defect of form goes back to the model with the gate's own error text, up to
`max_revisions` times (default 2): a memo with two sentences, a cost model missing a term, a
kill threshold that is not a number. The text the model sees is the text the gate produced;
there is no second validator.

A fact about the idea is final: its bound is below the floor, or nobody can be named who is
forced to lose after the revisions are spent. Returning that kind would invite the model to
raise a number until it clears, and a gate that can be argued with is not a gate.

## The bound is a declaration, not a measurement

The magnitude at layer 0 comes from one of three sources, in order of preference: a comparable
measurement on record, the dispersion of the quantity the signal keys on, or the scale of the
concession the mechanism forces. The model names which one it used, and the basis is written
into the ledger row beside the number. If no basis can be given, the honest answer is
`basis: none`, and the run is refused rather than a number invented.

An operator can declare the magnitude directly (`--magnitude`), in which case the bound role
is skipped and the basis is recorded as `declared`.

## What the models never do

- They do not measure anything. Layers 1, 4, 5, 6 and 7 are data and statistics, and they
  arrive in later releases as code.
- They do not change the mechanism after the critic accepted it. The registration draft is
  built around the accepted memo, which is passed in and not re-asked.
- They do not see a return, a win rate, or a profit target anywhere in a prompt. The prompts
  ask them to name and to formulate.
- They do not hold credentials. Keys are read by the vendor SDKs from the environment and are
  written nowhere by this package.

## The session roles

The three research roles above are one of two things the package calls an agent. The
other is the session: seven roles with full access to the gates, the technical analyst
among them as a decision core. `docs/agents.md` describes it; `docs/providers.md` lists the
model providers both share.

## Providers

```
pip install 'trading-research-agents[anthropic]'   # ANTHROPIC_API_KEY, default model claude-opus-5-5
pip install 'trading-research-agents[openai]'      # OPENAI_API_KEY, --model required
```

Any server that speaks the OpenAI API (a local model server, a relay, another vendor's
compatible endpoint) is reached through the openai adapter with `--base-url` or
`TRA_OPENAI_BASE_URL`; a local server that needs no key gets a placeholder. The URL is https,
or http on localhost only.

Both adapters request structured output against the role's JSON schema and both results are
still validated by the caller. The Anthropic adapter marks the system prompt for prompt
caching, since it is the frozen doctrine and never changes between calls. Tests use a
`FakeCompleter` with scripted responses and never touch the network.

## Every run leaves a trail

```
runs/<run_id>/
  01-bound.json          system, user, schema, raw response, model, tokens, time
  02-memo.json
  03-critique.json
  04-registration.json
  preregistration.json   when registered
  run.json               outcome, ledger entry, token totals, models used
```

A ledger row written by the agent can be traced back to the exchange that produced it. That is
what makes a kill count worth reading.

## Command line

```bash
tra agent propose --idea "Longs paying high funding get squeezed into settlement" \
    --envelope examples/envelope.toml --provider anthropic \
    --ledger ledger.jsonl --refusals refusals.jsonl --runs runs/
tra agent refusals
```

Exit 0 when registered, 1 when stopped or refused, 2 for a usage error. `--json` on either.

## Orchestration

There is no agent framework underneath. The pipeline is a handful of pure steps with two
bounded loops, and a run lasts seconds. Every step is a function of its inputs so it can be
placed into a graph runtime later without being rewritten. The condition for adopting one is
written down: the first release that needs parallel fan-out with checkpointed partial results,
or runs that must survive a process restart.
