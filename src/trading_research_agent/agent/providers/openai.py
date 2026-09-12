"""OpenAI adapter, on the official SDK (`pip install 'trading-research-agent[openai]'`).

No default model: the operator names one. Structured output is requested as a strict JSON
schema, and the result is still validated by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trading_research_agent.agent.completer import Completion
from trading_research_agent.agent.providers.anthropic import ProviderError


class OpenAICompleter:
    def __init__(self, model: str, *, client: Any = None, max_tokens: int = 4096) -> None:
        if not model.strip():
            raise ValueError("an OpenAI model name is required")
        if client is None:
            try:
                import openai
            except (
                ImportError
            ) as exc:  # pragma: no cover - exercised only where the extra is absent
                raise ProviderError(
                    "the openai SDK is not installed; pip install 'trading-research-agent[openai]'"
                ) from exc
            client = openai.OpenAI()
        self.model = model
        self.max_tokens = max_tokens
        self._client = client

    def request(self, *, system: str, user: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_completion_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "gate", "strict": True, "schema": dict(schema)},
            },
        }

    def complete(self, *, system: str, user: str, schema: Mapping[str, Any]) -> Completion:
        response = self._client.chat.completions.create(
            **self.request(system=system, user=user, schema=schema)
        )
        choice = response.choices[0]
        if getattr(choice.message, "refusal", None):
            raise ProviderError("the model declined the request")
        text = choice.message.content
        if not text:
            raise ProviderError("no content in the response")
        usage = response.usage
        return Completion(
            text=str(text),
            model=str(response.model),
            input_tokens=int(usage.prompt_tokens),
            output_tokens=int(usage.completion_tokens),
        )
