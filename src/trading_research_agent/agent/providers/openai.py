"""OpenAI adapter, on the official SDK (`pip install 'trading-research-agent[openai]'`).

No default model: the operator names one. Structured output is requested as a strict JSON
schema, and the result is still validated by the caller. Any server that speaks the same API
(a local model server, a relay, another vendor's compatible endpoint) is reached through
`base_url`; for a server that needs no key, the SDK still wants one, so a placeholder is used
when `OPENAI_API_KEY` is absent and a base URL is set.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from trading_research_agent.agent.completer import Completion
from trading_research_agent.agent.providers.anthropic import ProviderError

BASE_URL_ENV = "TRA_OPENAI_BASE_URL"


class OpenAICompleter:
    def __init__(
        self,
        model: str,
        *,
        client: Any = None,
        max_tokens: int = 4096,
        base_url: str | None = None,
        key_env: str | None = "OPENAI_API_KEY",
        structured: str = "json_schema",
    ) -> None:
        if not model.strip():
            raise ValueError("an OpenAI model name is required")
        if structured not in ("json_schema", "json_object"):
            raise ValueError("structured is json_schema or json_object")
        base_url = base_url or os.environ.get(BASE_URL_ENV) or None
        if base_url is not None and not base_url.startswith(
            ("https://", "http://localhost", "http://127.0.0.1")
        ):
            raise ProviderError("base_url is https, or http on localhost only")
        if client is None:
            try:
                import openai
            except (
                ImportError
            ) as exc:  # pragma: no cover - exercised only where the extra is absent
                raise ProviderError(
                    "the openai SDK is not installed; pip install 'trading-research-agent[openai]'"
                ) from exc
            options: dict[str, Any] = {}
            if base_url is not None:
                options["base_url"] = base_url
            key = os.environ.get(key_env) if key_env else None
            if key:
                options["api_key"] = key
            elif key_env:
                raise ProviderError(f"{key_env} is not set in the environment")
            elif base_url is not None:
                # A local or keyless server: the client library insists on a value.
                options["api_key"] = "not-needed"
            client = openai.OpenAI(**options)
        self.model = model
        self.base_url = base_url
        self.structured = structured
        self.max_tokens = max_tokens
        self._client = client

    def request(self, *, system: str, user: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        if self.structured == "json_schema":
            response_format: dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {"name": "gate", "strict": True, "schema": dict(schema)},
            }
        else:
            # JSON mode only: the schema travels in the prompt and the caller validates.
            response_format = {"type": "json_object"}
            schema_text = json.dumps(dict(schema), sort_keys=True)
            system = (
                system
                + "\n\nAnswer with one JSON object matching this schema exactly:\n"
                + schema_text
            )
        return {
            "model": self.model,
            "max_completion_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": response_format,
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
