"""Dependency direction and the adoption rules, checked from the source rather than trusted.

ledger and envelope are vocabulary and import nothing else from this project. Arrows only
point down the list in docs.bijectivelabs.dev/opensource/architecture. Nothing imports a language
model outside agent.
No strategy ships inside the package, and no core module names a venue: both live behind
the plugin directories or in examples/.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "trading_research_agents"
PACKAGE = "trading_research_agents"

GATES = {"ledger", "envelope", "data", "chain", "execution", "strategy", "trading"}

# How records reach the disk is beneath everything: any package may write one, and it imports none.
_ALLOWED: dict[str, set[str]] = {
    "records": set(),
    "ledger": set(),
    "envelope": set(),
    # The model layer sits beside the gates, not above them: it knows nothing about markets.
    "llm": set(),
    "data": {"ledger", "envelope"},
    # Bars are built from the trades the data layer reads, and nothing above them reaches down.
    "market_data": {"data"},
    # Readings are the operator's series against the operator's thresholds: data and nothing else.
    "readings": {"data"},
    # Features read bars and derivatives, and the regime rule and ATR the execution gate owns.
    "features": {"market_data", "execution"},
    # The one place a typed judge meets market data: archived text in, typed events out.
    "perception": {"llm", "market_data", "features"},
    # The harness replays features over the archive and prices ideas with the desk's own costs.
    "evaluation": {"features", "market_data", "data", "envelope", "execution"},
    "chain": {"ledger", "envelope", "data"},
    "execution": {"ledger", "envelope", "data", "chain"},
    "strategy": {"ledger", "envelope", "data", "chain", "execution"},
    "trading": {"ledger", "envelope", "data", "chain", "execution", "strategy"},
    "store": GATES | {"llm"},
    "surface": GATES,
    "agents": GATES | {"llm", "store", "surface", "readings"},
    "mcp": GATES | {"surface"},
    "cli": GATES
    | {
        "llm",
        "store",
        "surface",
        "agents",
        "mcp",
        "market_data",
        "features",
        "evaluation",
        "readings",
        "perception",
    },
}
# `extras` is one function that words a missing optional dependency; it imports nothing either.
BENEATH = {"records", "extras"}
ALLOWED = {name: deps | (BENEATH - {name}) for name, deps in _ALLOWED.items()}

LLM_CLIENTS = {"anthropic", "openai", "langchain", "langgraph", "litellm", "ollama", "typesafe"}

# Plugin directories are the only places a venue may be named in the package. Templates are
# copies of examples/ that `tra init` writes into a project; they are never imported.
PLUGIN_DIRS = {
    SRC / "data" / "venues",
    SRC / "data" / "sources",
    SRC / "trading" / "feeds",
    SRC / "trading" / "venues",
    SRC / "market_data" / "venues",
}
TEMPLATES = SRC / "templates"
EXAMPLES = SRC.parent.parent / "examples"
VENUE_NAMES = ("bybit", "binance", "hyperliquid", "okx", "coinbase", "deribit", "kraken")

# A decision core is a class with these methods, however it is spelled. Two classes carry the
# methods without carrying a rule about the market: the agent core, where a model decides
# from the operator's mechanism, and the config placeholder that stands in for it.
STRATEGY_METHODS = {"on_bar"}
MECHANISMS = {"agents/strategy.py:AgentStrategy", "trading/config.py:_Pending"}
FITTED_CORE_METHODS = {"fit", "entry", "exit", "mark"}


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
def test_only_the_llm_layer_talks_to_a_language_model(subpackage: str) -> None:
    for path in (SRC / subpackage).rglob("*.py"):
        if subpackage == "llm" and path.parent.name == "providers":
            continue
        roots = {n.split(".")[0] for n in _imports(path)}
        assert roots.isdisjoint(LLM_CLIENTS), f"{path}: vendor SDKs live only in llm/providers"


def test_nothing_reaches_into_a_private_platform() -> None:
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "Bijective_Core" not in text and "bijective_core" not in text, path


def test_source_is_ascii() -> None:
    for path in SRC.rglob("*.py"):
        raw = path.read_bytes()
        assert raw.isascii(), f"{path} contains non-ASCII bytes"


def _classes(path: Path) -> list[ast.ClassDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]


def _is_protocol(cls: ast.ClassDef) -> bool:
    for base in cls.bases:
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        if isinstance(base, ast.Subscript):
            inner = base.value
            name = inner.attr if isinstance(inner, ast.Attribute) else getattr(inner, "id", "")
        if name == "Protocol":
            return True
    return False


def _methods(cls: ast.ClassDef) -> set[str]:
    return {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}


def _names_itself(cls: ast.ClassDef) -> bool:
    # The Strategy protocol is `name` plus `on_bar`; a bar handler alone is not a strategy.
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == "name":
            return True
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == "name" for t in targets):
            return True
    return False


def test_no_strategy_ships_inside_the_package() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        for cls in _classes(path):
            if _is_protocol(cls):
                continue
            methods = _methods(cls)
            strategy = methods >= STRATEGY_METHODS and _names_itself(cls)
            if strategy or methods >= FITTED_CORE_METHODS:
                label = f"{path.relative_to(SRC).as_posix()}:{cls.name}"
                if label not in MECHANISMS:
                    offenders.append(label)
    assert not offenders, f"decision cores belong in examples/ or your own package: {offenders}"


def test_no_core_module_names_a_venue() -> None:
    offenders = []
    for path in SRC.rglob("*"):
        if path.suffix not in {".py", ".md"} or any(d in path.parents for d in PLUGIN_DIRS):
            continue
        if TEMPLATES in path.parents:
            continue
        text = path.read_text(encoding="utf-8").lower()
        hits = [v for v in VENUE_NAMES if v in text]
        if hits:
            offenders.append(f"{path.relative_to(SRC)}: {hits}")
    assert not offenders, f"venues are plugins; core modules stay venue-neutral: {offenders}"


def test_examples_are_not_importable_from_the_package() -> None:
    examples = SRC.parent.parent / "examples"
    assert not (examples / "__init__.py").exists()
    for path in SRC.rglob("*.py"):
        assert "examples." not in " ".join(_imports(path)), f"{path} imports an example"


def test_templates_are_the_examples_with_the_core_beside_the_config() -> None:
    same = {
        "envelope.toml": "envelope.toml",
        "mapping.toml": "mapping.toml",
        "observation.toml": "observation.toml",
        "data.toml": "data.toml",
        "sources/macro.toml": "sources/macro.toml",
        "sources/onchain.toml": "sources/onchain.toml",
        "sources/derivatives.toml": "sources/derivatives.toml",
        "sources/sentiment.toml": "sources/sentiment.toml",
        "sources/news.toml": "sources/news.toml",
    }
    for template, example in same.items():
        assert (TEMPLATES / template).read_text(encoding="utf-8") == (EXAMPLES / example).read_text(
            encoding="utf-8"
        ), template
    config = (TEMPLATES / "strategy.toml").read_text(encoding="utf-8")
    example = (EXAMPLES / "strategy.toml").read_text(encoding="utf-8")
    assert (
        config.replace(
            '"strategies/trailing_zscore.py"            ',
            '"examples/strategies/trailing_zscore.py"   ',
        )
        == example
    )
    core = (TEMPLATES / "core.py.tmpl").read_text(encoding="utf-8")
    source = (EXAMPLES / "strategies" / "trailing_zscore.py").read_text(encoding="utf-8")
    assert core.replace('file = "strategies/', 'file = "examples/strategies/') == source
    assert not (TEMPLATES / "__init__.py").exists()
    assert not list(TEMPLATES.glob("*.py")), "a template is not an importable module"
