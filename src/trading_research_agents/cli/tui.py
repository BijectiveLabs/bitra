"""The console face of `tra`: the same commands, rendered for a person at a terminal.

Every command still speaks plain text and `--json`; this module only decides how the same
figures look when a person is watching. It is off when stdout is not a terminal, when
`--plain` or `TRA_PLAIN=1` is given, or when `--json` is asked for, so scripts, CI and agent
hosts never see a table where they expected a line. Nothing here reaches back into a run:
the live views read reports and decide nothing.
"""

from __future__ import annotations

import io
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from importlib.resources import files
from time import monotonic, sleep
from typing import Any

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

BANNER = r"""
 _____  ____      _
|_   _||  _ \    / \
  | |  | |_) |  / _ \
  | |  |  _ <  / ___ \
  |_|  |_| \_\/_/   \_\
""".strip("\n")

# The home screen's colour, and the colour of every line that separates one answer from the next.
ACCENT = "cyan"
NAME = "TRA"
TAGLINE = (
    "autonomous trading agents with deterministic execution; the agents propose, the system decides"
)
_console: Console | None = None


# Widest first: the name on two lines, then the short name a narrow terminal fits. No frame is drawn
# around it, so the letters have the whole width and nothing beside them to misalign.
WORDMARKS = (
    ("trading-research.txt", "agents.txt"),
    ("tra.txt",),
)
# A terminal that wraps at its last column turns a line exactly as wide as itself into two.
MARGIN = 1


def _block(name: str, encoding: str) -> str | None:
    try:
        text = files("trading_research_agents.cli").joinpath(f"static/wordmark/{name}")
        drawn = text.read_text("utf-8")
        drawn.encode(encoding or "ascii")
    except (OSError, TypeError, LookupError, UnicodeError):
        return None
    lines = [line.rstrip() for line in drawn.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) or None


def wordmark(width: int, encoding: str = "utf-8") -> list[str]:
    """The wordmark as the blocks a console centres one under another.

    The block letters live in text files beside this module, not in source. A console whose
    encoding has no block characters, a legacy Windows code page, gets the ASCII one rather than a
    row of question marks or an encoding error.
    """
    for names in WORDMARKS:
        blocks = [_block(name, encoding) for name in names]
        drawn = [block for block in blocks if block is not None]
        if len(drawn) != len(names):
            continue
        widest = max(len(line) for block in drawn for line in block.splitlines())
        if widest + MARGIN <= width:
            return drawn
    return [BANNER]


def console(safe: bool = False) -> Console:
    global _console
    if safe:
        return _safe_console()
    if _console is None:
        # No markup: config names such as [run].size_step appear in refusals and would be read as
        # style tags and silently dropped from the line.
        _console = Console(highlight=False, markup=False)
    return _console


# Cursor home, erase the screen, then erase the scrollback: what `clear` sends. The order matters on
# the classic Windows console, where erasing the screen pushes it into the scrollback; erasing the
# scrollback first left the shell's lines above the home screen (seen 2026-10-06).
TAKE_SCREEN = "\x1b[H\x1b[2J\x1b[3J"
VT_PROCESSING = 0x0004
STD_OUTPUT = -11


def take_screen(stream: Any = None) -> bool:
    """Give the whole terminal to the console, the way a full-screen program starts.

    Returns whether it did; a pipe or a file is left alone.
    """
    out = stream or sys.stdout
    if not getattr(out, "isatty", lambda: False)():
        return False
    if os.name == "nt":
        _virtual_terminal()
    out.write(TAKE_SCREEN)
    out.flush()
    return True


def _virtual_terminal() -> None:
    """Escape sequences on the classic Windows console are text until this mode is on."""
    import ctypes

    kernel = getattr(ctypes, "windll").kernel32  # noqa: B009 - absent off Windows
    handle = kernel.GetStdHandle(STD_OUTPUT)
    mode = ctypes.c_ulong()
    if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel.SetConsoleMode(handle, mode.value | VT_PROCESSING)


