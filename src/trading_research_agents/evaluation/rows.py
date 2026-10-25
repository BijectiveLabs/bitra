"""Arena rows by name: the fixed baselines, the v1 record, and any policy a package registers under
the `trading_research_agents.policies` entry point group, as a callable that returns a policy."""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any

from trading_research_agents.evaluation.baselines import BuyHold, Fade, Momentum, RandomSide
from trading_research_agents.evaluation.policy import Policy

ENTRY_POINT_GROUP = "trading_research_agents.policies"
RANDOM = "random"
V1_RECORD = "v1_record"
BASELINES = ("buy_hold", "momentum", "fade", RANDOM)


class UnknownRowError(ValueError):
    """A row name that is neither a baseline, the v1 record, nor a registered policy."""


def _plugins() -> dict[str, Callable[[], Any]]:
    return {ep.name: ep.load() for ep in entry_points(group=ENTRY_POINT_GROUP)}


def make_policy(name: str, *, seed: int, end_ns: int, interval_ns: int) -> Policy:
    if name == "buy_hold":
        return BuyHold(end_ns, interval_ns)
    if name == "momentum":
        return Momentum()
    if name == "fade":
        return Fade()
    if name == RANDOM:
        return RandomSide(seed)
    plugins = _plugins()
    factory = plugins.get(name)
    if factory is None:
        known = ", ".join([*BASELINES, V1_RECORD, *sorted(plugins)])
        raise UnknownRowError(f"no row named {name!r}; have {known}")
    made = factory()
    named = isinstance(getattr(made, "name", None), str)
    if not named or not callable(getattr(made, "decide", None)):
        raise UnknownRowError(f"{name!r} is registered but is not a policy (name and decide)")
    policy: Policy = made
    return policy
