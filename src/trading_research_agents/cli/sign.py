"""`tra mandate --sign`: the mandate written into the project and signed, in one step.

Signing is the operator's act; this only does the typing. The text goes into MANDATE.md word for
word, the name and the date go into [execution], and venue mode stays off until the operator turns
it on. A MANDATE.md someone already wrote is never overwritten.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trading_research_agents.execution.mandate import DISCLAIMER, MANDATE_FILE, signed_in

HEADER = re.compile(r"^\s*\[execution\]\s*(#.*)?$")
TABLE = re.compile(r"^\s*\[")
SIGNED_KEY = re.compile(r"^\s*acknowledged_(by|on)\s*=")
MODE_KEY = re.compile(r"^\s*mode\s*=")


class SignError(ValueError):
    """The mandate cannot be signed as asked."""


@dataclass(frozen=True)
class Signed:
    mandate: Path
    config: Path
    wrote_mandate: bool


def sign(config: Path, name: str, on: date, *, venue_mode: bool = False) -> Signed:
    """Write MANDATE.md when it is not there, and the signature into [execution].

    `venue_mode` also sets `mode = "venue"`, for an operator who signs and turns orders on in the
    same breath. Every other door stays where it was: the keys, the leverage, the cap, and on
    mainnet the ladder.
    """
    who = name.strip()
    if not who or "\n" in who or "\r" in who:
        raise SignError("--sign takes the name of the person taking this on, on one line")
    if on > date.today():
        raise SignError(
            f"{on.isoformat()} is in the future; a mandate is signed on or before today"
        )
    if not config.is_file():
        raise SignError(f"{config} not found; run this beside strategy.toml or pass --config")

    project = config.resolve().parent
    mandate = project / MANDATE_FILE
    wrote = False
    if mandate.exists():
        if not signed_in(project):
            raise SignError(
                f"{mandate} exists and does not carry the mandate text word for word; it is "
                "yours, so it is left alone: move it aside or add the text from `tra mandate`"
            )
    else:
        mandate.write_text(
            f"Signed by {who} on {on.isoformat()}.\n\n{DISCLAIMER}", encoding="utf-8"
        )
        wrote = True

    before = config.read_text(encoding="utf-8")
    config.write_text(_signed(before, who, on.isoformat(), venue_mode), encoding="utf-8")
    try:
        from trading_research_agents.trading.config import load_strategy_config

        load_strategy_config(config)
    except Exception:
        # A config this step broke is put back exactly as it was.
        config.write_text(before, encoding="utf-8")
        if wrote:
            mandate.unlink()
        raise
    return Signed(mandate=mandate, config=config, wrote_mandate=wrote)


def _signed(text: str, who: str, on: str, venue_mode: bool = False) -> str:
    """The config with acknowledged_by and acknowledged_on set in its [execution] table."""
    lines = text.splitlines()
    lines_out = [
        *(['mode = "venue"'] if venue_mode else []),
        f"acknowledged_by = {json.dumps(who, ensure_ascii=False)}",
        f'acknowledged_on = "{on}"',
    ]
    start = next((i for i, line in enumerate(lines) if HEADER.match(line)), None)
    if start is None:
        tail = ["", "[execution]", *lines_out]
        body = text.rstrip("\n").splitlines()
        return "\n".join(body + tail) + "\n"
    end = next(
        (i for i in range(start + 1, len(lines)) if TABLE.match(lines[i])),
        len(lines),
    )
    kept = [
        line
        for line in lines[start + 1 : end]
        if not SIGNED_KEY.match(line) and not (venue_mode and MODE_KEY.match(line))
    ]
    out = lines[: start + 1] + lines_out + kept + lines[end:]
    return "\n".join(out) + "\n"
