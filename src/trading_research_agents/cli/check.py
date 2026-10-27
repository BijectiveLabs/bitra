"""`tra check`: what a first run needs, checked in the order it would fail, said in plain words."""

from __future__ import annotations

import argparse
import tomllib
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.agents.context import (
    TEXT_CLASSES,
    declared_in,
    inspect_shelf,
)
from trading_research_agents.cli.check_gates import _gate_lines, _one_sided
from trading_research_agents.cli.common import (
    CLEARED,
    PLACEHOLDER_PROVIDER,
    REFUSED,
    _data_dir,
    _datasets,
    _desk_data,
    _environment,
    _has_live_plugin,
    _ui,
    emit,
)
from trading_research_agents.cli.fees import _fill_account_fees
from trading_research_agents.cli.text import (
    judge_lines,
)
from trading_research_agents.envelope import EnvelopeError, load_envelope
from trading_research_agents.extras import install_hint
from trading_research_agents.llm.providers import (
    UnknownProviderError,
    provider,
    providers,
)
from trading_research_agents.trading import (
    ConfigError,
    Ladder,
    Tier,
    load_strategy_config,
)
from trading_research_agents.trading.venues import (
    live_venue,
)

NOT_A_DESK = "`tra init my-desk --profile autonomous` makes one, or cd into yours"


def _env_file_used(env_file: Path | None, beside: Path | None) -> Path | None:
    """The file a check actually read a key from, for the line that says so."""
    for path in (env_file, (beside / ".env") if beside is not None else None):
        if path is not None and path.is_file():
            return path
    return None


def _renamed(path: Path) -> tuple[str, ...]:
    """What the desk's file still calls by a name from before 0.11, and the name to write now."""
    from trading_research_agents.trading.config_agents import renamed_in

    return renamed_in(tomllib.loads(path.read_text(encoding="utf-8"), parse_float=Decimal))


def check_project(args: argparse.Namespace) -> int:
    """What a first run needs, checked in the order it would fail, said in plain words."""
    good, bad, next_command = _check(args)
    lines = [*(f"ok    {g}" for g in good), *(f"todo  {b}" for b in bad)]
    lines.append(
        f"ready to run `{next_command}`" if not bad else f"{len(bad)} thing(s) to fix first"
    )
    emit({"ok": good, "todo": bad}, args.json, lines, lambda: _ui().check_table(good, bad))
    return CLEARED if not bad else REFUSED


def _check(args: argparse.Namespace) -> tuple[list[str], list[str], str]:
    good: list[str] = []
    bad: list[str] = []
    beside = args.config.resolve().parent
    env = _environment(args.env_file, beside=beside)
    read_from = _env_file_used(args.env_file, beside)
    if read_from is not None:
        good.append(f"keys: read from {read_from.name} beside the config, and from the environment")
    cfg = _config_read(args, good, bad)
    if cfg is not None:
        filled, refused = _fill_account_fees(args.envelope, cfg, env, datetime.now(UTC))
        good.extend(filled)
        bad.extend(refused)
    channels = _envelope_read(args, cfg, good, bad)
    if cfg is not None:
        good.extend(_desk_files(args, cfg, bad))
        shelf = declared_in(beside)
        good.extend(_shelf_notes(args, shelf, bad))
        readings = (cfg.agents.readings if cfg.agents is not None else ()) or shelf.readings
        roster = ("technical_analyst", *readings)
        read = f"{', '.join(readings)} read by code" if readings else "no reading declared"
        good.append(f"roster: technical_analyst asks a model; {read}; `tra run` uses these")
        ready, missing = _gate_lines(
            cfg,
            roster,
            project=beside,
            tier=Ladder(args.ladder).tier.name,
            env=env,
            entry_role=next(
                (c.entry_role for c in channels if c.key == cfg.run.venue_key), "undeclared"
            ),
        )
        good.extend(ready)
        bad.extend(missing)
        good.extend(_data_config_notes(beside / "data.toml", cfg, env, bad))
        good.extend(_provider_notes(cfg, env, bad))
        good.extend(_live_notes(args, cfg, env, bad))
    return good, bad, _next_command(args, cfg)


