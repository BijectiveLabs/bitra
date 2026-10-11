"""Vendor SDK adapters and the provider registry. The only place a model client is imported.

Each adapter is constructed with an optional client so that tests can pass a fake and never
touch the network. Keys come from the environment through the SDKs' own defaults and are
never written anywhere by this package.
"""

from trading_research_agents.llm.completer import ProviderError
from trading_research_agents.llm.providers.anthropic import AnthropicCompleter
from trading_research_agents.llm.providers.google import GoogleCompleter
from trading_research_agents.llm.providers.openai import OpenAICompleter
from trading_research_agents.llm.providers.presets import PRESETS, Preset, preset
from trading_research_agents.llm.providers.registry import (
    CallOptions,
    Provider,
    UnknownProviderError,
    provider,
    providers,
    register,
)

__all__ = [
    "PRESETS",
    "AnthropicCompleter",
    "CallOptions",
    "GoogleCompleter",
    "OpenAICompleter",
    "Preset",
    "Provider",
    "ProviderError",
    "UnknownProviderError",
    "preset",
    "provider",
    "providers",
    "register",
]
