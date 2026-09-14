"""Endpoints that speak the OpenAI chat API. One adapter, many vendors.

Every base URL and key variable below was read from the vendor's own documentation on
2026-09-13. `structured` says what the vendor documents: `json_schema` when the vendor
documents strict schema output, `json_object` when it documents JSON mode only (the schema is
then put in the system prompt and the result is validated by the caller, as it always is).
A vendor whose URL depends on the account (region, workspace) has `base_url = None` and needs
`--base-url`; a local server needs it too.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    name: str
    base_url: str | None
    key_env: str | None
    structured: str
    note: str

    def __post_init__(self) -> None:
        if self.structured not in ("json_schema", "json_object"):
            raise ValueError("structured is json_schema or json_object")


PRESETS: dict[str, Preset] = {
    p.name: p
    for p in (
        Preset("openai", None, "OPENAI_API_KEY", "json_schema", "the SDK's own default endpoint"),
        Preset("deepseek", "https://api.deepseek.com", "DEEPSEEK_API_KEY", "json_object", ""),
        Preset("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "json_object", ""),
        Preset("xai", "https://api.x.ai/v1", "XAI_API_KEY", "json_object", ""),
        Preset("mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY", "json_schema", ""),
        Preset(
            "openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "json_object", ""
        ),
        Preset(
            "qwen",
            None,
            "DASHSCOPE_API_KEY",
            "json_object",
            "the compatible-mode URL depends on region and workspace; pass --base-url",
        ),
        Preset(
            "ollama",
            "http://localhost:11434/v1",
            None,
            "json_object",
            "a key is required by the client library and ignored by the server",
        ),
        Preset(
            "9router",
            "http://localhost:20128/v1",
            "9ROUTER_API_KEY",
            "json_schema",
            "a local router; the model is a combo or provider/model name from its dashboard",
        ),
        Preset(
            "local",
            None,
            None,
            "json_object",
            "any OpenAI-compatible server; pass --base-url (https, or http on localhost)",
        ),
    )
}


def preset(name: str) -> Preset:
    try:
        return PRESETS[name.strip().lower()]
    except KeyError:
        raise KeyError(f"no preset named {name!r}; have {', '.join(sorted(PRESETS))}") from None