def _config_read(args: argparse.Namespace, good: list[str], bad: list[str]) -> Any:
    """The desk's strategy.toml, or nothing with the reason it could not be read."""
    cfg = None
    try:
        cfg = load_strategy_config(args.config)
        good.append(
            f"{args.config}: valid; core {cfg.strategy.name!r}; instrument {cfg.run.instrument}"
        )
        good.extend(f"written before 0.11, still read: {note}" for note in _renamed(args.config))
    except FileNotFoundError:
        bad.append(f"{args.config}: not here, so this folder is not a desk; {NOT_A_DESK}")
    except (ConfigError, OSError, ValueError) as exc:
        bad.append(f"{args.config}: {exc}")
    return cfg


def _envelope_read(
    args: argparse.Namespace, cfg: Any, good: list[str], bad: list[str]
) -> tuple[Any, ...]:
    """The envelope's channels, and whether the desk's venue key is one of them."""
    try:
        envelope = load_envelope(args.envelope)
    except FileNotFoundError:
        if args.config.is_file():
            bad.append(f"{args.envelope}: not here; `tra init` writes one beside strategy.toml")
        return ()
    except (EnvelopeError, OSError, ValueError) as exc:
        bad.append(f"{args.envelope}: {exc}")
        return ()
    good.append(f"{args.envelope}: {len(envelope.channels)} channels")
    if cfg is not None and not any(c.key == cfg.run.venue_key for c in envelope.channels):
        bad.append(f"[run].venue_key {cfg.run.venue_key!r} is not a channel in {args.envelope}")
    return tuple(envelope.channels)


def _desk_files(args: argparse.Namespace, cfg: Any, bad: list[str]) -> list[str]:
    """The dataset, the funding and the observation a desk keeps beside its config."""
    good: list[str] = []
    # An autonomous project decides on live chains, so a recorded dataset and a funding file are
    # what a backtest needs, not what a cycle needs. Saying otherwise sends an operator to fetch a
    # day of prints they will not use.
    cycles = cfg.agents is not None
    found = _datasets(args.config)
    if cfg.run.instrument in found:
        good.append(f"dataset: {found[cfg.run.instrument]}")
    elif cycles:
        good.append(
            f"dataset: none; `tra run` decides on the live chains, and `tra backtest` is the "
            f"one that would need `tra data get {cfg.run.instrument} YYYY-MM-DD`"
        )
    else:
        bad.append(
            f"no dataset for {cfg.run.instrument} in {_data_dir(args.config)}: "
            f"run `tra data get {cfg.run.instrument} YYYY-MM-DD`"
        )
    funding = _data_dir(args.config) / f"{cfg.run.instrument}-funding.json"
    if cfg.funding is not None or funding.is_file():
        good.append("funding: present")
    elif cycles and _declares_kind(args.config, "funding"):
        good.append("funding: live, from the chain in data.toml")
    else:
        bad.append(
            "funding: none; a run that spans a settlement will refuse (tra data get fetches it)"
        )
    good.extend(_observation_notes(args.config.resolve().parent / "observation.toml", bad))
    return good


def _observation_notes(observation: Path, bad: list[str]) -> list[str]:
    if not observation.is_file():
        return []
    from trading_research_agents.chain.observation import ObservationError, load_observation

    try:
        declared = load_observation(observation)
    except ObservationError as exc:
        bad.append(f"observation.toml: {exc}")
        return []
    return [f"observation.toml: {declared.subject!r}; `tra chain observe` before any memo"]


def _shelf_notes(args: argparse.Namespace, shelf: Any, bad: list[str]) -> list[str]:
    """What the sources shelf declares, whether it is on disk, and how fresh it is."""
    good: list[str] = []
    for reading in inspect_shelf(shelf):
        where = f"sources/{reading.name}.toml"
        if reading.error:
            bad.append(f"{where}: {reading.error}")
        elif reading.missing:
            bad.append(f"{where}: declared but not on disk: {', '.join(reading.missing)}")
        else:
            listed = ", ".join(reading.entries)
            good.append(f"{where}: {len(reading.entries)} declared ({listed})")
            good.extend(_one_sided_note(args.config.resolve().parent / where, where))
    fresh, stale = _source_lines(
        path for name, path in shelf.classes.items() if name not in TEXT_CLASSES
    )
    good.extend(fresh)
    bad.extend(stale)
    if shelf.empty:
        good.append(
            f"sources: {', '.join(shelf.empty)} on the shelf, nothing declared yet; "
            "fill one in and its reading joins the roster"
        )
    return good


def _one_sided_note(path: Path, where: str) -> list[str]:
    one_sided = _one_sided(path)
    if not one_sided:
        return []
    return [
        f"{where}: {', '.join(one_sided)} declare one side of the threshold "
        "only, so they count as a full +1 or -1 whenever crossed; declare a "
        "bullish and a bearish line and the score grades how far past it they sit"
    ]


def _data_config_notes(
    data_config: Path, cfg: Any, env: Mapping[str, str], bad: list[str]
) -> list[str]:
    """The chains data.toml declares, and the typed judge beside them."""
    if not data_config.is_file():
        if cfg.agents is not None:
            bad.append("data.toml: missing; `tra run` needs it (tra init writes one)")
        return []
    from trading_research_agents.data.sources import DataConfigError

    good: list[str] = []
    try:
        declared_data = _desk_data(data_config, cfg)
    except DataConfigError as exc:
        bad.append(f"data.toml: {exc}")
    else:
        kinds = ", ".join(k.kind for k in declared_data.kinds)
        good.append(f"data.toml: chains for {kinds}; `tra run` uses them")
        if not any(k.kind == "orderbook" for k in declared_data.kinds):
            bad.append(
                "data.toml: no [data.orderbook] chain, so a cycle cannot price a trade "
                "and `tra run` will stop at the cost gate"
            )
        good.extend(_agrees_across_files(cfg, declared_data, bad))
    judged, unjudged = judge_lines(data_config, env)
    good.extend(judged)
    bad.extend(unjudged)
    return good


def _provider_notes(cfg: Any, env: Mapping[str, str], bad: list[str]) -> list[str]:
    """Whether the model provider is named, keyed and installed, and the spares after it."""
    if cfg.agents is None:
        return []
    if cfg.agents.provider == PLACEHOLDER_PROVIDER:
        bad.append(
            "[agents].provider is still the placeholder; name the provider you have a key "
            f"for ({', '.join(providers())}), and a model `tra models <provider>` lists"
        )
        return []
    good: list[str] = []
    try:
        p = provider(cfg.agents.provider)
    except UnknownProviderError as exc:
        bad.append(str(exc))
    else:
        if p.key_env and not env.get(p.key_env):
            bad.append(f"{p.key_env} is not set; put it in the .env beside strategy.toml")
        elif not p.installed:
            module, extra = p.needs
            bad.append(
                f"provider {p.name}: the {module} SDK is not installed; " + install_hint(extra)
            )
        else:
            good.append(f"provider {p.name}: ready")
    return good + _spare_notes(cfg.agents)


def _spare_notes(agents: Any) -> list[str]:
    from trading_research_agents.cli.credentials import in_play

    good: list[str] = []
    for name, why in in_play(agents)[1:]:
        try:
            spare = provider(name)
        except UnknownProviderError:
            continue
        if not spare.installed:
            module, extra = spare.needs
            # A spare that cannot load is skipped when it is reached, so this is a note.
            good.append(
                f"provider {name} ({why}): the {module} SDK is not installed, so this link "
                f"is skipped if it is ever reached; " + install_hint(extra)
            )
    return good


