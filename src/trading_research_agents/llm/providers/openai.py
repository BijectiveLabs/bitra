"""OpenAI adapter, on the official SDK (`pip install 'trading-research-agents[openai]'`).

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

from trading_research_agents.llm.completer import Completion, ProviderError

BASE_URL_ENV = "TRA_OPENAI_BASE_URL"
# reasoning_effort, as the Chat Completions API takes it. The two levels above high in our own
# vocabulary have nowhere to go on this API, so they land on high.
REASONING = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


class OpenAICompleter:
    def __init__(
        self,
        model: str,
        *,
        client: Any = None,
        max_tokens: int | None = None,
        base_url: str | None = None,
        key_env: str | None = "OPENAI_API_KEY",
        structured: str = "json_schema",
        effort: str | None = None,
        timeout_s: float = 60.0,
        max_retries: int = 2,
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
                    "the openai SDK is not installed; pip install 'trading-research-agents[openai]'"
                ) from exc
            # The declared timeout is per request; the SDK retries 429s and 5xx with backoff.
            options: dict[str, Any] = {"timeout": timeout_s, "max_retries": max_retries}
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
        self.effort = None if effort is None else REASONING.get(effort)
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
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": response_format,
        }
        if self.max_tokens is not None:
            body["max_completion_tokens"] = self.max_tokens
        if self.effort is not None:
            # Only when the operator asked for it: a model with no reasoning to configure rejects
            # the field outright, and most of the servers behind base_url are those models.
            body["reasoning_effort"] = self.effort
        return body

    def complete(self, *, system: str, user: str, schema: Mapping[str, Any]) -> Completion:
        try:
            response = self._client.chat.completions.create(
                **self.request(system=system, user=user, schema=schema)
            )
        except Exception as exc:
            if self.structured == "json_schema" and _refuses_schema(exc):
                # A server or a model that cannot enforce a schema says so; JSON mode with the
                # schema in the prompt is what it can do, and the caller still validates.
                self.structured = "json_object"
                return self.complete(system=system, user=user, schema=schema)
            # A connection drop or a vendor-side error is a failed call, never a crash.
            raise ProviderError.from_vendor(exc) from exc
        choice = response.choices[0]
        if getattr(choice.message, "refusal", None):
            raise ProviderError("the model declined the request")
        text = choice.message.content
        if not text:
            raise ProviderError("no content in the response")
        # A router or a local server may answer without a usage block; the call is then
        # metered by count and time only, and the usage line says so through the zeros.
        usage = response.usage
        # Read where the server reports it, so whether a prompt cache engages is measured; the
        # system prompt is the same on every call and is most of every input.
        details = getattr(usage, "prompt_tokens_details", None)
        return Completion(
            text=str(text),
            model=str(response.model),
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            cached_tokens=int(getattr(details, "cached_tokens", 0) or 0),
        )


def _refuses_schema(exc: Exception) -> bool:
    """A 400 about the structured-output request itself, not about the call in general."""
    said = str(exc).lower()
    status = getattr(exc, "status_code", None)
    return (status in (None, 400, 422)) and any(
        word in said for word in ("response_format", "json_schema", "structured output")
    )
