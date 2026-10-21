"""`tra mandate --sign`: the mandate kept and signed in one step, and nothing else touched."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from trading_research_agents.cli.main import main
from trading_research_agents.cli.sign import SignError, sign
from trading_research_agents.execution.mandate import MANDATE_FILE, blocking, signed_in
from trading_research_agents.trading import load_strategy_config

DAY = date(2026, 10, 10)


def desk(tmp_path: Path) -> Path:
    assert main(["init", str(tmp_path / "desk"), "--agent", "groq", "--model", "m"]) == 0
    return tmp_path / "desk" / "strategy.toml"


def test_one_command_writes_the_mandate_and_signs_the_desk(tmp_path: Path) -> None:
    config = desk(tmp_path)
    assert (
        main(["mandate", "--sign", "Ana Rivera", "--on", "2026-10-10", "--config", str(config)])
        == 0
    )

    project = config.parent
    assert signed_in(project)
    assert (
        (project / MANDATE_FILE)
        .read_text("utf-8")
        .startswith("Signed by Ana Rivera on 2026-10-10.")
    )
    mandate = load_strategy_config(config).execution
    assert mandate.acknowledged_by == "Ana Rivera" and mandate.acknowledged_on == "2026-10-10"
    # Signing is not switching on: the desk is still in sandbox.
    assert mandate.mode == "sandbox"
    from dataclasses import replace

    stops = blocking(replace(mandate, mode="venue"), project=project, tier="SHADOW", practice=True)
    assert not any("acknowledged" in s or MANDATE_FILE in s for s in stops)


def test_signing_again_replaces_the_signature_and_keeps_the_rest(tmp_path: Path) -> None:
    config = desk(tmp_path)
    config.write_text(
        config.read_text("utf-8")
        + '\n[execution]\nmode = "venue"\nacknowledged_by = "old"\nleverage = 3\n'
        + "\n[audit]\non_alarm = true\n",
        encoding="utf-8",
    )
    sign(config, 'Sam "the desk" Lee', DAY)
    sign(config, 'Sam "the desk" Lee', DAY)
    text = config.read_text("utf-8")
    live = [line for line in text.splitlines() if line.startswith("acknowledged_")]
    # The template's commented example stays; the live signature appears once.
    assert len(live) == 2
    mandate = load_strategy_config(config).execution
    assert mandate.acknowledged_by == 'Sam "the desk" Lee'
    assert mandate.mode == "venue" and mandate.leverage == 3
    assert load_strategy_config(config).audit.on_alarm


def test_signing_with_venue_mode_turns_orders_on_in_the_same_step(tmp_path: Path) -> None:
    target = tmp_path / "desk"
    assert main(["init", str(target), "--profile", "autonomous", "--venue", "okx"]) == 0
    config = target / "strategy.toml"
    assert main(["mandate", "--sign", "Ana", "--venue-mode", "--config", str(config)]) == 0
    mandate = load_strategy_config(config).execution
    assert mandate.mode == "venue" and mandate.acknowledged_by == "Ana"
    # The sandbox line it replaced is gone, so the table carries one mode.
    lines = config.read_text("utf-8").splitlines()
    assert lines.count('mode = "venue"') == 1 and 'mode = "sandbox"' not in lines


def test_a_mandate_file_someone_else_wrote_is_left_alone(tmp_path: Path) -> None:
    config = desk(tmp_path)
    (config.parent / MANDATE_FILE).write_text("our own notes\n", encoding="utf-8")
    before = config.read_text("utf-8")
    with pytest.raises(SignError, match="left alone"):
        sign(config, "Ana", DAY)
    assert config.read_text("utf-8") == before
    assert (config.parent / MANDATE_FILE).read_text("utf-8") == "our own notes\n"


def test_what_cannot_be_a_signature_is_refused(tmp_path: Path) -> None:
    config = desk(tmp_path)
    with pytest.raises(SignError, match="one line"):
        sign(config, "  ", DAY)
    with pytest.raises(SignError, match="in the future"):
        sign(config, "Ana", date.today() + timedelta(days=1))
    with pytest.raises(SignError, match="not found"):
        sign(tmp_path / "nowhere.toml", "Ana", DAY)
    assert not (config.parent / MANDATE_FILE).exists()
