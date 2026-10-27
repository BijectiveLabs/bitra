"""What every command shares: the environment and its keys, the data and execution clients, fees
read from a venue, and how a result is printed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import tomllib
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.envelope import load_envelope
from trading_research_agents.llm import Completer
from trading_research_agents.llm.chain import CompleterChain, Link, parse_link
from trading_research_agents.llm.providers import (
    CallOptions,
    provider,
)
from trading_research_agents.trading import (
    ConfigError,
    Ladder,
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


def _read_fees(
    venue: str,
    keys: list[str],
    env: Mapping[str, str],
    environment: str,
    instrument: str,
    symbol: str | None = None,
) -> tuple[tuple[Decimal, Decimal] | None, str]:
    """Maker and taker in bps: the account's own, else the venue's standard tier, else nothing.

    The account's figure is the true one, and it is asked for first. Some demo and testnet APIs
    do not report fees at all (one demo answers every fee request with a parameter error), and a
    desk without keys yet has no account to ask. Then the venue's standard tier from its own
    market list is used and said to be: it is the base tier, the one a new account pays, so an
    account on a better tier pays less than the gate prices, never more.
    """
    from trading_research_agents.data.sources.resolve import ResolveError, resolve
    from trading_research_agents.trading.dispatch import DispatchError, account_fees, hand_for

    missing = [k for k in keys if not env.get(k)]
    why = f"{', '.join(missing)} not set" if missing else ""
    listed: Any = None
    if not missing:
        # With keys, everything is read through the client orders go through: the account's fee,
        # and failing that the market list of the environment orders go to. A venue's full public
        # list can be the slow part (Gate's timed out on 2026-10-20 while its testnet answered).
        try:
            probe = hand_for(
                venue=venue,
                symbol=symbol or instrument,
                max_notional=Decimal(1),
                env=env,
                environment=environment,
            )
            listed = probe.client
            listed.load_markets()
            # The symbol orders go out under, when the caller knows it: a spot pair and the
            # perpetual on the same coin are charged different fees.
            symbol = symbol or resolve(instrument, listed.markets_by_id, listed.markets.values())
            fees = account_fees(
                venue, symbol=symbol, env=env, environment=environment, client=listed
            )
            return fees, f"read from your {environment} API for {symbol}"
        except ResolveError as exc:
            return None, str(exc)
        except DispatchError as exc:
            why = str(exc)
        except Exception as exc:
            why = f"{exc.__class__.__name__}: {exc}"
        if symbol is None or not getattr(listed, "markets", None):
            listed = None
    if listed is None:
        try:
            from trading_research_agents.data.sources.ccxt_client import build

            listed = build(venue, {"enableRateLimit": True})
            if listed is None:
                raise AttributeError(f"ccxt has no exchange named {venue!r}")
            listed.load_markets()
            symbol = symbol or resolve(instrument, listed.markets_by_id, listed.markets.values())
        except (ResolveError, ImportError, AttributeError) as exc:
            return None, str(exc)
        except Exception as exc:
            return None, f"{exc.__class__.__name__}: {exc}"
    market = listed.markets.get(symbol) or {}
    maker, taker = market.get("maker"), market.get("taker")
    if maker is None or taker is None:
        return None, f"your account did not report them ({why}) and {venue} lists none"
    return (Decimal(str(maker)) * BPS_PER_UNIT, Decimal(str(taker)) * BPS_PER_UNIT), (
        f"{venue}'s standard tier from its market list, because your account did not report "
        f"its own ({why}); an account on a better tier pays less"
    )


BPS_PER_UNIT = Decimal(10_000)


FEE_LINE = re.compile(r"^\s*(entry|exit)_fee_bps\s*=\s*([^#]*?)\s*(#.*)?$")


STANDARD_MARK = "standard tier"


def _fill_account_fees(
    envelope: Path, cfg: Any, env: Mapping[str, str], when: datetime
) -> tuple[list[str], list[str]]:
    """The fees of the venue orders go to, filled in for the operator and kept up to date.

    A blank fee line is filled: from the account when it reports its fees, otherwise from the
    venue's standard tier, and the comment on the line says which. A line this filled from the
    standard tier is replaced by the account's own figure as soon as the account reports one. A
    fee the operator wrote is never replaced; when it is below what the account pays, that is
    reported, because the cost gate would then price trades cheaper than they are.

    A channel's base fees are checked against the perpetual, and its `[channel.spot]` fees against
    the spot pair; a desk that trades spot alone is checked against the spot pair throughout.
    """
    from trading_research_agents.trading.dispatch import credentials_for

    if not envelope.is_file() or cfg is None:
        return [], []
    venue = cfg.execution.venue or cfg.run.venue_key.partition("/")[0]
    lines = envelope.read_text(encoding="utf-8").splitlines()
    # (line number, market, channel name, role, value, comment), for this venue's fee lines.
    found_lines: list[tuple[int, str, str, str, str, str]] = []
    current_venue, current_name, current_market = "", "", ""
    for number, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[[channel]]":
            current_venue, current_name, current_market = "", "", ""
        elif stripped.startswith("[channel.") and stripped.endswith("]"):
            current_market = stripped[len("[channel.") : -1].strip()
        elif stripped.startswith("venue") and "=" in stripped:
            current_venue = stripped.partition("=")[2].split("#")[0].strip().strip('"')
        elif stripped.startswith("name") and "=" in stripped:
            current_name = stripped.partition("=")[2].split("#")[0].strip().strip('"')
        found = FEE_LINE.match(line)
        if not found or current_venue != venue:
            continue
        side, value, comment = found.group(1), found.group(2), found.group(3) or ""
        roles = current_name.split("-")
        role = roles[0] if side == "entry" else roles[-1]
        if role in ("maker", "taker"):
            found_lines.append((number, current_market, current_name, role, value, comment))
    if not found_lines:
        return [], []
    keys = list(credentials_for(venue).values())
    filling = any(not v or STANDARD_MARK in c for _, _, _, _, v, c in found_lines)
    if not filling and any(not env.get(k) for k in keys):
        # Nothing to fill, and no account to check a written fee against: no call is worth making.
        return [], []
    markets = tuple(getattr(cfg.execution, "markets", ("linear perpetual",)))
    spot_symbol = _spot_reference(cfg, envelope.parent) if "spot" in markets else None
    good: list[str] = []
    bad: list[str] = []
    changed = False
    with_spot = {name for _, m, name, _, _, _ in found_lines if m == "spot"}
    for market in sorted({m for _, m, _, _, _, _ in found_lines}):
        group = [row for row in found_lines if row[1] == market]
        priced_as_spot = market == "spot" or "linear perpetual" not in markets
        if market == "" and priced_as_spot:
            # A spot-only desk prices with a channel's spot table when it has one; its base fees
            # are then never used, so they are not held to the spot pair.
            group = [row for row in group if row[2] not in with_spot]
            if not group:
                continue
        if priced_as_spot and spot_symbol is None:
            continue
        fees, said = _read_fees(
            venue,
            keys,
            env,
            _declared_environment(cfg),
            cfg.run.instrument,
            spot_symbol if priced_as_spot else None,
        )
        label = " spot" if market == "spot" else ""
        if fees is None:
            if any(not v for _, _, _, _, v, _ in group):
                bad.append(f"fees{label}: not read for {venue} ({said}); the blank lines stay")
            continue
        from_account = said.startswith("read from your")
        if not from_account and all(env.get(k) for k in keys):
            # A key the venue does not accept is why orders would fail too. A key without the one
            # permission fees are read through is not: Gate keeps them behind wallet read, which a
            # trading key has no other use for (2026-10-20). `tra keys --probe` proves the rest.
            refused = "AuthenticationError" in said
            (bad if refused else good).append(f"fees{label}: {said}")
        by_role = {"maker": fees[0], "taker": fees[1]}
        stamp = f"{when:%Y-%m-%d}"
        origin = (
            f"read from your {venue} API on {stamp}"
            if from_account
            else f"{venue} {STANDARD_MARK}, {stamp}; replaced once your account reports its own"
        )
        filled = False
        flagged = False
        for number, _, name, role, value, comment in group:
            if not value or (STANDARD_MARK in comment and from_account):
                side = lines[number].split("=")[0].rstrip()
                lines[number] = f"{side} = {_bps(by_role[role])}   # your {role} fee, {origin}"
                filled = changed = True
                continue
            if STANDARD_MARK in comment:
                continue
            try:
                declared = Decimal(value)
            except (ArithmeticError, ValueError):
                continue
            # Only the account's own figure is a fact to hold a written fee to: a venue's market
            # list can lag its fee schedule (one listed 6 bps for a perpetual it charges 5.5 on).
            if not from_account or declared >= by_role[role]:
                continue
            flagged = True
            bad.append(
                f"fees: {envelope.name} prices {venue}/{name}{label} {role} at "
                f"{_bps(declared)} bps and your account pays {_bps(by_role[role])}; the cost "
                "gate would price trades too cheap"
            )
        if filled:
            good.append(
                f"fees{label}: maker {_bps(fees[0])} bps, taker {_bps(fees[1])} bps, {said}; "
                f"written into {envelope.name}"
            )
        written = any(v and STANDARD_MARK not in c for _, _, _, _, v, c in group)
        if written and not flagged and from_account:
            good.append(
                f"fees{label}: {envelope.name} matches or exceeds what your {venue} account pays"
            )
    if changed:
        envelope.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return good, bad


def _spot_reference(cfg: Any, project: Path) -> str | None:
    """A spot pair the desk declares, to read the venue's spot fees for: the run's own, else any."""
    with contextlib.suppress(Exception):
        own = _symbol_for(cfg, project, cfg.run.instrument)
        if "/" in own and ":" not in own:
            return own
    declared = project / "data.toml"
    if declared.is_file():
        with contextlib.suppress(Exception):
            raw = tomllib.loads(declared.read_text(encoding="utf-8"))
            for symbol in (raw.get("data", {}).get("symbols") or {}).values():
                if "/" in str(symbol) and ":" not in str(symbol):
                    return str(symbol)
    return None


def _bps(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.001")).normalize(), "f")


def _environment_words(able: Any) -> str:
    if able.demo:
        return "this venue's demo API"
    if able.testnet:
        return "ccxt's sandbox switch, which is this venue's demo or testnet"
    return "this venue has no practice environment through ccxt"


# What `tra init --profile autonomous` writes where the operator's own provider goes. `tra check`
# names it rather than passing the vendor's "no such provider" error through.
PLACEHOLDER_PROVIDER = "your-provider"


# What discovery found in this process, per instrument: the venue's own symbol and its size step.
# A discovered instrument has no line in data.toml or [run].size_step, and guessing either would
# send an order under the wrong market or off the venue's step.
_DISCOVERED: dict[str, dict[str, Any]] = {}


_DISCOVERY_CLIENTS: dict[str, Any] = {}


def _discovered_universe(cfg: Any) -> tuple[Any, dict[str, Any] | None, list[str]]:
    """This pass's universe: the declared instruments, plus what discovery found on the venue."""
    from trading_research_agents.data.sources.discover import (
        DiscoverError,
        Rules,
        aligned,
        discover,
        merged,
    )

    universe = cfg.universe
    if not universe.discover:
        return universe, None, []
    venue = cfg.execution.venue or cfg.run.venue_key.partition("/")[0]
    rules = Rules(
        quote=universe.quote,
        top=universe.top,
        min_quote_volume=universe.min_quote_volume,
        max_spread_bps=universe.max_spread_bps,
        min_listed_days=universe.min_listed_days,
        crypto_only=universe.crypto_only,
        exclude=universe.exclude,
        leverage=cfg.execution.leverage,
        type_field=universe.type_field,
        exclude_types=universe.exclude_types,
    )
    try:
        client = _DISCOVERY_CLIENTS.get(venue)
        if client is None:
            from trading_research_agents.data.sources.ccxt_client import build

            client = build(venue, {"enableRateLimit": True})
            if client is None:
                raise AttributeError(f"ccxt has no exchange named {venue!r}")
            _DISCOVERY_CLIENTS[venue] = client
        found = discover(client, venue, rules)
    except (DiscoverError, ImportError, AttributeError) as exc:
        why = f"discovery: {exc}; this pass looks at the declared instruments only"
        return universe.with_instruments(universe.instruments), None, [why]
    said: list[str] = []
    try:
        found = aligned(found, _execution_client(cfg), _declared_environment(cfg))
    except Exception as exc:
        said.append(
            f"discovery: the {_declared_environment(cfg)} market list of {venue} could not be read "
            f"({exc.__class__.__name__}); steps are the live market's, and an order off the "
            "environment's own step is refused at the venue hand"
        )
    for row in found.listed:
        _DISCOVERED[row.instrument] = {"symbol": row.symbol, "step": row.step}
    names = merged(universe.instruments, found)
    return universe.with_instruments(names), found.record(), said


