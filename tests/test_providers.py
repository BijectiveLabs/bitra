"""Adapters are tested on what they send and how they read a reply. No network, no keys."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from trading_research_agents.agents.roles import BOUND_SCHEMA
from trading_research_agents.llm.completer import ProviderError
from trading_research_agents.llm.providers import AnthropicCompleter, OpenAICompleter


class FakeMessages:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


def anthropic_response(text: str, *, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        model="claude-opus-5-5",
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
    assert sent["model"] == "claude-opus-5-5"
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert sent["system"][0]["text"] == "DOCTRINE"
    assert sent["messages"] == [{"role": "user", "content": "Idea: x"}]
    assert sent["output_config"]["format"] == {"type": "json_schema", "schema": BOUND_SCHEMA}
    assert sent["output_config"]["effort"] == "high"
    assert "thinking" not in sent
    assert completion.text == '{"magnitude_bps": "3.0"}'
    assert completion.model == "claude-opus-5-5"
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
    # A router that answers without a usage block: the call is counted, the tokens are zero,
    # and the usage line says so.
    without = openai_response('{"accepted": true, "objection": ""}')
    without.usage = None
    bare = OpenAICompleter(
        "gpt-x", client=SimpleNamespace(chat=SimpleNamespace(completions=FakeMessages(without)))
    ).complete(system="s", user="u", schema=BOUND_SCHEMA)
    assert bare.input_tokens == 0 and bare.output_tokens == 0 and bare.text
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
    from trading_research_agents.llm.providers.openai import BASE_URL_ENV, OpenAICompleter

    class Client:
        pass

    monkeypatch.setenv(BASE_URL_ENV, "http://localhost:11434/v1")
    completer = OpenAICompleter("local-model", client=Client())
    assert completer.base_url == "http://localhost:11434/v1"
    monkeypatch.delenv(BASE_URL_ENV)
    with pytest.raises(ProviderError, match="https"):
        OpenAICompleter("m", client=Client(), base_url="http://example.com/v1")
    assert OpenAICompleter("m", client=Client()).base_url is None


def test_effort_is_one_vocabulary_mapped_onto_what_each_vendor_takes() -> None:
    from trading_research_agents.llm.providers import CallOptions
    from trading_research_agents.llm.providers.google import GoogleCompleter

    # Anthropic takes low through max, so the one level below that lands on low.
    request = AnthropicCompleter(client=object(), effort="minimal").request(
        system="s", user="u", schema=BOUND_SCHEMA
    )
    assert request["output_config"]["effort"] == "low"
    kept = AnthropicCompleter(client=object(), effort="xhigh").request(
        system="s", user="u", schema=BOUND_SCHEMA
    )
    assert kept["output_config"]["effort"] == "xhigh"

    # Gemini takes a thinking level, and only when the operator asked for one.
    thinking = GoogleCompleter("gemini-x", client=object(), effort="max").request(
        system="s", user="u", schema=BOUND_SCHEMA
    )
    assert thinking["config"]["thinking_config"] == {"thinking_level": "HIGH"}
    plain = GoogleCompleter("gemini-x", client=object()).request(
        system="s", user="u", schema=BOUND_SCHEMA
    )
    assert "thinking_config" not in plain["config"]

    # Most servers behind base_url reject reasoning_effort outright, so it is sent only on request.
    asked = OpenAICompleter("gpt-x", client=object(), effort="xhigh").request(
        system="s", user="u", schema=BOUND_SCHEMA
    )
    assert asked["reasoning_effort"] == "high"
    quiet = OpenAICompleter("gpt-x", client=object()).request(
        system="s", user="u", schema=BOUND_SCHEMA
    )
    assert "reasoning_effort" not in quiet

    assert CallOptions(effort="medium").effort == "medium"
    with pytest.raises(ValueError, match="effort is one of"):
        CallOptions(effort="ludicrous")
