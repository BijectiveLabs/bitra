"""The console runs the same commands the shell does, and cannot do anything else.

Every line typed at the prompt goes through the same parser and the same handler as a command typed
in a shell, so these tests check the loop rather than the commands: what it dispatches, what it
survives, and that it never becomes a second way of deciding something.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.cli import console as shell
from trading_research_agents.cli.main import CLEARED, REFUSED, build_parser


def build(
    lines: list[str] | None = None, *, code: int = CLEARED, boom: type[BaseException] | None = None
) -> tuple[shell.Console, list[argparse.Namespace], list[str]]:
    ran: list[argparse.Namespace] = []
    drawn: list[str] = []
    queue = list(lines or [])

    def run(args: argparse.Namespace) -> int:
        if boom is not None:
            raise boom()
        ran.append(args)
        return code

    def read() -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    def cockpit() -> None:
        drawn.append("cockpit")

    return (
        shell.Console(
            parser=build_parser(),
            run=run,
            cockpit=cockpit,
            commands=["check", "run", "audit"],
            read=read,
        ),
        ran,
        drawn,
    )


def test_a_typed_command_is_the_same_command_a_shell_would_run() -> None:
    console, ran, _ = build()
    assert console.one("run --instrument ETHUSDT --now") == CLEARED
    assert len(ran) == 1
    args = ran[0]
    assert (args.group, args.command) == ("run", "run")
    assert args.instrument == "ETHUSDT" and args.now is True


def test_quotes_and_the_word_tra_are_both_handled() -> None:
    console, ran, _ = build()
    assert console.one('tra data get "BTC USDT" 2025-01-01') == CLEARED
    assert ran[0].instrument == "BTC USDT"


def test_leaving_clearing_and_an_empty_line() -> None:
    console, ran, _ = build()
    assert console.one("exit") is None and console.one("quit") is None
    assert console.one("   ") == CLEARED and not ran
    console.last = REFUSED
    assert console.one("") == REFUSED  # an empty line changes nothing, not even the last verdict


def test_an_unknown_command_is_one_line_not_the_end_of_the_session() -> None:
    console, ran, _ = build()
    # argparse would end the process here; inside the console it is a message and a prompt.
    assert console.one("nonsense --wrong") == 2
    assert not ran
    assert console.one("check") == CLEARED and len(ran) == 1


def test_an_interrupted_command_leaves_the_console_standing() -> None:
    console, _, _ = build(boom=KeyboardInterrupt)
    assert console.one("check") == REFUSED


def test_the_loop_opens_on_the_cockpit_and_ends_on_end_of_input(tmp_path: Path) -> None:
    console, ran, drawn = build(["check", "", "run --now"])
    assert console.loop() == CLEARED
    assert drawn == ["cockpit"]
    assert [a.group for a in ran] == ["check", "run"]


def test_the_loop_ends_when_the_operator_says_so() -> None:
    console, ran, _ = build(["check", "exit", "run"])
    assert console.loop() == CLEARED
    assert [a.group for a in ran] == ["check"]


def test_a_refusal_is_carried_out_as_the_exit_code() -> None:
    console, _, _ = build(["check"], code=REFUSED)
    assert console.loop() == REFUSED


def test_the_history_is_the_operators_own_record(tmp_path: Path) -> None:
    assert shell.history_path(False) is None
    where = shell.history_path(True, tmp_path)
    assert where is not None and where.name == ".tra-history"

    where.write_text("\n".join(f"check {i}" for i in range(800)) + "\n", encoding="utf-8")
    shell.trim(where, lines=100)
    kept = where.read_text(encoding="utf-8").splitlines()
    assert len(kept) == 100 and kept[-1] == "check 799"
    # A history file that cannot be written is never a reason to fail a session.
    shell.trim(tmp_path / "missing" / "x", lines=10)


def test_the_console_only_opens_for_a_person(monkeypatch: pytest.MonkeyPatch) -> None:
    class Fake:
        def __init__(self, tty: bool) -> None:
            self.tty = tty

        def isatty(self) -> bool:
            return self.tty

    import sys

    monkeypatch.setattr(sys, "stdin", Fake(True))
    monkeypatch.setattr(sys, "stdout", Fake(True))
    assert shell.interactive()
    monkeypatch.setattr(sys, "stdout", Fake(False))
    assert not shell.interactive()


def test_help_lists_what_can_be_typed(capsys: pytest.CaptureFixture[str]) -> None:
    console, ran, _ = build()
    assert console.one("help") == CLEARED and not ran
    printed = capsys.readouterr().out
    assert "check" in printed and "audit" in printed


def test_one_shot_runs_without_the_consoles_own_footer(capsys: pytest.CaptureFixture[str]) -> None:
    console, ran, _ = build()
    assert console.one("check", say=False) == CLEARED
    assert "in 0.0s" not in capsys.readouterr().out
    assert len(ran) == 1


def test_the_console_reaches_every_command_the_cli_has() -> None:
    from trading_research_agents.cli.main import HANDLERS

    names = [f"{group} {command}" for group, command in sorted(HANDLERS)]
    console = shell.Console(
        parser=build_parser(),
        run=lambda args: CLEARED,
        cockpit=lambda: None,
        commands=names,
        read=lambda: "exit",
    )
    # Nothing is hidden from the console, and the three built-ins are not commands.
    assert set(names) <= set(console.words)
    for built_in in shell.BUILT_IN:
        assert built_in in console.words


def test_the_reader_falls_back_rather_than_failing_without_a_terminal(tmp_path: Path) -> None:
    read: Any = shell.reader(tmp_path / "history", ["check"])
    assert callable(read)


def test_plain_output_carries_typography_as_ascii() -> None:
    from trading_research_agents.cli.main import plain_text

    said = "the operator\u2019s z\u2011score \u2014 \u201cbullish\u201d\u2026"
    assert plain_text(said) == 'the operator\'s z-score - "bullish"...'


def test_the_console_clears_the_screen_before_it_draws_the_cockpit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console, _, _ = build()
    steps: list[str] = []

    class Ui:
        def take_screen(self) -> None:
            steps.append("clear")

    monkeypatch.setattr(shell.Console, "ui", lambda self: Ui())
    console.cockpit = lambda: steps.append("cockpit")
    monkeypatch.setattr(shell.Console, "say", lambda self, *a, **k: None)
    monkeypatch.setattr(shell.Console, "rule", lambda self: None)
    console.loop()
    assert steps[:2] == ["clear", "cockpit"]


def test_taking_the_screen_erases_the_scrollback_and_leaves_a_pipe_alone() -> None:
    import io

    from trading_research_agents.cli import tui

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    screen = Terminal()
    assert tui.take_screen(screen) is True
    assert screen.getvalue() == "\x1b[H\x1b[2J\x1b[3J"
    pipe = io.StringIO()
    assert tui.take_screen(pipe) is False and pipe.getvalue() == ""