def _live_notes(
    args: argparse.Namespace, cfg: Any, env: Mapping[str, str], bad: list[str]
) -> list[str]:
    if cfg.live is None:
        return []
    if not _has_live_plugin(cfg.live.venue):
        return [
            f"live: {cfg.live.venue} is not an exchange ccxt knows and no plugin names it, so "
            "`tra live` is not available there"
        ]
    good: list[str] = []
    tier = Ladder(args.ladder).tier
    if tier is Tier.CANARY:
        good.append(f"ladder: {tier.name}; live sessions may run")
    else:
        bad.append(f"ladder: {tier.name}; `tra live` needs CANARY (promote with evidence)")
    missing = [k for k in live_venue(cfg.live.venue).env_keys if not env.get(k)]
    if missing:
        bad.append(f"missing {', '.join(missing)} for the live venue")
    return good


def _next_command(args: argparse.Namespace, cfg: Any) -> str:
    if not args.config.is_file():
        return "tra init my-desk --profile autonomous"
    if cfg is not None and cfg.agents is not None:
        return "tra run"
    if cfg is not None and cfg.run.instrument not in _datasets(args.config):
        return f"tra data get {cfg.run.instrument} YYYY-MM-DD"
    return "tra backtest"


def _agrees_across_files(cfg: Any, data: Any, bad: list[str]) -> list[str]:
    """Whether the instrument and the venue mean the same thing in every file that names them.

    Three declarations sit in three files, and a desk that prices one venue's fees while taking its
    candles from another has made a decision without noticing. Nothing here decides for them; it
    says what the files add up to.
    """
    good: list[str] = []
    instrument = cfg.run.instrument
    venue = cfg.run.venue_key.partition("/")[0]
    symbols = dict(data.settings.get("symbols") or {})
    if instrument in symbols:
        good.append(f"symbols: {instrument} is {symbols[instrument]} to the sources")
    else:
        good.append(
            f"symbols: {instrument} is not mapped in data.toml, so each source looks it up itself"
        )
    named = {
        source.partition(":")[2].partition("@")[0]
        for kind in data.kinds
        for source in kind.chain
        if source.startswith("ccxt:")
    }
    strangers = sorted(named - {venue})
    if strangers:
        elsewhere = ", ".join(strangers)
        good.append(
            f"venues: fees are priced on {venue}, data also comes from {elsewhere}; "
            "deliberate is fine, accidental is not"
        )
    if cfg.execution.places_orders and cfg.execution.venue != venue:
        bad.append(
            f"[execution].venue is {cfg.execution.venue!r} but [run].venue_key prices {venue!r}; "
            "an order would go to one venue at the other's fee"
        )
    return good


def _declares_kind(config: Path, kind: str) -> bool:
    """Whether data.toml beside this config declares a chain for one kind of live figure."""
    where = config.resolve().parent / "data.toml"
    if not where.is_file():
        return False
    try:
        raw = tomllib.loads(where.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    table = raw.get("data")
    return isinstance(table, dict) and isinstance(table.get(kind), dict)


def _source_lines(declarations: Iterable[Path]) -> tuple[list[str], list[str]]:
    """Per series: where it comes from, the last value, its age, and whether a cycle counts it."""
    from trading_research_agents.data.pull import describe
    from trading_research_agents.data.series import SeriesError, load_series_map

    good: list[str] = []
    stale: list[str] = []
    now = datetime.now(UTC)
    for path in declarations:
        try:
            specs = load_series_map(path)
        except SeriesError:
            continue
        for spec in specs:
            seen = describe(spec, path.resolve().parent, now)
            named = seen["source"] or "no source named"
            head = f"series {seen['series']}: {seen['from']} ({named})"
            if "error" in seen:
                stale.append(f"{head}; {seen['error']}")
            elif seen["counted"]:
                good.append(f"{head}; last {seen['last'][:16]}, {seen['age_h']}h old")
            else:
                # Old data never blocks a cycle: the series is shown and not counted. Only a file
                # that cannot be read is something to fix.
                then = (
                    "`tra run` fetches it again before a cycle"
                    if spec.fetch is not None
                    else "refresh the file to count it"
                )
                good.append(
                    f"{head}; last {seen['last'][:16]}, {seen['age_h']}h old, past max_age "
                    f"{seen['max_age']}: shown but not counted until a newer value arrives; {then}"
                )
    return good, stale
