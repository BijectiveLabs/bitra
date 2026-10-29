"""Providers are plugins. `tra agent ... --provider NAME` takes any registered name.

Three adapters ship: Anthropic (native SDK), Google (native SDK), and OpenAI (native SDK), the
last of which also serves every vendor in `presets.py` that speaks the same API. A vendor
that needs its own client registers under the `trading_research_agents.providers` entry point
or by calling `register`, and is then a name like any other.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from importlib.util import find_spec
from typing import Any

from trading_research_agents.llm.completer import Completer
from trading_research_agents.llm.providers import typesafe
from trading_research_agents.llm.providers.presets import PRESETS

ENTRY_POINT_GROUP = "trading_research_agents.providers"
# A readiness check waits a few seconds for an answer, not a minute: it is run by a person.
PROBE_TIMEOUT_S = 10.0


class UnknownProviderError(KeyError):
    """No provider with that name is registered."""


# How hard a model is asked to think, in one vocabulary the three vendors are mapped onto. A role
# that only labels a series does not need what a role reasoning over sixty bars needs, and on a
# reasoning model that difference is most of the latency and most of the bill.
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class CallOptions:
    """What a run declares about each call: the output cap, the timeout, retries, and effort."""

    # None is the default and means no cap: the vendor's own maximum for the model. A cap that
    # cuts an answer off halfway costs the call and returns something unusable.
    max_output_tokens: int | None = None
    timeout_s: float = 60.0
    max_retries: int = 2
    effort: str | None = None

    def __post_init__(self) -> None:
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ValueError("call options: an output cap is positive when it is declared")
        if self.timeout_s <= 0 or self.max_retries < 0:
            raise ValueError("call options: a positive timeout, retries >= 0")
        if self.effort is not None and self.effort not in EFFORTS:
            raise ValueError(f"effort is one of {list(EFFORTS)}")


DEFAULT_OPTIONS = CallOptions()


@dataclass(frozen=True)
class Provider:
    """What the CLI needs to know: the name, which key it reads, and how to build a completer."""

    name: str
    key_env: str | None
    default_model: str | None
    accepts_base_url: bool
    build: Callable[[str | None, str | None], Completer]
    note: str = ""
    build_with: Callable[[str | None, str | None, CallOptions], Completer] | None = None
    # The SDK this provider talks through, and the extra that installs it. A missing SDK is a
    # thing to say at `tra check`, before a cycle has paid for data and briefs to find out.
    needs: tuple[str, str] = ("", "")
    # One authenticated call that costs nothing and sends no prompt: the vendor's own model list.
    # Empty means the key was accepted; anything else is what the vendor said, as it said it.
    verify: Callable[[str | None, str | None], str] | None = None

    @property
    def installed(self) -> bool:
        module, _ = self.needs
        if not module:
            return True
        try:
            return find_spec(module) is not None
        except (ImportError, ValueError):
            # find_spec("google.genai") imports "google" first and raises when it is not there.
            return False


_registry: dict[str, Provider] = {}
_discovered = False


def register(provider: Provider) -> None:
    name = provider.name.strip().lower()
    if not name:
        raise ValueError("a provider names itself")
    _registry[name] = provider


def _anthropic(
    model: str | None, base_url: str | None, options: CallOptions = DEFAULT_OPTIONS
) -> Completer:
    from trading_research_agents.llm.providers.anthropic import DEFAULT_MODEL, AnthropicCompleter

    if base_url:
        raise ValueError("--base-url does not apply to the anthropic provider")
    return AnthropicCompleter(
        model or DEFAULT_MODEL,
        max_tokens=options.max_output_tokens,
        effort=options.effort,
        timeout_s=options.timeout_s,
        max_retries=options.max_retries,
    )


def _google(
    model: str | None, base_url: str | None, options: CallOptions = DEFAULT_OPTIONS
) -> Completer:
    from trading_research_agents.llm.providers.google import GoogleCompleter

    if base_url:
        raise ValueError("--base-url does not apply to the google provider")
    if not model:
        raise ValueError("--model is required for the google provider")
    return GoogleCompleter(
        model,
        max_tokens=options.max_output_tokens,
        effort=options.effort,
        timeout_s=options.timeout_s,
        max_retries=options.max_retries,
    )


def _verified(build: Callable[[], Any]) -> str:
    """Run one free authenticated call and say what the vendor said. Never raises."""
    try:
        build()
    except Exception as exc:  # the vendor's own error is the answer, whatever kind it is
        return f"{exc.__class__.__name__}: {exc}"[:240]
    return ""


def _verify_anthropic(model: str | None, base_url: str | None) -> str:
    def call() -> Any:
        import anthropic

        return anthropic.Anthropic(timeout=PROBE_TIMEOUT_S, max_retries=0).models.list(limit=1)

    return _verified(call)


def _verify_google(model: str | None, base_url: str | None) -> str:
    def call() -> Any:
        from google import genai

        client = genai.Client(http_options={"timeout": int(PROBE_TIMEOUT_S * 1000)})
        return next(iter(client.models.list()), None)

    return _verified(call)


def _verify_compatible(name: str) -> Callable[[str | None, str | None], str]:
    def verify(model: str | None, base_url: str | None) -> str:
        def call() -> Any:
            import openai

            p = PRESETS[name]
            url = base_url or p.base_url
            options: dict[str, Any] = {"timeout": PROBE_TIMEOUT_S, "max_retries": 0}
            if url is not None:
                options["base_url"] = url
            key = os.environ.get(p.key_env) if p.key_env else None
            options["api_key"] = key or "not-needed"
            return openai.OpenAI(**options).models.list()

        return _verified(call)

    return verify


def _compatible(name: str) -> Callable[[str | None, str | None, CallOptions], Completer]:
    def build(
        model: str | None, base_url: str | None, options: CallOptions = DEFAULT_OPTIONS
    ) -> Completer:
        from trading_research_agents.llm.providers.openai import OpenAICompleter

        p = PRESETS[name]
        if not model:
            raise ValueError(f"--model is required for the {name} provider")
        url = base_url or p.base_url
        if url is None and name != "openai":
            raise ValueError(f"the {name} provider needs --base-url: {p.note}")
        return OpenAICompleter(
            model,
            base_url=url,
            key_env=p.key_env,
            structured=p.structured,
            max_tokens=options.max_output_tokens,
            effort=options.effort,
            timeout_s=options.timeout_s,
            max_retries=options.max_retries,
        )

    return build


def _two_args(
    build: Callable[[str | None, str | None, CallOptions], Completer],
) -> Callable[[str | None, str | None], Completer]:
    def plain(model: str | None, base_url: str | None) -> Completer:
        return build(model, base_url, DEFAULT_OPTIONS)

    return plain


# A model is a default only after it has run live cycles here; the others are named by the client.
TESTED_DEFAULTS = {"groq": "openai/gpt-oss-120b", "google": "gemini-flash-latest"}


def _bundled() -> None:
    from trading_research_agents.llm.providers.anthropic import DEFAULT_MODEL

    register(
        Provider(
            "anthropic",
            "ANTHROPIC_API_KEY",
            DEFAULT_MODEL,
            False,
            _anthropic,
            "",
            _anthropic,
            ("anthropic", "anthropic"),
            _verify_anthropic,
        )
    )
    register(
        Provider(
            "google",
            "GEMINI_API_KEY",
            TESTED_DEFAULTS["google"],
            False,
            _google,
            "or GOOGLE_API_KEY",
            _google,
            ("google.genai", "google"),
            _verify_google,
        )
    )
    for name, p in PRESETS.items():
        register(
            Provider(
                name,
                p.key_env,
                TESTED_DEFAULTS.get(name),
                True,
                _two_args(_compatible(name)),
                p.note,
                _compatible(name),
                ("openai", "openai"),
                _verify_compatible(name),
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
            + (f"; {typesafe.WHERE}" if name.strip().lower() == typesafe.NAME else "")
        ) from None


def providers() -> tuple[str, ...]:
    _discover()
    return tuple(sorted(_registry))


def model_ids(name: str, *, base_url: str | None = None) -> list[str]:
    """The models this machine's key can use on a provider, as the vendor lists them.

    One authenticated read of the vendor's own model list: no prompt is sent and nothing is spent.
    It is how an operator picks a model name without copying it from a web page.
    """
    found = provider(name)
    if found.name == "anthropic":
        import anthropic

        client = anthropic.Anthropic(timeout=PROBE_TIMEOUT_S, max_retries=0)
        return sorted(m.id for m in client.models.list())
    if found.name == "google":
        from google import genai

        # The client is held while the pages are read: a listing outlives a client it was asked of
        # only until the next page, and then reads through a closed connection.
        gclient = genai.Client(http_options={"timeout": int(PROBE_TIMEOUT_S * 1000)})
        return sorted(str(m.name).removeprefix("models/") for m in gclient.models.list() if m.name)
    if found.name in PRESETS:
        import openai

        p = PRESETS[found.name]
        url = base_url or p.base_url
        options: dict[str, Any] = {"timeout": PROBE_TIMEOUT_S, "max_retries": 0}
        if url is not None:
            options["base_url"] = url
        key = os.environ.get(p.key_env) if p.key_env else None
        options["api_key"] = key or "not-needed"
        oclient = openai.OpenAI(**options)
        return sorted(m.id for m in oclient.models.list())
    raise UnknownProviderError(f"{name} does not list its models through this package")
