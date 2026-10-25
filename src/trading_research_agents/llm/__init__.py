"""Everything that talks to a language model, and nothing that decides anything.

The agents are not the only caller: the auditors work outside the trading loop, and a host
may call a model through this package without any agent at all. So the client adapters, the
structured-output parser, the call budget and the usage ledger live here, one layer below
the roles that use them. This package imports no other part of the package; the direction is
always downward.
"""

from trading_research_agents.llm.cache import (
    CachedCompleter,
    CacheError,
    CacheMissError,
    CacheMode,
    CallIdentity,
    ResponseCache,
    Sampling,
)
from trading_research_agents.llm.completer import Completer, Completion, FakeCompleter
from trading_research_agents.llm.form import FormError, parse_object
from trading_research_agents.llm.providers import (
    PRESETS,
    AnthropicCompleter,
    CallOptions,
    GoogleCompleter,
    OpenAICompleter,
    Preset,
    Provider,
    ProviderError,
    UnknownProviderError,
    preset,
    provider,
    providers,
    register,
)
from trading_research_agents.llm.usage import (
    BudgetLimits,
    BudgetSpentError,
    MeteredCompleter,
    Pricing,
    UsageLedger,
    UsageRecord,
)

__all__ = [
    "PRESETS",
    "AnthropicCompleter",
    "BudgetLimits",
    "BudgetSpentError",
    "CacheError",
    "CacheMissError",
    "CacheMode",
    "CachedCompleter",
    "CallIdentity",
    "CallOptions",
    "Completer",
    "Completion",
    "FakeCompleter",
    "FormError",
    "GoogleCompleter",
    "MeteredCompleter",
    "OpenAICompleter",
    "Preset",
    "Pricing",
    "Provider",
    "ProviderError",
    "ResponseCache",
    "Sampling",
    "UnknownProviderError",
    "UsageLedger",
    "UsageRecord",
    "parse_object",
    "preset",
    "provider",
    "providers",
    "register",
]