def _venue_hand(
    args: argparse.Namespace,
    cfg: Any,
    project: Path,
    instrument: str = "",
    rate: Any = None,
    *,
    authorise: bool = True,
) -> Any:
    """What venue mode sends with, built only after everything standing in the way is cleared."""
    from trading_research_agents.execution.mandate import blocking
    from trading_research_agents.trading.dispatch import credentials_for, hand_for

    mandate = cfg.execution
    env = _environment(getattr(args, "env_file", None))
    keys = tuple(credentials_for(mandate.venue).values())
    stops = blocking(
        mandate,
        project=project,
        tier=Ladder(args.ladder).tier.name,
        missing_keys=[k for k in keys if not env.get(k)],
        practice=_declared_environment(cfg) in ("demo", "testnet"),
    )
    if stops and authorise:
        raise ConfigError("venue mode is not authorised here: " + "; ".join(stops))
    channel = next(
        (c for c in load_envelope(args.envelope).channels if c.key == cfg.run.venue_key), None
    )
    if channel is None:
        raise ConfigError(f"[run].venue_key {cfg.run.venue_key!r} is not a channel in the envelope")
    if channel.entry_role not in ("maker", "taker"):
        raise ConfigError(
            f"channel {channel.key!r} does not say whether entry is maker or taker, and the fee "
            "you priced depends on it; name it <entry>-<exit>, as in maker-taker"
        )
    return hand_for(
        venue=mandate.venue,
        symbol=_symbol_for(cfg, project, instrument),
        instrument=instrument or cfg.run.instrument,
        max_notional=mandate.max_notional,
        role=channel.entry_role,
        exits=mandate.exits,
        environment=_declared_environment(cfg),
        limit_timeout_s=mandate.limit_timeout_s,
        price_offset_bps=mandate.price_offset_bps,
        adopt_open_position=mandate.adopt_open_position,
        params=mandate.params,
        max_drift_bps=mandate.max_drift_bps,
        max_decision_age_s=mandate.max_decision_age_s,
        leverage=mandate.leverage,
        margin_mode=mandate.margin_mode,
        max_slippage_bps=mandate.max_slippage_bps,
        max_margin_share=mandate.max_margin_share,
        liquidation_buffer_bps=mandate.liquidation_buffer_bps,
        markets=mandate.markets,
        rate=rate,
        env=env,
        ccxt_options=mandate.ccxt_options,
    )