def enabled(plain: bool = False, as_json: bool = False) -> bool:
    """Whether a person is watching: a terminal, no --plain, no TRA_PLAIN, no --json."""
    if plain or as_json or os.environ.get("TRA_PLAIN"):
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _fmt(value: Any) -> Any:
    """Plain values for a two-column panel: mappings and lists read as words, not as reprs."""
    if value is None:
        return ""
    if isinstance(value, Text):
        return value
    if isinstance(value, Mapping):
        return "; ".join(f"{k}: {_fmt(v)}" for k, v in value.items())
    if isinstance(value, list | tuple):
        return ", ".join(str(_fmt(v)) for v in value)
    return str(value)


def _kv(rows: Sequence[tuple[str, Any]]) -> Table:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="dim")
    table.add_column()
    for key, value in rows:
        table.add_row(str(key), _fmt(value))
    return table


def _bps(value: Any) -> Text:
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return Text(str(value))
    style = "green" if number > 0 else "red" if number < 0 else ""
    return Text(f"{number:+.2f} bps", style=style)


def home(version: str, good: Sequence[str], bad: Sequence[str], next_command: str) -> None:
    """`tra` with nothing else: who this is, what is ready, what to type next."""
    c = console()
    rows: list[Any] = []
    for block in wordmark(c.width, c.encoding):
        lines = block.splitlines()
        # One width for every row of a block, so centring moves it whole, not row by row.
        wide = max(len(line) for line in lines)
        rows.append(
            Align.center(Text("\n".join(line.ljust(wide) for line in lines), f"bold {ACCENT}"))
        )
    head = Group(
        Text(""),
        *rows,
        Text(""),
        Text(NAME, style="bold", justify="center"),
        Text(TAGLINE, style="dim", justify="center"),
        Text(f"tra {version}  by Bijective", style="dim", justify="center"),
        Text(""),
    )
    c.print(head)
    c.print(check_table(good, bad))
    c.print(Text("next: ", style="dim") + Text(next_command, style="bold"))


def check_table(good: Sequence[str], bad: Sequence[str]) -> Table:
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    table.add_column("state", width=5)
    table.add_column("what")
    for g in good:
        table.add_row(Text("ok", style="green"), g)
    for b in bad:
        table.add_row(Text("todo", style="yellow"), b)
    return table


def countdown(seconds: float, *, until: str = "the next cycle") -> None:
    """Wait, with the time left on screen, so a desk between cycles does not look hung."""
    deadline = monotonic() + max(0.0, seconds)
    with Live(console=console(), refresh_per_second=2, transient=True) as live:
        while True:
            left = deadline - monotonic()
            if left <= 0:
                return
            minutes, secs = divmod(int(left + 0.999), 60)
            shown = f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"
            live.update(
                Text(f"{until} in {shown}; Ctrl-C stops the desk", style="dim"), refresh=True
            )
            sleep(min(1.0, left))


def show(renderable: Any) -> None:
    """Draw it, and never let a character a console cannot encode end a run.

    A model's answer carries whatever the model wrote - a non-breaking hyphen, an accent, a dash
    from a headline - and an older Windows console encodes none of those. The run has already
    happened and is already on disk by the time this draws it, so a console that cannot show a
    character falls back to text rather than raising in front of the operator.
    """
    try:
        console().print(renderable)
    except UnicodeEncodeError:
        console(safe=True).print(renderable)


def _safe_console() -> Console:
    """The same console, writing through a stream that replaces what it cannot encode."""
    stream = getattr(sys.stdout, "buffer", None)
    file = (
        io.TextIOWrapper(stream, encoding="utf-8", errors="backslashreplace", newline="")
        if stream is not None
        else sys.stdout
    )
    return Console(highlight=False, markup=False, file=file)


Renderer = Callable[[], Any]
