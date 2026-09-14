"""Google adapter, on the official SDK (`pip install 'trading-research-agent[google]'`).

Read from the `google-genai` README and `types.py` on 2026-09-13: `genai.Client()` takes the
key from `GEMINI_API_KEY` or `GOOGLE_API_KEY`; JSON output is requested with
`response_mime_type="application/json"` and `response_json_schema`; the system prompt goes in
`system_instruction`; token counts are `usage_metadata.prompt_token_count`,
`candidates_token_count`, `cached_content_token_count` and `thoughts_token_count`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trading_research_agent.agent.completer import Completion
from trading_research_agent.agent.providers.anthropic import ProviderError


class GoogleCompleter:
    def __init__(self, model: str, *, client: Any = None, max_tokens: int = 4096) -> None:
        if not model.strip():
            raise ValueError("a Google model name is required")
        if client is None:
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - exercised only without the extra
                raise ProviderError(
                    "the google-genai SDK is not installed; "
                    "pip install 'trading-research-agent[google]'"
                ) from exc
            client = genai.Client()
        self.model = model
        self.max_tokens = max_tokens
        self._client = client

    def request(self, *, system: str, user: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "model": self.model,
            "contents": user,
            "config": {
                "system_instruction": system,
                "max_output_tokens": self.max_tokens,
                "response_mime_type": "application/json",
                "response_json_schema": dict(schema),
            },
        }

    def complete(self, *, system: str, user: str, schema: Mapping[str, Any]) -> Completion:
        try:
            response = self._client.models.generate_content(
                **self.request(system=system, user=user, schema=schema)
            )
        except Exception as exc:
            raise ProviderError(f"{exc.__class__.__name__}: {exc}") from exc
        text = getattr(response, "text", None)
        if not text:
            raise ProviderError("no text in the response")
        usage = getattr(response, "usage_metadata", None)
        prompt = int(getattr(usage, "prompt_token_count", 0) or 0)
        cached = int(getattr(usage, "cached_content_token_count", 0) or 0)
        answer = int(getattr(usage, "candidates_token_count", 0) or 0)
        thoughts = int(getattr(usage, "thoughts_token_count", 0) or 0)
        return Completion(
            text=str(text),
            model=self.model,
            input_tokens=prompt + cached,
            output_tokens=answer + thoughts,
        )
