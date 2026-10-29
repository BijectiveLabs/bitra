"""Providers as plugins, the seven roles' contracts, and every model call written down."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from trading_research_agents.agents import (
    RoleError,
    auditor,
    structure_lead,
)
from trading_research_agents.agents.roles import prompt
from trading_research_agents.llm import Completion, FakeCompleter
from trading_research_agents.llm.completer import ProviderError
from trading_research_agents.llm.providers import (
    PRESETS,
    GoogleCompleter,
    OpenAICompleter,
    UnknownProviderError,
    preset,
    provider,
    providers,
)
from trading_research_agents.llm.usage import (
    BudgetLimits,
    BudgetSpentError,
    MeteredCompleter,
    Pricing,
    UsageLedger,
)

ROLES = (
    structure_lead,
    auditor,
)


# Providers


def test_the_registry_lists_the_bundled_providers_and_refuses_unknown_ones() -> None:
    names = providers()
    for expected in ("anthropic", "google", "openai", "deepseek", "groq", "mistral", "ollama"):
        assert expected in names
    with pytest.raises(UnknownProviderError, match="registered"):
        provider("no-such-vendor")
    assert provider("DeepSeek").key_env == "DEEPSEEK_API_KEY"
    assert provider("anthropic").default_model and not provider("anthropic").accepts_base_url


def test_presets_carry_verified_endpoints_and_their_structured_mode() -> None:
    assert preset("mistral").base_url == "https://api.mistral.ai/v1"
    assert preset("mistral").structured == "json_schema"
    assert preset("deepseek").base_url == "https://api.deepseek.com"
    assert preset("groq").base_url == "https://api.groq.com/openai/v1"
    assert preset("xai").base_url == "https://api.x.ai/v1"
    assert preset("openrouter").base_url == "https://openrouter.ai/api/v1"
    assert (
        preset("ollama").base_url == "http://localhost:11434/v1"
        and preset("ollama").key_env is None
    )
    assert preset("qwen").base_url is None and "base-url" in preset("qwen").note
    assert preset("9router").base_url == "http://localhost:20128/v1"
    assert preset("9router").key_env == "9ROUTER_API_KEY"
    assert preset("9router").structured == "json_schema"
    for p in PRESETS.values():
        assert p.base_url is None or p.base_url.startswith(("https://", "http://localhost"))
    with pytest.raises(KeyError, match="no preset"):
        preset("nope")


def test_a_preset_without_a_fixed_url_needs_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="needs --base-url"):
        provider("qwen").build("qwen-plus", None)
    with pytest.raises(ValueError, match="--model is required"):
        provider("groq").build(None, None)
    with pytest.raises(ValueError, match="does not apply"):
        provider("anthropic").build(None, "https://x")


def test_json_object_vendors_get_the_schema_in_the_prompt() -> None:
    class Client:
        pass

    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    strict = OpenAICompleter("m", client=Client(), structured="json_schema")
    loose = OpenAICompleter("m", client=Client(), structured="json_object")
    assert (
        strict.request(system="S", user="U", schema=schema)["response_format"]["type"]
        == "json_schema"
    )
    request = loose.request(system="S", user="U", schema=schema)
    assert request["response_format"] == {"type": "json_object"}
    assert json.dumps(schema, sort_keys=True) in request["messages"][0]["content"]
    with pytest.raises(ValueError, match="json_schema or json_object"):
        OpenAICompleter("m", client=Client(), structured="yaml")


def test_openai_reads_the_vendor_key_variable_and_refuses_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai = pytest.importorskip("openai")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="GROQ_API_KEY is not set"):
        OpenAICompleter("m", base_url=None, key_env="GROQ_API_KEY")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    completer = OpenAICompleter("m", base_url=preset("groq").base_url, key_env="GROQ_API_KEY")
    assert isinstance(completer._client, openai.OpenAI)
    local = OpenAICompleter("m", base_url="http://localhost:11434/v1", key_env=None)
    assert local.base_url == "http://localhost:11434/v1"


def test_google_adapter_requests_json_and_counts_every_token() -> None:
    captured: dict[str, Any] = {}

    class Models:
        def generate_content(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(
                text='{"a": "b"}',
                usage_metadata=SimpleNamespace(
                    prompt_token_count=10,
                    cached_content_token_count=5,
                    candidates_token_count=3,
                    thoughts_token_count=2,
                ),
            )

    completer = GoogleCompleter("gemini-x", client=SimpleNamespace(models=Models()))
    out = completer.complete(system="S", user="U", schema={"type": "object"})
    # The prompt count already holds the five cached tokens; they are reported, not added again.
    assert out.text == '{"a": "b"}' and out.input_tokens == 10 and out.output_tokens == 5
    assert out.cached_tokens == 5
    config = captured["config"]
    assert config["response_mime_type"] == "application/json"
    assert config["response_json_schema"] == {"type": "object"}
    assert config["system_instruction"] == "S" and captured["contents"] == "U"
    with pytest.raises(ValueError, match="model name"):
        GoogleCompleter(" ", client=SimpleNamespace())


# Roles


def test_every_role_has_a_prompt_a_schema_and_a_parser() -> None:
    for role in ROLES:
        text = prompt(role.ROLE)
        assert "JSON object" in text and text.isascii()
        assert role.SCHEMA["type"] == "object" and role.SCHEMA["additionalProperties"] is False
        assert set(role.SCHEMA["required"]) == set(role.SCHEMA["properties"])


@pytest.mark.parametrize(
    ("role", "good", "bad", "message"),
    [
        (
            structure_lead,
            {
                "target": "long",
                "setup": "range_reversion",
                "reason": "z below entry",
                "figures_used": [{"figure": "zscore", "value": "-2.1"}],
                "confidence": 0.7,
                "horizon_bars": 12,
            },
            {
                "target": "buy",
                "setup": "range_reversion",
                "reason": "x",
                "figures_used": [],
                "confidence": 0.7,
                "horizon_bars": 12,
            },
            "target must be one of",
        ),
        (
            structure_lead,
            {
                "target": "flat",
                "setup": "no_setup",
                "reason": "nothing",
                "figures_used": [],
                "confidence": 0.0,
                "horizon_bars": 1,
            },
            {
                "target": "flat",
                "setup": "no_setup",
                "reason": "x",
                "figures_used": [],
                "confidence": 1.5,
                "horizon_bars": 1,
            },
            "in \\[0, 1\\]",
        ),
        (
            auditor,
            {"findings": [], "verdict": "sound", "summary": "s"},
            {"findings": [], "verdict": "great", "summary": "s"},
            "verdict must be one of",
        ),
    ],
)
def test_role_parsers_accept_the_schema_and_refuse_the_rest(
    role: Any, good: dict[str, Any], bad: dict[str, Any], message: str
) -> None:
    parsed = role.parse(json.dumps(good))
    assert parsed is not None
    with pytest.raises(RoleError, match=message):
        role.parse(json.dumps(bad))
    with pytest.raises(RoleError, match="not valid JSON"):
        role.parse("{")


# Usage and budget


def test_every_call_is_written_down_and_the_budget_refuses_before_the_call(
    tmp_path: Path,
) -> None:
    ledger = UsageLedger(tmp_path / "usage.jsonl")
    clock = [1000.0]
    inner = FakeCompleter(['{"a": 1}', '{"a": 2}', '{"a": 3}'])
    metered = MeteredCompleter(
        inner,
        role="structure_lead",
        provider="fake",
        limits=BudgetLimits(token_budget=40, max_calls_per_hour=2, min_interval_s=10),
        ledger=ledger,
        pricing={"fake": Pricing(Decimal("3"), Decimal("15"))},
        clock=lambda: clock[0],
    )
    first = metered.complete(system="s", user="u" * 10, schema={})
    assert first.text == '{"a": 1}'
    with pytest.raises(BudgetSpentError, match="minimum 10s"):
        metered.complete(system="s", user="u", schema={})
    clock[0] += 11
    metered.complete(system="s", user="u" * 10, schema={})
    clock[0] += 11
    with pytest.raises(BudgetSpentError, match="calls this hour"):
        metered.complete(system="s", user="u", schema={})
    rows = UsageLedger.read(tmp_path / "usage.jsonl")
    assert len(rows) == 2 and rows[0]["role"] == "structure_lead"
    assert rows[0]["input_tokens"] == 10 and rows[0]["output_tokens"] == 8
    assert Decimal(rows[0]["cost"]) == (Decimal(3) * 10 + Decimal(15) * 8) / Decimal(1_000_000)
    assert ledger.by_role()["structure_lead"]["calls"] == 2
    assert ledger.tokens == 36 and len(inner.calls) == 2

    clock[0] += 3600
    exhausted = MeteredCompleter(
        inner,
        role="structure_lead",
        provider="fake",
        limits=BudgetLimits(token_budget=1, max_calls_per_hour=10, min_interval_s=0),
        ledger=ledger,
        clock=lambda: clock[0],
    )
    with pytest.raises(BudgetSpentError, match="token budget spent"):
        exhausted.complete(system="s", user="u", schema={})


def test_a_call_without_usage_is_counted_and_says_so(tmp_path: Path) -> None:
    class Silent:
        def complete(self, **kwargs: Any) -> Completion:
            return Completion(text="{}", model="router", input_tokens=0, output_tokens=0)

    ledger = UsageLedger(tmp_path / "usage.jsonl")
    metered = MeteredCompleter(
        Silent(),
        role="auditor",
        provider="router",
        limits=BudgetLimits(token_budget=100, max_calls_per_hour=10, min_interval_s=0),
        ledger=ledger,
        clock=lambda: 0.0,
    )
    metered.complete(system="s", user="u", schema={})
    row = UsageLedger.read(tmp_path / "usage.jsonl")[0]
    assert row["outcome"] == "ok, no usage reported" and row["input_tokens"] == 0
    assert ledger.by_role()["auditor"]["calls"] == 1


def test_a_failing_call_is_written_down_too(tmp_path: Path) -> None:
    class Broken:
        def complete(self, **kwargs: Any) -> Any:
            raise TimeoutError("slow")

    ledger = UsageLedger(tmp_path / "usage.jsonl")
    metered = MeteredCompleter(
        Broken(),
        role="auditor",
        provider="x",
        limits=BudgetLimits(token_budget=100, max_calls_per_hour=10, min_interval_s=0),
        ledger=ledger,
    )
    with pytest.raises(TimeoutError):
        metered.complete(system="s", user="u", schema={})
    assert UsageLedger.read(tmp_path / "usage.jsonl")[0]["outcome"] == "error: TimeoutError"
    with pytest.raises(ValueError, match="token_budget is positive when it is declared"):
        BudgetLimits(token_budget=0, max_calls_per_hour=1, min_interval_s=0)
    # Nothing declared is the default: every call is counted and none is refused.
    open_ended = BudgetLimits()
    assert not open_ended.capped and open_ended.call_timeout_s == 60
