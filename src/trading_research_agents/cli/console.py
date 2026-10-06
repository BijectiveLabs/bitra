"""`tra` on its own: one process that stays open and runs the rest of the commands inside it.

A desk operator does not want to type a long command, read a wall of text, lose it to the scrollback
and type the next one. They want somewhere to stand. So `tra` opens a console: the cockpit first,
then a prompt that takes every command this CLI has, rendered where they are looking, until they
type `exit`.

What it deliberately is not: a second way of doing anything. Every line is parsed by the same parser
and handed to the same handler with the same arguments, so a command typed here and a command typed
in a shell are the same code path leaving the same record on disk. The console renders; it decides
nothing, changes no configuration and sends no order. That is why it can afford to be comfortable.

Line editing, history and completion come from prompt_toolkit, and from plain input where that
cannot start. The history file is the operator's own record of what they ran, kept beside their
project and turned off with `--no-history`.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

from rich.text import Text

HISTORY_FILE = ".tra-history"
HISTORY_LINES = 500
PROMPT = "> "
PLACEHOLDER = 'a command, or "help" for the list'
LEAVE = ("exit", "quit")
HELP = ("help", "?")
CLEAR = ("clear", "cls")
BUILT_IN = (*LEAVE, *HELP, *CLEAR)
CLEARED, REFUSED = 0, 1


def reader(history: Path | None, words: Sequence[str]) -> Callable[[], str]:
    """A line of input, with history and completion where the terminal can give them."""
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.history import FileHistory, InMemoryHistory
    except ImportError:  # pragma: no cover - only where the dependency is absent
        return lambda: input(PROMPT)

    try:
        session: Any = PromptSession(
            history=InMemoryHistory() if history is None else FileHistory(str(history)),
            completer=WordCompleter(list(words), ignore_case=True, sentence=True),
            complete_while_typing=False,
        )
    except Exception:  # pragma: no cover - a terminal that cannot do line editing
        return lambda: input(PROMPT)
    hint = FormattedText([("class:placeholder", PLACEHOLDER)])
    return lambda: str(session.prompt(PROMPT, placeholder=hint))


@dataclass
class Console:
    """The loop. It owns nothing but the prompt and the screen."""

    parser: argparse.ArgumentParser
    run: Callable[[argparse.Namespace], int]
    cockpit: Callable[[], Any]
    commands: Sequence[str] = ()
    set_plain: Callable[[bool], None] = lambda _: None
    history: Path | None = None
    read: Callable[[], str] | None = None
    words: list[str] = field(default_factory=list)
    last: int = CLEARED

    def __post_init__(self) -> None:
        if not self.words:
            self.words = [*self.commands, *BUILT_IN]
        if self.read is None:
            self.read = reader(self.history, self.words)

    def one(self, line: str, *, say: bool = True) -> int | None:
        """Run one typed line. None means the console should close.

        `say` is the console's own footer, how long the command took and how it ended. `tra -c`
        turns it off, because a one-shot command's output belongs to whatever reads it.
        """
        text = line.strip()
        if not text:
            return self.last
        if text in LEAVE:
            return None
        if text in HELP:
            self.help()
            return CLEARED
        if text in CLEAR:
            self.ui().console().clear()
            self.cockpit()
            return CLEARED
        try:
            argv = shlex.split(text)
        except ValueError as exc:
            self.say(f"tra: {exc}", bad=True)
            return REFUSED
        if argv and argv[0] == "tra":
            # Muscle memory from the shell, and harmless: drop it rather than fail on it.
            argv = argv[1:]
        try:
            args = self.parser.parse_args(argv)
        except SystemExit as leaving:
            # argparse prints its own message for an unknown command or --help and then tries to
            # end the process. In here that is one line of output, not the end of the session.
            return int(leaving.code or CLEARED)
        if args.group is None:
            self.cockpit()
            return CLEARED
        self.set_plain(bool(args.plain))
        started = perf_counter()
        try:
            code = self.run(args)
        except KeyboardInterrupt:
            # The command is gone; the console is not. Whatever it wrote before this is on disk.
            self.say("interrupted", bad=True)
            return REFUSED
        if say:
            self.say(
                f"{'ok' if code == CLEARED else 'refused'} in {perf_counter() - started:.1f}s",
                bad=code != CLEARED,
            )
        return code

    def loop(self) -> int:
        """Open on the cockpit, then take commands until the operator leaves."""
        # The console owns the screen from its first line; what the shell showed before stays in
        # the scrollback, above, rather than on top of the cockpit.
        self.ui().console().clear()
        self.cockpit()
        self.say('commands run here. "help" lists them, "exit" leaves.')
        read = self.read or (lambda: input(PROMPT))
        while True:
            self.rule()
            try:
                line = read()
            except KeyboardInterrupt:
                self.say('nothing to interrupt; "exit" leaves', bad=True)
                continue
            except EOFError:
                break
            if line.strip():
                # A line under what was typed, so the answer below it is plainly the answer to it.
                self.rule()
            code = self.one(line)
            if code is None:
                break
            self.last = code
        if self.history is not None:
            trim(self.history)
        return self.last

    def help(self) -> None:
        """The commands, in the columns a person reads. `<command> --help` has the detail."""
        from rich import box
        from rich.table import Table

        table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, title="commands")
        for _ in range(3):
            table.add_column("")
        rows = [*self.commands]
        height = (len(rows) + 2) // 3
        columns = [rows[i * height : (i + 1) * height] for i in range(3)]
        for row in range(height):
            table.add_row(*(column[row] if row < len(column) else "" for column in columns))
        self.ui().show(table)
        self.say('"<command> --help" for what one takes; "exit" leaves')

    def rule(self) -> None:
        """A line across the terminal, so one command's output never runs into the next."""
        self.ui().console().print(Text("-" * self.width(), style="dim"), highlight=False)

    def width(self) -> int:
        try:
            return max(20, min(int(self.ui().console().width), 120))
        except Exception:  # pragma: no cover - a console that will not say how wide it is
            return 80

    def say(self, text: str, *, bad: bool = False) -> None:
        self.ui().console().print(Text(text, style="yellow" if bad else "dim"))

    def ui(self) -> Any:
        from trading_research_agents.cli import tui

        return tui


def history_path(keep: bool, where: Path | None = None) -> Path | None:
    """Where the console remembers what was typed, or nothing when the operator said not to."""
    if not keep:
        return None
    return (where or Path.cwd()) / HISTORY_FILE


def trim(path: Path, lines: int = HISTORY_LINES) -> None:
    """Keep the history readable: the last few hundred lines, not a year of them."""
    try:
        kept = path.read_text(encoding="utf-8").splitlines()[-lines:]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError:
        # A history file that cannot be written is not a reason to fail a session.
        pass


def interactive() -> bool:
    """Whether there is a person at the other end of this process."""
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )
