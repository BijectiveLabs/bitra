"""Dependency direction between packages, checked from the source rather than trusted.

ledger and envelope are vocabulary and import nothing else from this project. Arrows only
point down the list in docs/architecture.md. Nothing imports a language model outside agent.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "trading_research_agent"
PACKAGE = "trading_research_agent"

ALLOWED: dict[str, set[str]] = {
    "ledger": set(),
    "envelope": set(),
    "data": {"ledger", "envelope"},
    "chain": {"ledger", "envelope", "data"},
    "execution": {"ledger", "envelope", "data", "chain"},
    "strategy": {"ledger", "envelope", "data", "chain", "execution"},
    "trading": {"ledger", "envelope", "data", "chain", "execution", "strategy"},
    "agent": {"ledger", "envelope", "data", "chain", "execution"},
    "mcp": {"ledger", "envelope", "data", "chain", "execution", "strategy", "trading"},
    "cli": {
        "ledger",
        "envelope",
        "data",
        "chain",
        "execution",
        "strategy",
        "trading",
        "agent",
        "mcp",
    },
}

LLM_CLIENTS = {"anthropic", "openai", "langchain", "langgraph", "litellm", "ollama"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _subpackages() -> list[str]:
    return sorted(p.name for p in SRC.iterdir() if p.is_dir() and (p / "__init__.py").exists())


@pytest.mark.parametrize("subpackage", _subpackages())
def test_subpackage_only_reaches_downward(subpackage: str) -> None:
    assert subpackage in ALLOWED, f"{subpackage} is not in the architecture; add it deliberately"
    for path in (SRC / subpackage).rglob("*.py"):
        for name in _imports(path):
            if not name.startswith(PACKAGE + "."):
                continue
            target = name.split(".")[1]
            if target == subpackage:
                continue
            reason = f"{path.name}: {subpackage} must not import {target}"
            assert target in ALLOWED[subpackage], reason


@pytest.mark.parametrize("subpackage", _subpackages())
def test_only_agent_talks_to_a_language_model(subpackage: str) -> None:
    for path in (SRC / subpackage).rglob("*.py"):
        if subpackage == "agent" and path.parent.name == "providers":
            continue
        roots = {n.split(".")[0] for n in _imports(path)}
        assert roots.isdisjoint(LLM_CLIENTS), f"{path}: vendor SDKs live only in agent/providers"


def test_nothing_reaches_into_a_private_platform() -> None:
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "Bijective_Core" not in text and "bijective_core" not in text, path


def test_source_is_ascii() -> None:
    for path in SRC.rglob("*.py"):
        raw = path.read_bytes()
        assert raw.isascii(), f"{path} contains non-ASCII bytes"
