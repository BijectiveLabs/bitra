"""Providers are plugins. `tra agent ... --provider NAME` takes any registered name.

Three adapters ship: Anthropic (native SDK), Google (native SDK), and OpenAI (native SDK), the
last of which also serves every vendor in `presets.py` that speaks the same API. A vendor
that needs its own client registers under the `trading_research_agent.providers` entry point
or by calling `register`, and is then a name like any other.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points

from trading_research_agent.agent.completer import Completer
from trading_research_agent.agent.providers.presets import PRESETS

ENTRY_POINT_GROUP = "trading_research_agent.providers"


class UnknownProviderError(KeyError):
    """No provider with that name is registered."""


@dataclass(frozen=True)
class Provider:
    """What the CLI needs to know: the name, which key it reads, and how to build a completer."""

    name: str
    key_env: str | None
    default_model: str | None
    accepts_base_url: bool
    build: Callable[[str | None, str | None], Completer]
    note: str = ""
    build_with: Callable[[str | None, str | None, int], Completer] | None = None


_registry: dict[str, Provider] = {}
_discovered = False


def register(provider: Provider) -> None:
    name = provider.name.strip().lower()
    if not name:
        raise ValueError("a provider names itself")
    _registry[name] = provider


def _anthropic(model: str | None, base_url: str | None, max_tokens: int = 4096) -> Completer:
    from trading_research_agent.agent.providers.anthropic import DEFAULT_MODEL, AnthropicCompleter

    if base_url:
        raise ValueError("--base-url does not apply to the anthropic provider")
    return AnthropicCompleter(model or DEFAULT_MODEL, max_tokens=max_tokens)


def _google(model: str | None, base_url: str | None, max_tokens: int = 4096) -> Completer:
    from trading_research_agent.agent.providers.google import GoogleCompleter

    if base_url:
        raise ValueError("--base-url does not apply to the google provider")
    if not model:
        raise ValueError("--model is required for the google provider")
    return GoogleCompleter(model, max_tokens=max_tokens)


def _compatible(name: str) -> Callable[[str | None, str | None, int], Completer]:
    def build(model: str | None, base_url: str | None, max_tokens: int = 4096) -> Completer:
        from trading_research_agent.agent.providers.openai import OpenAICompleter

        p = PRESETS[name]
        if not model:
            raise ValueError(f"--model is required for the {name} provider")
        url = base_url or p.base_url
        if url is None and name != "openai":
            raise ValueError(f"the {name} provider needs --base-url: {p.note}")
        return OpenAICompleter(
            model, base_url=url, key_env=p.key_env, structured=p.structured, max_tokens=max_tokens
        )

    return build


def _two_args(
    build: Callable[[str | None, str | None, int], Completer],
) -> Callable[[str | None, str | None], Completer]:
    def plain(model: str | None, base_url: str | None) -> Completer:
        return build(model, base_url, 4096)

    return plain


def _bundled() -> None:
    from trading_research_agent.agent.providers.anthropic import DEFAULT_MODEL

    register(
        Provider("anthropic", "ANTHROPIC_API_KEY", DEFAULT_MODEL, False, _anthropic, "", _anthropic)
    )
    register(
        Provider("google", "GEMINI_API_KEY", None, False, _google, "or GOOGLE_API_KEY", _google)
    )
    for name, p in PRESETS.items():
        register(
            Provider(
                name, p.key_env, None, True, _two_args(_compatible(name)), p.note, _compatible(name)
            )
        )


def _discover() -> None:
    global _discovered
    if _discovered:
        return
    _discovered = True
    _bundled()
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        plugin = ep.load()
        register(plugin() if isinstance(plugin, type) else plugin)


def provider(name: str) -> Provider:
    _discover()
    try:
        return _registry[name.strip().lower()]
    except KeyError:
        raise UnknownProviderError(
            f"no provider named {name!r}; registered: {', '.join(sorted(_registry))}"
        ) from None


def providers() -> tuple[str, ...]:
    _discover()
    return tuple(sorted(_registry))
