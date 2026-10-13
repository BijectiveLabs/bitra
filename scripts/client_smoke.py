"""Run the commands a new client types first, against an installed `tra`, and fail on a crash.

    python scripts/client_smoke.py PATH_TO_TRA WORKDIR

No key and no network are needed. A command may refuse (exit 1 or 2) - a missing extra, a missing
key - but it has to say so in a sentence: a traceback, a bare `No module named` or an unhandled
error class is a failure whatever the exit code.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

CRASH = re.compile(
    r"Traceback \(most recent call last\)|No module named|"
    r"\b(AttributeError|KeyError|TypeError|NameError|UnboundLocalError)\b"
)


def main() -> int:
    tra, work = Path(sys.argv[1]), Path(sys.argv[2])
    work.mkdir(parents=True, exist_ok=True)
    desk = work / "desk"
    agent = ["--profile", "autonomous", "--agent", "groq", "--model", "m"]
    steps: list[tuple[list[str], Path, str]] = [
        (["--version"], work, ""),
        (["-c", "help"], work, ""),
        ([], work, "exit\n"),
        (["check"], work, ""),
        (["run"], work, ""),
        (["venues"], work, ""),
        (["venues", "okx", "--setup"], work, ""),
        (["mandate"], work, ""),
        (["models", "groq"], work, ""),
        (["init", str(desk), *agent], work, ""),
        (["check"], desk, ""),
        (["check", "--json"], desk, ""),
        (["keys"], desk, ""),
        (["keys", "--probe"], desk, ""),
        ([], desk, "help\ncheck\nexit\n"),
        (["sources", "show"], desk, ""),
        (["universe"], desk, ""),
        (["runs", "verify"], desk, ""),
        (["ladder", "show"], desk, ""),
        (["ledger", "show"], desk, ""),
        (["watch"], desk, ""),
        (["audit"], desk, ""),
        (["mandate", "--sign", "Smoke Test"], desk, ""),
        (["mcp", "serve"], desk, ""),
    ]
    failed = 0
    for args, cwd, stdin in steps:
        # The command is this script's own list run against the path the caller gave it.
        done = subprocess.run(  # noqa: S603
            [str(tra), "--plain", *args] if args and args[0] != "-c" else [str(tra), *args],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        out = (done.stdout or "") + (done.stderr or "")
        crashed = CRASH.search(out)
        bad = crashed is not None or done.returncode not in (0, 1, 2)
        failed += bad
        print(f"{'FAIL' if bad else 'ok  '} exit {done.returncode}  tra {' '.join(args)}")
        if bad:
            print(out[-2000:])
    print(f"{len(steps) - failed} of {len(steps)} commands answered without crashing")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
