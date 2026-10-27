"""What every command shares: the environment and its keys, the data and execution clients, fees
read from a venue, and how a result is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_research_agents.llm import Completer
from trading_research_agents.llm.chain import CompleterChain, Link, parse_link
from trading_research_agents.llm.providers import (
    CallOptions,
    provider,
)
from trading_research_agents.trading.venues import (
    UnknownLiveVenueError,
    live_venue,
)

# The exit codes a pipeline acts on.
CLEARED = 0


REFUSED = 1


USAGE = 2


def parse_month(text: str) -> tuple[int, int]:
    match = re.match(r"^(\d{4})-(\d{2})$", text.strip())
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise argparse.ArgumentTypeError(f"{text!r} is not a month like 2025-01")
    return int(match.group(1)), int(match.group(2))


# `--plain` for this process: set once by the entry point, read by every command that draws.
_PLAIN = False


def set_plain(plain: bool) -> None:
    global _PLAIN
    _PLAIN = plain


def plain_asked() -> bool:
    return _PLAIN


def _watching(as_json: bool = False) -> bool:
    """A person at a terminal, who gets the console view instead of the lines."""
    from trading_research_agents.cli import tui

    return tui.enabled(_PLAIN, as_json)


def emit(
    payload: dict[str, Any],
    as_json: bool,
    lines: list[str],
    render: Callable[[], Any] | None = None,
) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
    elif render is not None and _watching():
        from trading_research_agents.cli import tui

        tui.show(render())
    else:
        # Flushed per report: a desk logging to a file is read while it runs, not after it stops.
        print(plain_text("\n".join(lines)), flush=True)


# Typographic characters models like to write. A log or an old console shows them as escapes or
# replacement marks, so plain output carries their ASCII form; the records keep the original.
TYPOGRAPHY = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u2026": "...",
        "\u00a0": " ",
        "\u202f": " ",
    }
)


def plain_text(text: str) -> str:
    return text.translate(TYPOGRAPHY)


# Measured twice, on two different models: a schema with several cited claims in it needs about
# twice the output cap a one-line answer does, and below this a model returns an invalid object
# rather than a short one. The floor is announced when it is applied, never applied silently.
REPORT_HEADROOM = 2048


def _completer_for_role(
    a: Any,
    role: str,
    *,
    floor: int = 0,
    on_attempt: Callable[[str, Any], None] | None = None,
) -> Completer:
    """The models this role is asked with, in the order the operator declared them.

    A screener that labels a regime and an analyst that reasons over sixty bars do not need the
    same model, and the difference is most of a token bill. `[agents.roles.<role>]` is where an
    operator says so, and `fallback` is where they say what to do when that provider is rate
    limited or down: the next one is tried with the same brief, because the brief is built by code
    and there is no conversation to carry over.
    """
    settings = a.for_role(role)
    links = [Link(f"{settings.provider}:{settings.model or 'default'}", _link(a, settings, floor))]
    for name in settings.fallback:
        named, model = parse_link(name)
        links.append(Link(name, _link(a, settings, floor, provider_name=named, model=model)))
    wait = float(getattr(a, "max_retry_wait_s", 0.0) or 0.0)
    if len(links) == 1 and wait <= 0:
        return links[0].build()
    return CompleterChain(role=role, links=links, on_attempt=on_attempt, max_wait_s=wait)


def _link(
    a: Any,
    settings: Any,
    floor: int,
    *,
    provider_name: str | None = None,
    model: str | None = None,
) -> Callable[[], Completer]:
    """How one link is built, when it is first needed rather than when it is declared."""

    def build() -> Completer:
        p = provider(provider_name or settings.provider)
        declared = settings.max_output_tokens
        options = CallOptions(
            # No cap stays no cap: the floor exists to raise one an operator set too low, and a
            # report needs more room than a one-line answer does.
            max_output_tokens=None if declared is None else max(declared, floor),
            timeout_s=float(settings.call_timeout_s),
            max_retries=settings.max_retries,
            effort=settings.effort,
        )
        wanted = model if provider_name else settings.model
        base = None if provider_name else settings.base_url
        if p.build_with is not None:
            return p.build_with(wanted, base, options)
        return p.build(wanted, base)

    return build


def _ui() -> Any:
    from trading_research_agents.cli import tui

    return tui


def _data_dir(config: Path) -> Path:
    return config.resolve().parent / "data"


def _datasets(config: Path) -> dict[str, Path]:
    """`data/<INSTRUMENT>-<when>.parquet` as `tra data get` writes them: newest per instrument."""
    found: dict[str, Path] = {}
    for path in sorted(_data_dir(config).glob("*.parquet")):
        instrument = path.stem.split("-", 1)[0]
        found[instrument] = path
    return found


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines. Blank lines and comments are skipped; quotes are stripped."""
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            raise ValueError(f"{path}:{number}: not a KEY=VALUE line")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _load_env(args: argparse.Namespace) -> None:
    """Keys into this process: the file the operator named, then the project's own `.env`.

    The shell always wins. Nothing outside those two files is read, and a command that finds no key
    still says which name it was looking for rather than failing at the vendor.
    """
    beside = getattr(args, "config", None)
    here = beside.resolve().parent / ".env" if beside is not None else None
    for path in (getattr(args, "env_file", None), here):
        if path is None or not path.is_file():
            continue
        for key, value in read_env_file(path).items():
            os.environ.setdefault(key, value)


