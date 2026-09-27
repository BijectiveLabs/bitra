"""Anthropic adapter, on the official SDK (`pip install 'trading-research-agent[anthropic]'`).

The system prompt is the frozen doctrine, so it is marked for prompt caching; the user turn
carries the idea and the feedback and is never cached. Structured output is requested with
the role's JSON schema, and the result is still validated by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trading_research_agent.agent.completer import Completion

DEFAULT_MODEL = "claude-opus-5"


class ProviderError(RuntimeError):
    """The provider did not return a usable completion."""


class AnthropicCompleter:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        client: Any = None,
        max_tokens: int = 4096,
        effort: str = "high",
        timeout_s: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        if client is None:
            try:
                import anthropic
            except (
                ImportError
            ) as exc:  # pragma: no cover - exercised only where the extra is absent
                raise ProviderError(
                    "the anthropic SDK is not installed; "
                    "pip install 'trading-research-agent[anthropic]'"
                ) from exc
            client = anthropic.Anthropic(timeout=timeout_s, max_retries=max_retries)
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self._client = client

    def request(self, *, system: str, user: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": dict(schema)},
            },
        }

    def complete(self, *, system: str, user: str, schema: Mapping[str, Any]) -> Completion:
        try:
            response = self._client.messages.create(
                **self.request(system=system, user=user, schema=schema)
            )
        except Exception as exc:
            raise ProviderError(f"{exc.__class__.__name__}: {exc}") from exc
        if getattr(response, "stop_reason", None) == "refusal":
            raise ProviderError("the model declined the request")
        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
        if text is None:
            raise ProviderError("no text block in the response")
        usage = response.usage
        cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        written = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        return Completion(
            text=text,
            model=str(response.model),
            input_tokens=int(usage.input_tokens) + cached + written,
            output_tokens=int(usage.output_tokens),
        )