def _symbol_for(cfg: Any, project: Path, instrument: str = "") -> str:
    """The venue's own name for one instrument, as data.toml already maps it."""
    named = instrument or cfg.run.instrument
    declared = project / "data.toml"
    if declared.is_file():
        raw = tomllib.loads(declared.read_text(encoding="utf-8"))
        mapped = (raw.get("data", {}).get("symbols") or {}).get(named)
        if mapped:
            return str(mapped)
    if named in _DISCOVERED:
        return str(_DISCOVERED[named]["symbol"])
    if "/" in named:
        return str(named)
    return _on_the_execution_venue(cfg, str(named))


_EXECUTION_CLIENTS: dict[tuple[str, str], Any] = {}


def _execution_client(cfg: Any) -> Any:
    """A keyless client of the venue orders go to, in the environment they go to, markets loaded.

    A practice environment is not a copy of the live one. On 2026-10-08 one venue's demo listed 155
    linear perpetuals against 485 live, and 50 of the 138 on both had a different contract
    size or lot: MET is 1 coin a contract live and 10 on the demo. Whatever an order is sized,
    stepped or resolved by has to come from the list the order will be checked against.
    """
    from trading_research_agents.data.sources.ccxt_client import build

    venue = cfg.execution.venue or cfg.run.venue_key.partition("/")[0]
    environment = _declared_environment(cfg)
    key = (venue, environment)
    if key not in _EXECUTION_CLIENTS:
        client = build(venue, {"enableRateLimit": True})
        if client is None:
            raise AttributeError(f"ccxt has no exchange named {venue!r}")
        if environment == "demo":
            client.enable_demo_trading(True)
        elif environment == "testnet":
            client.set_sandbox_mode(True)
        client.load_markets()
        _EXECUTION_CLIENTS[key] = client
    return _EXECUTION_CLIENTS[key]


def _on_the_execution_venue(cfg: Any, name: str) -> str:
    """The market `name` stands for on the venue orders go to, which is the venue that decides.

    The same string can be a perpetual on one venue, spot on another and nothing on a third, so it
    is read against the execution venue's own market list. When that list cannot be read the name
    is returned as it was, and the venue hand still refuses any market type the desk did not list.
    """
    from trading_research_agents.data.sources.resolve import ResolveError, resolve

    venue = cfg.execution.venue or cfg.run.venue_key.partition("/")[0]
    try:
        client = _execution_client(cfg)
        return resolve(name, client.markets_by_id, client.markets.values())
    except ResolveError as exc:
        raise ConfigError(f"{venue} ({_declared_environment(cfg)}): {exc}") from exc
    except Exception:
        return name


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
