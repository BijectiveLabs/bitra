# Model providers

Providers are plugins. `--provider NAME` on `tra agent propose` and `[agents].provider` in
`strategy.toml` take any registered name; three adapters ship, one of which serves every
vendor that speaks the OpenAI chat API.

| Name | Extra | Key variable | Endpoint | Structured output |
|---|---|---|---|---|
| `anthropic` | `[anthropic]` | `ANTHROPIC_API_KEY` | the SDK's | JSON schema, prompt caching on the doctrine |
| `google` | `[google]` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | the SDK's | JSON schema |
| `openai` | `[openai]` | `OPENAI_API_KEY` | the SDK's | strict JSON schema |
| `deepseek` | `[openai]` | `DEEPSEEK_API_KEY` | `https://api.deepseek.com` | JSON mode |
| `groq` | `[openai]` | `GROQ_API_KEY` | `https://api.groq.com/openai/v1` | enforced schema; JSON mode where a model cannot |
| `xai` | `[openai]` | `XAI_API_KEY` | `https://api.x.ai/v1` | JSON mode |
| `mistral` | `[openai]` | `MISTRAL_API_KEY` | `https://api.mistral.ai/v1` | JSON schema |
| `openrouter` | `[openai]` | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` | JSON mode |
| `qwen` | `[openai]` | `DASHSCOPE_API_KEY` | `--base-url` (depends on region and workspace) | JSON mode |
| `ollama` | `[openai]` | none | `http://localhost:11434/v1` | JSON mode |
| `9router` | `[openai]` | `9ROUTER_API_KEY` | `http://localhost:20128/v1` | JSON schema; the model is a combo or `provider/model` from the router's dashboard; a combo whose first upstream ignores JSON mode fails as any other model would |
| `local` | `[openai]` | none | `--base-url` (https, or http on localhost) | JSON mode |

Every endpoint and key variable was read from the vendor's own documentation on 2026-09-13.

Model names belong to your account, not to this package, and that is deliberate: a catalogue of
model names goes stale every month and a warning about an unknown name is noise. If a vendor
answers `404` for a model that used to work, ask the vendor which names your key may use. Seen on
2026-09-27 with a Gemini key: `gemini-2.5-flash` replied "no longer available to new users" while
`gemini-flash-latest` worked on the same key. A failed call is recorded in `usage.jsonl` with the
vendor's own error, so this is visible rather than mysterious.

Output caps are the other account-shaped trap. A reasoning model spends most of its output budget
thinking before it writes the answer, so a cap that looks generous for a short JSON object is not.
Measured on 2026-09-27 against one small reasoning model, asked for a three-field object from an
800-character brief:

| Cap | Outcome |
|---|---|
| 512 | `json_validate_failed`: "max completion tokens reached before generating a valid document" |
| 1024 | answered, using 704 output tokens; one cycle out of two still failed the same way |
| 2048 | answered, using 681 output tokens |

Two things follow. Give a reasoning model at least twice the output it actually needs, because the
thinking is the part that varies. And treat a schema failure as a role that did not answer: it
arrives as a 400, which no SDK retries, and it is recorded in `usage.jsonl` as a failed call while
the cycle carries on without that role.
"JSON mode" means the vendor documents JSON output but not a strict schema: the schema is put
in the system prompt, and the result is validated by the role's parser exactly as it is for
every other provider. A malformed answer is a refusal of form, never a decision.

```
tra agent propose --idea "..." --envelope envelope.toml --provider groq --model <model>
tra agent propose --idea "..." --envelope envelope.toml --provider local \
    --base-url http://localhost:8000/v1 --model <whatever the server serves>
```

A missing key is refused before any network call, naming the variable. A connection drop or
a vendor-side error during a call is a failed call: it is written to `usage.jsonl` with its
class, the role gets no answer, and the session goes on; five failures in a row halt it.

Reasoning models spend most of their tokens and their seconds thinking before the JSON. On a
bar cadence that is the latency that matters: measured on one such model, 12 s median and 20 s
at the 90th percentile per call. `[agents].max_output_tokens` caps the thinking; set it below
what the model needs and it returns no JSON at all, which the vendor reports as a validation
failure and the ledger records as a failed call. `decide_every_bars` is the other lever.

A local server that needs no key gets a placeholder, because the client library insists on
one.

## Picking a model

```bash
tra models groq                    # every model your key can use there
tra models google --filter flash   # only the names that contain "flash"
tra init my-desk --profile autonomous --agent groq --model openai/gpt-oss-120b
```

`tra models` reads the vendor's own model list with your key: no prompt is sent and nothing is
spent. Without a provider it reads `[agents].provider` from the desk. `tra init --agent` writes the
provider and model into the desk from the start; a provider with a default model (Anthropic) needs
no `--model`, and one without says to run `tra models` first.

## Adding a provider

A vendor that needs its own client registers under the `trading_research_agents.providers`
entry point, or by calling `register`:

```python
from trading_research_agents.llm.providers import Provider, register

register(Provider("my-vendor", "MY_VENDOR_API_KEY", None, False, build_my_completer))
```

`build_my_completer(model, base_url)` returns anything with `complete(system=, user=,
schema=) -> Completion`. The completion carries the model name and the token counts, which is
what `usage.jsonl` records; a provider that cannot count tokens should say so rather than
return zero. A provider that can honour the run's call settings also passes `build_with`:
`build_with(model, base_url, options: CallOptions)`, where `CallOptions` carries
`max_output_tokens`, `timeout_s` (per request) and `max_retries`; the bundled adapters hand
all three to their SDK clients.
