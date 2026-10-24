"""How to add an optional dependency, said the same way wherever one is missing."""

from __future__ import annotations

BASE = ("ccxt", "openai", "anthropic", "google", "data")


def install_hint(extra: str) -> str:
    """The command that adds `extra` to an install made the way the quick start makes one."""
    extras = ",".join(dict.fromkeys([*BASE, extra]))
    return (
        f"add the `{extra}` extra: uv tool install 'bitra[{extras}]', "
        f"or pip install 'bitra[{extra}]' in your environment"
    )