def _environment(env_file: Path | None, *, beside: Path | None = None) -> dict[str, str]:
    """The environment, plus the file the operator named, plus the project's own `.env`.

    A key in the shell wins over a file, and a file the operator named wins over the project's own.
    `.env` beside the config is read because that is where `tra init` tells them to put it, and
    because a check that says "no key" while the key sits in the next file is a check nobody trusts.
    Which file was read is reported, so nothing is picked up silently.
    """
    env = dict(os.environ)
    for path in (env_file, (beside / ".env") if beside is not None else None):
        if path is None or not path.is_file():
            continue
        for key, value in read_env_file(path).items():
            env.setdefault(key, value)
    return env


def _metered_roles(a: Any, usage: Any, *, floor: int = 0) -> Callable[[str], Any]:
    """One metered completer per role, built when the role is first asked.

    A role may run on its own model without the others paying for it, and every call goes through
    the same ledger, so what a desk spent is one file however many roles were involved.
    """
    from trading_research_agents.llm.usage import BudgetLimits, MeteredCompleter, Pricing

    limits = BudgetLimits(
        token_budget=a.token_budget,
        max_calls_per_hour=a.max_calls_per_hour,
        min_interval_s=a.min_interval_s,
        call_timeout_s=a.call_timeout_s,
    )
    pricing = {model: Pricing(i, o) for model, (i, o) in a.pricing.items()}
    metered: dict[str, MeteredCompleter] = {}

    def completer_for(role: str) -> MeteredCompleter:
        if role not in metered:
            settings = a.for_role(role)
            metered[role] = MeteredCompleter(
                _completer_for_role(a, role, floor=floor),
                role=role,
                provider=settings.provider,
                limits=limits,
                ledger=usage,
                pricing=pricing,
            )
        return metered[role]

    return completer_for


def _audit_lines(result: Any, raised: list[str]) -> list[str]:
    """One audit as the lines a person reads, used by the command and by a scheduled run."""
    lines = [f"audit {result.at}: {result.cycles} cycle(s) read, {result.findings} finding(s)"]
    if raised:
        lines.append(
            f"output cap raised to {REPORT_HEADROOM} for {', '.join(raised)}: a report with "
            "findings in it does not fit in less"
        )
    for role, report in sorted(result.reports.items()):
        lines.append(f"{role.replace('_', ' ')}: {report.summary}")
        for finding in report.findings:
            lines.append(f"  - {finding.what} ({finding.confidence})")
            lines.append(f"    proposal: {finding.proposal}")
            if finding.change is not None:
                lines.append(
                    f"    change: {finding.change.key} "
                    f"{finding.change.from_value} -> {finding.change.to_value}"
                )
        for reason in report.discarded:
            lines.append(f"  discarded: {reason}")
    for role, error in sorted(result.failed.items()):
        lines.append(f"{role}: {error}")
    lines.append(f"proposals.md and audit.json in {result.path}; nothing has been applied")
    return lines


def _has_live_plugin(name: str) -> bool:
    try:
        live_venue(name)
    except UnknownLiveVenueError:
        return False
    return True


def _desk_data(path: Path, cfg: Any, mode: str | None = None) -> Any:
    """data.toml as this desk reads it: `venue` is the venue it trades on.

    In venue mode the book and the funding an order is priced on must be that venue's own, so a
    chain that reads them from another venue is refused here rather than noticed in a fill.
    """
    from trading_research_agents.data.sources import DataConfigError, load_data_config

    mode = mode or cfg.execution.mode
    venue = cfg.execution.venue or cfg.run.venue_key.partition("/")[0]
    environment = (
        _declared_environment(cfg) if cfg.execution.environment or mode == "venue" else "mainnet"
    )
    declared = load_data_config(path).on_venue(venue, environment)
    elsewhere = declared.off_venue(venue)
    if mode == "venue" and elsewhere:
        raise DataConfigError(
            f"orders go to {venue}, but data.toml prices them on {'; '.join(elsewhere)}. The book "
            f"and the funding an order is priced on are the venue's own: write `venue` (or "
            f'"ccxt:{venue}") in those chains'
        )
    return declared


def _declared_environment(cfg: Any) -> str:
    """Where orders go: [execution].environment, else the live session's, else the venue's demo."""
    if cfg.execution.environment:
        return str(cfg.execution.environment)
    return str(cfg.live.environment) if cfg.live is not None else "demo"


def _environment_words(able: Any) -> str:
    if able.demo:
        return "this venue's demo API"
    if able.testnet:
        return "ccxt's sandbox switch, which is this venue's demo or testnet"
    return "this venue has no practice environment through ccxt"


# What `tra init --profile autonomous` writes where the operator's own provider goes. `tra check`
# names it rather than passing the vendor's "no such provider" error through.
PLACEHOLDER_PROVIDER = "your-provider"


def _refresh_news(path: Path, now: datetime, *, every: bool = False) -> list[str]:
    """Fetch each feed a news declaration names, when it is due or `every` says so."""
    from trading_research_agents.data.news import news_due, pull_news
    from trading_research_agents.data.pull import PullError
    from trading_research_agents.data.series import SeriesError, load_news_map

    try:
        specs = load_news_map(path)
    except SeriesError:
        return []
    said: list[str] = []
    base = path.resolve().parent
    for spec in specs:
        if spec.fetch is None or not (every or news_due(spec, base, now)):
            continue
        try:
            got = pull_news(spec, base, now=now)
        except PullError as exc:
            said.append(f"news: {exc}; the file on disk is read as it is")
        else:
            said.append(f"news: {got.series} refreshed, {got.added} new, {got.rows} kept")
    return said
