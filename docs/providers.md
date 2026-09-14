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
| `groq` | `[openai]` | `GROQ_API_KEY` | `https://api.groq.com/openai/v1` | JSON mode |
| `xai` | `[openai]` | `XAI_API_KEY` | `https://api.x.ai/v1` | JSON mode |
| `mistral` | `[openai]` | `MISTRAL_API_KEY` | `https://api.mistral.ai/v1` | JSON schema |
| `openrouter` | `[openai]` | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` | JSON mode |
| `qwen` | `[openai]` | `DASHSCOPE_API_KEY` | `--base-url` (depends on region and workspace) | JSON mode |
| `ollama` | `[openai]` | none | `http://localhost:11434/v1` | JSON mode |
| `local` | `[openai]` | none | `--base-url` (https, or http on localhost) | JSON mode |

Every endpoint and key variable was read from the vendor's own documentation on 2026-09-13.
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

## Adding a provider

A vendor that needs its own client registers under the `trading_research_agent.providers`
entry point, or by calling `register`:

```python
from trading_research_agent.agent.providers import Provider, register

register(Provider("my-vendor", "MY_VENDOR_API_KEY", None, False, build_my_completer))
```

`build_my_completer(model, base_url)` returns anything with `complete(system=, user=,
schema=) -> Completion`. The completion carries the model name and the token counts, which is
what `usage.jsonl` records; a provider that cannot count tokens should say so rather than
return zero.
