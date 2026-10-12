"""Anthropic adapter, on the official SDK (`pip install 'trading-research-agents[anthropic]'`).

The system prompt is the frozen doctrine, so it is marked for prompt caching; the user turn
carries the idea and the feedback and is never cached. Structured output is requested with
the role's JSON schema, and the result is still validated by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trading_research_agents.extras import install_hint
from trading_research_agents.llm.completer import Completion, ProviderError

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
# This vendor requires max_tokens on every request, so an undeclared cap becomes this number rather
# than nothing. It is a field the API insists on, not a budget this software is imposing.
DEFAULT_MAX_TOKENS = 8192
# Read from the SDK's OutputConfigParam on 2026-09-29: low, medium, high, xhigh, max. Our own
# vocabulary carries one level this vendor does not take, so it is mapped rather than refused - a
# fallback chain that dies on a spelling is a chain that does not help on the day it is needed.
EFFORTS = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


class AnthropicCompleter:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        client: Any = None,
        max_tokens: int | None = None,
        effort: str | None = None,
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
                    f"the anthropic SDK is not installed; {install_hint('anthropic')}"
                ) from exc
            client = anthropic.Anthropic(timeout=timeout_s, max_retries=max_retries)
        self.model = model
        self.max_tokens = DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens
        self.effort = DEFAULT_EFFORT if effort is None else EFFORTS.get(effort, DEFAULT_EFFORT)
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
            raise ProviderError.from_vendor(exc) from exc
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
            cached_tokens=cached,
            cache_write_tokens=written,
        )
