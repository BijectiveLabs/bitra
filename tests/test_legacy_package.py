"""The package's old name installs bitra at the same version, with the same extras."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def load(path: Path) -> dict[str, Any]:
    project: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
    return project


def test_the_old_name_pins_bitra_at_the_same_version_with_every_extra() -> None:
    main = load(ROOT / "pyproject.toml")
    legacy = load(ROOT / "packaging" / "legacy" / "pyproject.toml")
    version = main["version"]
    assert main["name"] == "bitra" and legacy["name"] == "trading-research-agents"
    assert legacy["version"] == version
    assert legacy["dependencies"] == [f"bitra=={version}"]
    extras = sorted(k for k in main["optional-dependencies"] if k != "dev")
    assert sorted(legacy["optional-dependencies"]) == extras
    for extra in extras:
        assert legacy["optional-dependencies"][extra] == [f"bitra[{extra}]=={version}"]


def test_every_command_name_runs_the_same_entry_point() -> None:
    scripts = load(ROOT / "pyproject.toml")["scripts"]
    assert set(scripts) == {"tra", "bitra", "trading-research-agents"}
    assert len(set(scripts.values())) == 1
