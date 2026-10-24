"""The names the package answers to. The old name is the same package, built by the release."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_every_command_name_runs_the_same_entry_point() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == "bitra"
    scripts = project["scripts"]
    assert set(scripts) == {"tra", "bitra", "trading-research-agents"}
    assert len(set(scripts.values())) == 1


def test_the_old_name_is_built_from_this_source() -> None:
    build = (ROOT / "scripts" / "build_old_name.sh").read_text(encoding="utf-8")
    assert 's/^name = "bitra"$/name = "trading-research-agents"/' in build
    for workflow in ("ci.yml", "release.yml"):
        text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
        assert "bash scripts/build_old_name.sh" in text
