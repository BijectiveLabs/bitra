"""Adapters are tested on what they send and how they read a reply. No network, no keys."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from trading_research_agent.agent.providers import AnthropicCompleter, OpenAICompleter
from trading_research_agent.agent.providers.anthropic import ProviderError
from trading_research_agent.agent.roles import BOUND_SCHEMA


class FakeMessages:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


def anthropic_response(text: str, *, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        model="claude-opus-5",
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=0,
        ),
    )


def test_anthropic_request_caches_the_doctrine_and_asks_for_the_schema() -> None:
    messages = FakeMessages(anthropic_response('{"magnitude_bps": "3.0"}'))
    completer = AnthropicCompleter(client=SimpleNamespace(messages=messages))
    completion = completer.complete(system="DOCTRINE", user="Idea: x", schema=BOUND_SCHEMA)
    sent = messages.kwargs
    assert sent["model"] == "claude-opus-5"
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert sent["system"][0]["text"] == "DOCTRINE"
    assert sent["messages"] == [{"role": "user", "content": "Idea: x"}]
    assert sent["output_config"]["format"] == {"type": "json_schema", "schema": BOUND_SCHEMA}
    assert sent["output_config"]["effort"] == "high"
    assert "thinking" not in sent
    assert completion.text == '{"magnitude_bps": "3.0"}'
    assert completion.model == "claude-opus-5"
    assert completion.input_tokens == 1000 and completion.output_tokens == 20


def test_anthropic_refusal_and_missing_text_are_errors() -> None:
    declined = FakeMessages(anthropic_response("", stop_reason="refusal"))
    with pytest.raises(ProviderError, match="declined"):
        AnthropicCompleter(client=SimpleNamespace(messages=declined)).complete(
            system="s", user="u", schema=BOUND_SCHEMA
        )
    empty = anthropic_response("x")
    empty.content = []
    with pytest.raises(ProviderError, match="no text block"):
        AnthropicCompleter(client=SimpleNamespace(messages=FakeMessages(empty))).complete(
            system="s", user="u", schema=BOUND_SCHEMA
        )


def openai_response(content: str | None, *, refusal: str | None = None) -> Any:
    return SimpleNamespace(
        model="gpt-x",
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, refusal=refusal))],
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=10),
    )


def test_openai_request_is_a_strict_json_schema_and_needs_a_model_name() -> None:
    messages = FakeMessages(openai_response('{"accepted": true, "objection": ""}'))
    client = SimpleNamespace(chat=SimpleNamespace(completions=messages))
    completer = OpenAICompleter("gpt-x", client=client)
    completion = completer.complete(system="DOCTRINE", user="Idea: x", schema=BOUND_SCHEMA)
    sent = messages.kwargs
    assert sent["model"] == "gpt-x"
    assert sent["messages"][0] == {"role": "system", "content": "DOCTRINE"}
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["response_format"]["json_schema"]["schema"] == BOUND_SCHEMA
    assert completion.input_tokens == 50 and completion.output_tokens == 10
    with pytest.raises(ValueError, match="model name is required"):
        OpenAICompleter("  ", client=client)
    with pytest.raises(ProviderError, match="declined"):
        OpenAICompleter(
            "gpt-x",
            client=SimpleNamespace(
                chat=SimpleNamespace(completions=FakeMessages(openai_response(None, refusal="no")))
            ),
        ).complete(system="s", user="u", schema=BOUND_SCHEMA)


def test_openai_base_url_reaches_a_compatible_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_research_agent.agent.providers.openai import BASE_URL_ENV, OpenAICompleter

    class Client:
        pass

    monkeypatch.setenv(BASE_URL_ENV, "http://localhost:11434/v1")
    completer = OpenAICompleter("local-model", client=Client())
    assert completer.base_url == "http://localhost:11434/v1"
    monkeypatch.delenv(BASE_URL_ENV)
    with pytest.raises(ProviderError, match="https"):
        OpenAICompleter("m", client=Client(), base_url="http://example.com/v1")
    assert OpenAICompleter("m", client=Client()).base_url is None
