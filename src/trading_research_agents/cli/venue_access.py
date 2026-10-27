"""Reaching a venue from the command line: its clients, its symbols, its universe and the hand that
sends orders.
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path
from typing import Any

from trading_research_agents.cli.common import _declared_environment, _environment
from trading_research_agents.envelope import load_envelope
from trading_research_agents.trading import (
    ConfigError,
    Ladder,
)

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
    from trading_research_agents.trading.dispatch import hand_for
    from trading_research_agents.trading.hand_state import credentials_for

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
