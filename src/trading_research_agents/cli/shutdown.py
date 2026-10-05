"""A request to stop, from wherever it came, takes the path a keyboard interrupt takes.

A desk on a server is stopped by its service manager with SIGTERM, and a desk in a terminal over
SSH gets SIGHUP when the connection drops. Left to their defaults both end the process on the spot,
in the middle of whatever it was doing, including waiting on an order that rests on the book. As an
interrupt they reach the same handlers Ctrl-C reaches: the working order is cancelled, the record
is written, and the scheduler says why it stopped.

Closing a Windows console window is the one case this cannot cover. Windows gives the process a few
seconds and ends it whatever the handler is doing, which is why the exits travel with the entry and
the next cycle refuses while an order works.
"""

from __future__ import annotations

import json
import os
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_research_agents.records import append_line

STOPPING = ("SIGTERM", "SIGHUP", "SIGBREAK")
LOG = "scheduler.jsonl"


@contextmanager
def as_interrupt() -> Iterator[None]:
    """Inside this block SIGTERM, SIGHUP and SIGBREAK raise KeyboardInterrupt, named."""
    if threading.current_thread() is not threading.main_thread():
        # Only the main thread may set a handler. A caller off it keeps the defaults it had.
        yield
        return

    def stop(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(signal.Signals(signum).name)

    before: dict[signal.Signals, Any] = {}
    for name in STOPPING:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            before[sig] = signal.signal(sig, stop)
        except (OSError, ValueError):
            continue
    try:
        yield
    finally:
        for sig, handler in before.items():
            with suppress(OSError, ValueError):
                signal.signal(sig, handler)


def stopped_by(exc: KeyboardInterrupt) -> str:
    """What to call the stop in the record: the signal when there was one, the keyboard if not."""
    said = str(exc)
    if said in STOPPING:
        return f"{said} from outside the process"
    if said.startswith("interrupted at the venue"):
        return said
    return "a keyboard interrupt"


def journal(where: Path, event: str, **fields: Any) -> None:
    """One line per start and stop of `tra run`, beside the cycles it ran.

    Each cycle already keeps its own directory. What none of them can say is when the desk was
    running at all, who stopped it and why - and on a server whose output nobody kept, that line is
    the difference between knowing the desk was down for an hour and guessing.
    """
    line = {"at": datetime.now(UTC).isoformat(), "event": event, "pid": os.getpid(), **fields}
    # The journal is a convenience over the cycle records, never a reason to stop trading.
    with suppress(OSError):
        append_line(where / LOG, line, durable=True)


LOCK = "desk.lock"


class DeskBusyError(ValueError):
    """Another desk process is already sending orders from this project."""


def alive(pid: int) -> bool:
    """Whether a process with this id is running. Never signals it."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        # os.kill(pid, 0) terminates a process on Windows, so ask the kernel instead.
        kernel = getattr(ctypes, "windll").kernel32  # noqa: B009 - absent off Windows
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        finally:
            kernel.CloseHandle(handle)
        return code.value == 259
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def claim(where: Path) -> Path:
    """Take the project's venue lock, or refuse while another live process holds it.

    Two desks sending from one project read the same account in the same second and can both decide
    an instrument is free. One of them has to be refused before it starts, not reconciled after.
    A lock left by a process that has died is simply taken over.
    """
    path = Path(where) / LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            held = json.loads(path.read_text(encoding="utf-8"))
            holder = int(held.get("pid", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            holder = 0
        if holder and holder != os.getpid() and alive(holder):
            raise DeskBusyError(
                f"another desk is already running from this project (pid {holder}, since "
                f"{held.get('at', 'unknown')}); stop it first, so two processes never send orders "
                "for one account"
            )
    path.write_text(
        json.dumps({"pid": os.getpid(), "at": datetime.now(UTC).isoformat()}) + "\n",
        encoding="utf-8",
    )
    return path


def release(path: Path) -> None:
    with suppress(OSError):
        held = json.loads(path.read_text(encoding="utf-8"))
        if int(held.get("pid", 0)) == os.getpid():
            path.unlink()
