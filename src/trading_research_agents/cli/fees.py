"""Fees as the venue charges them: read from the account, written into the envelope, said in plain
words.
"""

from __future__ import annotations

import contextlib
import re
import tomllib
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.cli.common import _declared_environment
from trading_research_agents.cli.venue_access import _symbol_for


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
