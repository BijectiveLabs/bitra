"""Vendor SDK adapters. The only place a model provider's client is imported.

Each adapter is constructed with an optional client so that tests can pass a fake and never
touch the network. Keys come from the environment through the SDKs' own defaults and are
never written anywhere by this package.
"""

from trading_research_agent.agent.providers.anthropic import AnthropicCompleter
from trading_research_agent.agent.providers.openai import OpenAICompleter

__all__ = ["AnthropicCompleter", "OpenAICompleter"]
