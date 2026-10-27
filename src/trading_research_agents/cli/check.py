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
from trading_research_agents.cli.common import (
    CLEARED,
    PLACEHOLDER_PROVIDER,
    REFUSED,
    _bps,
    _data_dir,
    _datasets,
    _declared_environment,
    _desk_data,
    _environment,
    _fill_account_fees,
    _has_live_plugin,
    _ui,
    emit,
)
from trading_research_agents.cli.text import (
    judge_lines,
)
from trading_research_agents.envelope import EnvelopeError, load_envelope
from trading_research_agents.extras import install_hint
from trading_research_agents.llm.chain import parse_link
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
from trading_research_agents.trading.config import RunSettings
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
    from trading_research_agents.trading.config import renamed_in

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
    channels: tuple[Any, ...] = ()
    if cfg is not None:
        filled, refused = _fill_account_fees(args.envelope, cfg, env, datetime.now(UTC))
        good.extend(filled)
        bad.extend(refused)
    try:
        envelope = load_envelope(args.envelope)
        channels = tuple(envelope.channels)
        good.append(f"{args.envelope}: {len(envelope.channels)} channels")
        if cfg is not None and not any(c.key == cfg.run.venue_key for c in envelope.channels):
            bad.append(f"[run].venue_key {cfg.run.venue_key!r} is not a channel in {args.envelope}")
    except FileNotFoundError:
        if args.config.is_file():
            bad.append(f"{args.envelope}: not here; `tra init` writes one beside strategy.toml")
    except (EnvelopeError, OSError, ValueError) as exc:
        bad.append(f"{args.envelope}: {exc}")
    if cfg is not None:
        # An autonomous project decides on live chains, so a recorded dataset and a funding file
        # are what a backtest needs, not what a cycle needs. Saying otherwise sends an operator to
        # fetch a day of prints they will not use.
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
        observation = args.config.resolve().parent / "observation.toml"
        if observation.is_file():
            from trading_research_agents.chain.observation import ObservationError, load_observation

            try:
                declared = load_observation(observation)
            except ObservationError as exc:
                bad.append(f"observation.toml: {exc}")
            else:
                good.append(
                    f"observation.toml: {declared.subject!r}; `tra chain observe` before any memo"
                )
        shelf = declared_in(args.config.resolve().parent)
        for reading in inspect_shelf(shelf):
            where = f"sources/{reading.name}.toml"
            if reading.error:
                bad.append(f"{where}: {reading.error}")
            elif reading.missing:
                bad.append(f"{where}: declared but not on disk: {', '.join(reading.missing)}")
            else:
                listed = ", ".join(reading.entries)
                good.append(f"{where}: {len(reading.entries)} declared ({listed})")
                one_sided = _one_sided(args.config.resolve().parent / where)
                if one_sided:
                    good.append(
                        f"{where}: {', '.join(one_sided)} declare one side of the threshold "
                        "only, so they count as a full +1 or -1 whenever crossed; declare a "
                        "bullish and a bearish line and the score grades how far past it they sit"
                    )
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
        readings = (cfg.agents.readings if cfg.agents is not None else ()) or shelf.readings
        roster = ("technical_analyst", *readings)
        read = f"{', '.join(readings)} read by code" if readings else "no reading declared"
        good.append(f"roster: technical_analyst asks a model; {read}; `tra run` uses these")
        ready, missing = _gate_lines(
            cfg,
            roster,
            project=args.config.resolve().parent,
            tier=Ladder(args.ladder).tier.name,
            env=env,
            entry_role=next(
                (c.entry_role for c in channels if c.key == cfg.run.venue_key), "undeclared"
            ),
        )
        good.extend(ready)
        bad.extend(missing)
        data_config = args.config.resolve().parent / "data.toml"
        if data_config.is_file():
            from trading_research_agents.data.sources import DataConfigError

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
        elif cfg.agents is not None:
            bad.append("data.toml: missing; `tra run` needs it (tra init writes one)")
        if cfg.agents is not None and cfg.agents.provider == PLACEHOLDER_PROVIDER:
            bad.append(
                "[agents].provider is still the placeholder; name the provider you have a key "
                f"for ({', '.join(providers())}), and a model `tra models <provider>` lists"
            )
        elif cfg.agents is not None:
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
                        f"provider {p.name}: the {module} SDK is not installed; "
                        + install_hint(extra)
                    )
                else:
                    good.append(f"provider {p.name}: ready")
            from trading_research_agents.cli.credentials import in_play

            for name, why in in_play(cfg.agents)[1:]:
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
        if cfg.live is not None and not _has_live_plugin(cfg.live.venue):
            good.append(
                f"live: {cfg.live.venue} is not an exchange ccxt knows and no plugin names it, so "
                "`tra live` is not available there"
            )
        elif cfg.live is not None:
            tier = Ladder(args.ladder).tier
            if tier is Tier.CANARY:
                good.append(f"ladder: {tier.name}; live sessions may run")
            else:
                bad.append(f"ladder: {tier.name}; `tra live` needs CANARY (promote with evidence)")
            missing = [k for k in live_venue(cfg.live.venue).env_keys if not env.get(k)]
            if missing:
                bad.append(f"missing {', '.join(missing)} for the live venue")
    next_command = "tra backtest"
    if not args.config.is_file():
        next_command = "tra init my-desk --profile autonomous"
    elif cfg is not None and cfg.agents is not None:
        next_command = "tra run"
    elif cfg is not None and cfg.run.instrument not in _datasets(args.config):
        next_command = f"tra data get {cfg.run.instrument} YYYY-MM-DD"
    return good, bad, next_command


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


def _gate_lines(
    cfg: Any,
    roster: tuple[str, ...],
    *,
    project: Path,
    tier: str,
    env: Mapping[str, str],
    entry_role: str = "undeclared",
) -> tuple[list[str], list[str]]:
    """What the two declared gates will do, and where they contradict the rest of the project.

    A gate that can never pass is worth saying out loud here, before a cycle spends a token finding
    out: a required role nobody asks, or a vote count nobody can cast.
    """
    good: list[str] = []
    bad: list[str] = []
    matrix = cfg.confluence
    stances = () if cfg.agents is None else cfg.agents.validators
    if stances:
        from trading_research_agents.agents.cycle import judge_mode

        weight = cfg.conviction.validator_weight
        how = (
            "; the advocates build their cases apart and neutral judges them, its verdict the one "
            "that counts; with a case on one side only, code decides without asking it"
            if judge_mode(stances)
            else ""
        )
        good.append(
            f"validators: {', '.join(stances)}; asked once there is a proposal and the system "
            "has not already refused it"
            + how
            + (f"; each counted vote moves conviction by {weight}" if weight > 0 else "")
        )
    if matrix.idle:
        good.append("confluence: not declared; the context is recorded but gates nothing")
    else:
        good.append(
            f"confluence: needs {', '.join(matrix.required) or 'no role'}; "
            f"long {list(matrix.long_states)}, short {list(matrix.short_states)}; "
            f"a missing state means {matrix.on_missing}"
        )
        absent = sorted(set(matrix.required) - set(roster))
        if absent:
            bad.append(
                f"[confluence].required names {absent}, which the roster does not read; "
                "declare their sources or the cycle refuses every time"
            )
        if matrix.min_support > len(stances):
            bad.append(
                f"[confluence].min_support is {matrix.min_support} but "
                f"{len(stances)} validator(s) are asked; no cycle can clear it"
            )
    from trading_research_agents.execution.mandate import blocking

    mandate = cfg.execution
    if entry_role in ("maker", "taker"):
        crosses = "crosses the book at once" if entry_role == "taker" else "rests on the book"
        good.append(
            f"channel: {cfg.run.venue_key} enters as a {entry_role}, priced at that fee; "
            f"an order {crosses}"
        )
    if not mandate.places_orders:
        good.append(
            f"execution: {mandate.mode}; nothing leaves the machine"
            if mandate.mode == "sandbox"
            else "execution: signals; intents are written for your own executor"
        )
    else:
        from trading_research_agents.trading.dispatch import credentials_for, key_hint

        keys = tuple(credentials_for(mandate.venue).values())
        stops = blocking(
            mandate,
            project=project,
            tier=tier,
            missing_keys=[k for k in keys if not env.get(k)],
            practice=_declared_environment(cfg) in ("demo", "testnet"),
        )
        if stops:
            bad.extend(f"execution: {stop}" for stop in stops)
            hint = key_hint([k for k in keys if not env.get(k)], keys, env)
            if hint:
                bad.append(f"execution: {hint}")
        else:
            good.append(
                f"execution: venue {mandate.venue}, signed by {mandate.acknowledged_by} on "
                f"{mandate.acknowledged_on}, at most {mandate.max_notional} per order"
            )
            # Two declarations that only meet at the last gate, after a cycle has been paid for.
            # The price where they stop agreeing is arithmetic, and it can be said here instead.
            if mandate.max_notional is not None:
                _size_against_cap(cfg, mandate.max_notional, good, bad)
            good.extend(_capital_notes(cfg))
            if mandate.exits == "attached":
                good.append(
                    "execution: the stop and the take profit go to the venue with the entry, so a "
                    "fill is never left unattended"
                )
            elif mandate.exits == "separate":
                good.append(
                    "execution: the stop and the take profit go to the venue as reduce-only orders "
                    "right after the fill; a fill they cannot be placed for is closed at once"
                )
            else:
                bad.append(
                    '[execution].exits is "none": a fill would sit at the venue with no stop and '
                    "no take profit, and only you would be carrying them"
                )
            if entry_role == "maker":
                good.append(
                    "execution: an order that has not filled is cancelled after "
                    f"{mandate.limit_timeout_s}s; nothing is chased across the spread"
                )
            elif entry_role == "undeclared":
                bad.append(
                    f"channel {cfg.run.venue_key!r} does not say whether entry is maker or taker; "
                    "the fee you priced depends on it"
                )
    if cfg.agents is not None and cfg.agents.fallback:
        unknown = []
        for name in cfg.agents.fallback:
            named, _ = parse_link(name)
            if named not in providers():
                unknown.append(name)
        if unknown:
            bad.append(f"[agents].fallback names {unknown}, which no provider is registered for")
        else:
            good.append(
                f"models: fallback {', '.join(cfg.agents.fallback)}; "
                "tried in order when a provider cannot be reached"
            )
    if cfg.agents is not None:
        good.extend(_load_notes(cfg.agents, roster, screener=cfg.universe.screener))
    good.append(_regime_note(cfg))
    rule = cfg.conviction
    if rule.idle:
        good.append("conviction: not declared; every admitted cycle trades the base size")
    else:
        rungs = ", ".join(f"{t.at_least}+ -> {t.size_multiple}x" for t in rule.ladder)
        good.append(
            f"conviction: floor {rule.min_score}; ladder {rungs or 'none'}"
            + (
                f"; notional at most {rule.max_leverage}x the equity (a cap on exposure, not the "
                "venue's leverage, which is [execution].leverage)"
                if rule.max_leverage
                else ""
            )
        )
        good.append(
            "conviction: every voice counts as strong as it is - the deciding analyst by its "
            "confidence, a reading by how far its series sit past your lines - and the "
            "score is their net over what they expressed; opposition shrinks the size, and only "
            "opposition that outweighs the case refuses the trade"
        )
        if not rule.ladder:
            bad.append(
                "[conviction] has no ladder, so no score can size a trade; "
                "declare [[conviction.ladder]] rungs or drop the table"
            )
        above = [t for t in rule.ladder if t.size_multiple > 1]
        if above:
            good.append(
                f"conviction: {len(above)} rung(s) above 1x let agreement add size beyond the "
                "base; the risk per trade still caps the loss at the stop, and `tra hindsight` "
                "says whether the score has separated good trades from bad before you rely on it"
            )
        unweighted = sorted(set(roster) - set(rule.weights))
        if unweighted:
            good.append(
                f"conviction: {unweighted} is on the roster but carries no weight; "
                "it explains without moving the size"
            )
    asked = cfg.agents.validators if cfg.agents is not None else ()
    gated = cfg.confluence.min_support > 0 or cfg.confluence.max_opposition is not None
    if asked and not gated and rule.validator_weight <= 0:
        bad.append(
            f"validators: {', '.join(asked)} are asked every cycle and their votes move nothing; "
            "give them [conviction].validator_weight, or [confluence].min_support / "
            "max_opposition, or stop asking them"
        )
    targets = cfg.targets
    if targets.idle:
        good.append(
            "targets: not declared; only the cost gate stands between a claim and an intent"
        )
    else:
        said = []
        if targets.min_net_bps is not None:
            said.append(f"at least {targets.min_net_bps} bps net")
        if targets.min_edge_multiple is not None:
            said.append(f"at least {targets.min_edge_multiple}x the round trip")
        if targets.min_reward_risk is not None:
            said.append(f"reward-to-risk {targets.min_reward_risk}")
        if targets.max_research_bps is not None:
            said.append(f"at most {targets.max_research_bps} bps of notional in model calls")
        if targets.max_round_trips_per_day is not None:
            said.append(f"at most {targets.max_round_trips_per_day} round trips a day")
        good.append("targets: " + "; ".join(said))
    if targets.min_edge_multiple is None:
        good.append(
            "targets: no edge multiple declared, so a trade worth two basis points more than it "
            "costs can still pass; [targets].min_edge_multiple is what stops that"
        )
    priced = cfg.agents is not None and any(i > 0 or o > 0 for i, o in cfg.agents.pricing.values())
    if targets.max_research_bps is not None and not priced:
        bad.append(
            "[targets].max_research_bps is declared and no model price is, so the research cost "
            "reads as zero whatever the models spend; put what you were quoted in "
            "[agents.pricing.<model>] or drop the target"
        )
    good.extend(_exit_notes(cfg, bad))
    good.extend(_risk_notes(cfg, bad))
    good.extend(_trigger_notes(cfg))
    good.extend(_spend_notes(cfg))
    good.extend(_audit_notes(cfg))
    good.extend(_universe_notes(cfg, bad))
    good.extend(_size_notes(cfg))
    good.extend(_desk_notes(cfg, bad))
    good.append(_limit_line(cfg))
    good.append(
        "keys: this check reads which are set, not whether they work; `tra keys --probe` asks each "
        "provider and the venue itself, sends no prompt and places no order"
    )
    return good, bad


def _limit_line(cfg: Any) -> str:
    """Every daily line and switch the desk declared, in one sentence."""
    said = [f"daily loss {cfg.limits.daily_loss_limit_bps} bps"]
    if cfg.limits.max_drawdown_bps is not None:
        said.append(f"drawdown {cfg.limits.max_drawdown_bps} bps")
    if cfg.limits.max_trades_per_day is not None:
        said.append(f"{cfg.limits.max_trades_per_day} trades a day")
    if cfg.limits.max_losing_streak is not None:
        said.append(f"{cfg.limits.max_losing_streak} losers in a row")
    a = cfg.agents
    if a is not None and a.daily_token_budget is not None:
        said.append(f"{a.daily_token_budget} tokens a day")
    if a is not None and a.daily_cost_budget is not None:
        said.append(f"{a.daily_cost_budget} a day in model cost")
    switches = [f"`tra pause` ({cfg.pause_switch})"] if cfg.pause_switch is not None else []
    if cfg.kill_switch is not None:
        switches.append(f"`tra kill` ({cfg.kill_switch})")
    return "limits: " + ", ".join(said) + "; switches: " + (", ".join(switches) or "none")


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


def _one_sided(path: Path) -> list[str]:
    """Series on a shelf file whose threshold names only bullish or only bearish lines."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    found = []
    for entry in raw.get("series") or []:
        threshold = entry.get("threshold") if isinstance(entry, dict) else None
        if not isinstance(threshold, dict) or not threshold:
            continue
        bull = any(str(k).startswith("bullish_") for k in threshold)
        bear = any(str(k).startswith("bearish_") for k in threshold)
        if bull != bear:
            found.append(str(entry.get("name", "?")))
    return found


def _size_notes(cfg: Any) -> list[str]:
    """What one unit of base size means on each instrument this desk can trade."""
    run = cfg.run
    stated = run.size_step == RunSettings.__dataclass_fields__["size_step"].default
    if run.size_steps:
        steps = "the step " + ", ".join(f"{name} {step}" for name, step in run.size_steps.items())
    elif stated:
        steps = "the step the venue states for each instrument, in the environment orders go to"
    else:
        steps = f"the step {run.size_step}"
    if run.size_quote is not None:
        found = (
            "; discovered instruments use the venue's own step"
            if cfg.universe.discover and not stated
            else ""
        )
        said = [
            f"size: {run.size_quote} of quote currency, converted at each cycle's own mid and "
            f"rounded down to {steps}{found}; a replay still needs [run].size",
        ]
        cap = cfg.limits.max_position_quote
        if cap is not None:
            said.append(
                f"size: every position is capped at {cap} of quote currency at its own mid; "
                f"[controls].max_position {cfg.limits.max_position} units applies to "
                f"{run.instrument} as well"
            )
        else:
            said.append(
                f"size: [controls].max_position {cfg.limits.max_position} is in base units, so it "
                "caps each instrument at that many units whatever they are worth; "
                "[controls].max_position_quote says it in quote currency"
            )
        return said
    said = [f"size: {run.size} in base units, at {steps}"]
    if not cfg.universe.idle:
        said.append(
            f"size: {run.size} is a different amount of money on each of "
            f"{', '.join(cfg.universe.instruments)}; [run].size_quote says it once, in quote "
            "currency"
        )
    return said


def _regime_note(cfg: Any) -> str:
    from trading_research_agents.agents.briefs import interval_name

    rule = cfg.regime
    frames = ", ".join(interval_name(s) for s in cfg.run.context_intervals)
    return (
        f"regime: measured on {interval_name(cfg.run.bar_interval_s)}"
        + (f" with {frames} as context" if frames else ", no higher timeframe declared")
        + f"; trending at {rule.trend_above}+, ranging at {rule.range_below} or less; "
        + (
            "a setup that does not fit is refused"
            if rule.setups_must_fit
            else "a setup that does not fit is noted and allowed"
        )
    )


def _span_s(seconds: float) -> str:
    whole = int(seconds)
    for size, unit in ((86_400, "d"), (3_600, "h"), (60, "m")):
        if whole and whole % size == 0:
            return f"{whole // size}{unit}"
    return f"{whole}s"


def _load_notes(a: Any, roster: tuple[str, ...], screener: bool = False) -> list[str]:
    """How a cycle's calls land on providers, and what happens when one of them says no."""
    # The readings are read by code; the analyst and its team are the models a cycle asks.
    asked = ["technical_analyst", *a.team]
    reading = [name for name in (a.readings or roster) if name != "technical_analyst"]
    roles = [
        *asked,
        *(["validator"] if a.validators else []),
        *(["market_screener"] if screener else []),
    ]
    calls = len(asked) + len(a.validators) + (1 if screener else 0)
    on = sorted({a.for_role(role).provider for role in roles})
    said = [
        f"models: a full pass makes at most {calls} call(s) within a minute"
        + (" (the screener, then the cycle)" if screener else "")
        + f", on {', '.join(on)}; a provider that says wait up to {a.max_retry_wait_s:.0f}s is "
        "waited for once, a provider whose quota is spent rests 15 minutes, then the next link "
        "is tried"
    ]
    if reading:
        said.append(
            f"readers: {', '.join(reading)} are read by code against your thresholds and ask no "
            "model"
        )
    if "technical_peer" in a.team:
        lead, peer = a.for_role("technical_analyst"), a.for_role("technical_peer")
        if (lead.provider, lead.model) == (peer.provider, peer.model):
            said.append(
                "team: technical_peer runs on the same model as the technical analyst, and two "
                "reads from one model err together; give it another family in "
                "[agents.roles.technical_peer]"
            )
    # Vendors meter per model, so two models on one provider are two sets of limits.
    pairs = sorted({f"{a.for_role(r).provider}:{a.for_role(r).model}" for r in roles})
    if len(pairs) == 1 and calls >= 4:
        said.append(
            f"models: every role is on {pairs[0]}; if your tier limits tokens per minute or per "
            "day, give the table-reading roles another model or provider in [agents.roles.<role>]"
        )
    elif len(on) == 1:
        said.append(
            f"models: the roles share {on[0]} across {', '.join(pairs)}, each model on "
            "its own limits"
        )
    if a.validators:
        said.append(
            "models: a declared validator that no model answers "
            + (
                "refuses the trade"
                if a.on_missing_validator == "refuse"
                else "is left out and the trade goes on"
            )
            + " ([agents].on_missing_validator)"
        )
    return said


def _capital_notes(cfg: Any) -> list[str]:
    """What venue mode does with capital, said in the order it happens."""
    mandate = cfg.execution
    hold = {
        "timeout": f"after [exit].timeout_bars ({cfg.exit_rule.timeout_bars} bars)",
        "horizon": "after the analyst's own horizon, capped by [exit].timeout_bars",
        "none": "never by time; only its stop or take profit closes it",
    }[mandate.hold_limit]
    said = [
        f"capital: {mandate.leverage}x {mandate.margin_mode}, applied at the venue before the "
        "first order and read back; an order whose estimated liquidation sits inside the stop "
        f"plus {mandate.liquidation_buffer_bps} bps is refused",
        "capital: sizing uses the smaller of the account's equity and [run].equity; an order "
        f"may use at most {mandate.max_margin_share} of the free margin",
        f"capital: no new entry once equity is down {cfg.limits.daily_loss_limit_bps} bps on the "
        f"day; at most {cfg.limits.max_orders_per_minute} orders a minute",
        _drawdown_line(cfg.limits),
        f"orders: a taker entry is a limit IOC at most {mandate.max_slippage_bps} bps through the "
        "touch, never a naked market order",
        f"orders: a decision older than {mandate.max_decision_age_s:.0f}s is not sent; the book is "
        "read again just before sending"
        + (
            f", and an order is refused when the mid moved {mandate.max_drift_bps} bps against it"
            if mandate.max_drift_bps is not None
            else ""
        )
        + "; each order records its age, the venue's answer time and its slippage",
        f"positions: an open position is closed with a reduce-only order {hold}; the kill switch "
        "flattens every position before the run stops",
    ]
    if mandate.hold_limit == "none":
        said.append(
            "positions: hold_limit is none, so a position the market never takes to its stop or "
            "target stays open, and holds back new entries on correlated instruments"
        )
    return said


def _size_against_cap(cfg: Any, cap: Decimal, good: list[str], bad: list[str]) -> None:
    """Two declarations that only meet at the last gate, after a cycle has been paid for.

    Where they stop agreeing is arithmetic, so it is said here instead of at the venue.
    """
    run = cfg.run
    top = max((rung.size_multiple for rung in cfg.conviction.ladder), default=Decimal(1))
    if run.size_quote is None:
        if run.size > 0:
            ceiling = (cap / run.size).quantize(Decimal("0.01"))
            good.append(
                f"execution: {run.size} at a price above {ceiling} is more than {cap} of "
                "notional, and every order would be refused at the venue gate; that is the size "
                "and the cap talking to each other"
            )
        return
    if run.size_quote > cap:
        bad.append(
            f"[run].size_quote {run.size_quote} is above [execution].max_notional {cap}, so every "
            "order would be refused at the venue gate after the cycle had been paid for"
        )
    elif run.size_quote * top > cap:
        good.append(
            f"execution: the top rung of the ladder asks for {run.size_quote * top} of notional "
            f"and the cap is {cap}; orders at that rung are refused at the venue gate"
        )
    else:
        good.append(
            f"execution: at most {run.size_quote * top} of notional per order on any instrument, "
            f"inside the cap of {cap}"
        )


def _desk_notes(cfg: Any, bad: list[str]) -> list[str]:
    """What the whole book may carry at once, and whether that can actually be measured."""
    desk = cfg.desk
    if desk.idle:
        return [
            "desk: not declared, so nothing bounds the book as a whole; [desk].max_open_risk_bps "
            "is the sum of what every open position loses at its own stop"
        ]
    said = []
    if desk.max_open_risk_bps is not None:
        said.append(f"at most {desk.max_open_risk_bps} bps of equity at risk across the book")
    if desk.max_open_positions is not None:
        said.append(f"at most {desk.max_open_positions} position(s) open at once")
    if desk.correlation_above is not None:
        said.append(
            f"a candidate moving with an open position at {desk.correlation_above} or above is "
            "refused by the screen"
        )
    if desk.max_open_risk_bps is not None and cfg.limits.max_risk_per_trade_bps is None:
        bad.append(
            "[desk].max_open_risk_bps sums what each position risks, and "
            "[controls].max_risk_per_trade_bps is what puts that figure on a trade; declare it, or "
            "the sum has nothing to add up"
        )
    if desk.correlation_above is not None and cfg.universe.idle:
        bad.append(
            "[desk].correlation_above compares one instrument with another and [universe] names "
            "only one; declare the instruments this desk watches or drop the threshold"
        )
    return ["desk: " + "; ".join(said)]


def _universe_notes(cfg: Any, bad: list[str]) -> list[str]:
    """What this desk watches, and what a pass over it will do."""
    universe = cfg.universe
    if universe.idle:
        return [
            f"universe: not declared, so every cycle is {cfg.run.instrument} from [run]; "
            "[universe].instruments is where a desk names what else it watches"
        ]
    named = ", ".join(universe.instruments) or "nothing declared"
    said = [
        f"universe: {named}"
        + (f" always, plus up to {universe.top} discovered" if universe.discover else "")
        + f"; a pass looks at each for the price of a data pull and runs {universe.max_per_cycle} "
        "of them"
    ]
    if universe.discover:
        said.append(
            f"universe: discovery adds the {universe.top} most traded {universe.quote} "
            f"perpetuals each pass (at least {universe.min_quote_volume:,} a day, spread at most "
            f"{universe.max_spread_bps} bps, listed {universe.min_listed_days}+ days"
            + (", crypto only" if universe.crypto_only else "")
            + "); `tra universe` shows them now"
        )
    if universe.max_intents_per_day is not None:
        said.append(
            f"universe: at most {universe.max_intents_per_day} intent(s) a day across every "
            "instrument, counted from what is on disk"
        )
    if universe.screener:
        said.append(
            "universe: the screener picks and orders what the system admitted, citing each "
            "instrument's own figures; it cannot add an instrument, and the system's order "
            "is kept beside its own"
        )
    if cfg.trigger.idle:
        bad.append(
            "[universe] is declared and [trigger] is not, so every pass would ask the whole roster "
            "about every instrument; [trigger].min_move_bps is what makes a pass cheap"
        )
    return said


def _audit_notes(cfg: Any) -> list[str]:
    """When the auditors are asked without anyone typing the command."""
    policy = cfg.audit
    if policy.idle:
        return [
            "auditors: asked only by hand (`tra audit`); a provider that stops answering halfway "
            "through a run will not be looked at until you look"
        ]
    said = []
    if policy.on_alarm:
        said.append(
            f"the technical auditor wakes on an alarm, at most once per kind every "
            f"{policy.alarm_cooldown_s}s"
        )
    if policy.on_alarm:
        streak = (
            f" or after {policy.losing_streak} losing round trips in a row"
            if policy.losing_streak
            else ""
        )
        said.append(f"the quant auditor wakes when a loss limit is reached{streak}")
    if policy.quant_every_cycles is not None:
        said.append(f"the quant auditor runs every {policy.quant_every_cycles} cycles")
    if policy.quant_every_s is not None:
        said.append(f"the quant auditor runs every {_span_s(policy.quant_every_s)}")
    if policy.risk_manager:
        said.append(
            "the risk manager is asked with the quant auditor and may only cut size, pause an "
            "instrument or halt, each for a stated number of hours"
        )
    return ["auditors: " + "; ".join(said) + "; the auditors report and never act"]


def _spend_notes(cfg: Any) -> list[str]:
    """What the models may spend, what they are asked to spend it on, and who is counting."""
    a = cfg.agents
    if a is None:
        return []
    out: list[str] = []
    ceilings = []
    if a.token_budget is not None:
        ceilings.append(f"{a.token_budget} tokens for the whole process")
    if a.max_calls_per_hour is not None:
        ceilings.append(f"{a.max_calls_per_hour} calls an hour per role")
    if a.max_output_tokens is not None:
        ceilings.append(f"{a.max_output_tokens} output tokens a call")
    if ceilings:
        out.append(
            "spend: capped at " + "; ".join(ceilings) + "; a cycle that reaches a ceiling stops"
        )
    else:
        out.append(
            "spend: nothing is capped; every call is counted in usage.jsonl, each cycle reports "
            "what it cost, and the auditors read those records"
        )
    if a.effort is None:
        out.append(
            "effort: not declared, so each vendor's own default applies; [agents].effort sets it "
            "for every role and [agents.roles.<role>].effort overrides one"
        )
    else:
        roles = (
            "technical_analyst",
            "validator",
            "market_screener",
        )
        levels: dict[str, list[str]] = {}
        for role in roles:
            levels.setdefault(str(a.for_role(role).effort), []).append(role)
        out.append(
            "effort: "
            + "; ".join(f"{level} for {', '.join(who)}" for level, who in sorted(levels.items()))
        )
    quoted = sorted(m for m, (i, o) in a.pricing.items() if i > 0 or o > 0)
    free = sorted(m for m, (i, o) in a.pricing.items() if i == 0 and o == 0)
    used = {
        a.for_role(role).model or "default"
        for role in ("technical_analyst", "validator", "market_screener")
    }
    used |= {name.partition(":")[2] for name in a.fallback}
    unpriced = sorted(m for m in used if m not in a.pricing and m != "default")
    parts = []
    if quoted:
        parts.append(f"declared for {', '.join(quoted)}")
    if free:
        parts.append(f"declared free for {', '.join(free)}")
    if unpriced:
        parts.append(f"not declared for {', '.join(unpriced)}, so its calls are counted in tokens")
    out.append(
        "prices: "
        + ("; ".join(parts) if parts else "none quoted, so spend is counted in tokens only")
        + "; [agents.pricing.<model>] is where a quote goes"
    )
    return out


def _drawdown_line(limits: Any) -> str:
    if limits.max_drawdown_bps is None:
        return (
            "capital: no drawdown limit across days; [controls].max_drawdown_bps stops new entries "
            "once equity falls that far from its peak"
        )
    then = (
        "and the kill switch flattens and stops the run"
        if limits.on_drawdown == "flatten"
        else "until runs/cycles/equity_peak.json is cleared; open positions keep their exits"
    )
    return (
        f"capital: no new entry once equity is {limits.max_drawdown_bps} bps below its peak, "
        + then
    )


def _exit_notes(cfg: Any, bad: list[str]) -> list[str]:
    """Where each trade's stop and take profit will sit, in words."""
    rule = cfg.exit_rule
    scale = rule.volatility
    if scale is None:
        take = (
            "no take profit"
            if rule.take_profit_frac is None
            else (f"take profit {_bps(rule.take_profit_frac * 10_000)} bps")
        )
        return [
            f"exits: fixed, stop {_bps(rule.stop_frac * 10_000)} bps and {take} on every trade; "
            '[exit].mode = "volatility" sizes them to the range of each market'
        ]
    agents = getattr(cfg, "agents", None)
    lookback = getattr(agents, "lookback_bars", None)
    if lookback is not None and lookback < scale.atr_bars + 1:
        bad.append(
            f"[exit].atr_bars is {scale.atr_bars} and [agents].lookback_bars is {lookback}; the "
            f"average true range needs {scale.atr_bars + 1} closed candles"
        )
    return [
        (
            f"exits: the analyst names its stop and target; the stop is held between "
            f"{_bps(scale.min_stop_frac * 10_000)} and {_bps(scale.max_stop_frac * 10_000)} bps "
            f"and never inside {scale.min_stop_atr} ATR, the target between "
            f"{scale.min_reward_to_risk}x and {scale.max_reward_to_risk}x the "
            f"stop; with no level named, {scale.stop_atr} x ATR({scale.atr_bars}) and "
            f"{scale.reward_to_risk}x"
        )
        if scale.analyst_levels
        else (
            f"exits: measured per trade, stop {scale.stop_atr} x ATR({scale.atr_bars}) grown with "
            f"the horizon, held between {_bps(scale.min_stop_frac * 10_000)} and "
            f"{_bps(scale.max_stop_frac * 10_000)} bps; take profit "
            f"{scale.reward_to_risk} x the stop"
        )
    ]


def _risk_notes(cfg: Any, bad: list[str]) -> list[str]:
    """Whether the risk per trade the operator declared can actually be applied to a size."""
    allowed = cfg.limits.max_risk_per_trade_bps
    if allowed is None:
        return [
            "risk per trade: not declared; the conviction ladder and [controls].max_position are "
            "all that bound a size"
        ]
    if cfg.exit_rule.stop_frac is None:
        bad.append(
            f"[controls].max_risk_per_trade_bps is {allowed} and no stop is declared, so the loss "
            "at the stop cannot be computed; declare [exit].stop_frac"
        )
        return []
    if cfg.run.equity is None:
        bad.append(
            f"[controls].max_risk_per_trade_bps is {allowed} and [run].equity is not set, so there "
            "is nothing to measure the loss against"
        )
        return []
    return [
        f"risk per trade: {allowed} bps of equity at the stop, applied to every size the ladder "
        "produces and cut to fit rather than sent"
    ]


def _trigger_notes(cfg: Any) -> list[str]:
    """What a quiet market costs this desk, which is the whole point of the trigger."""
    trigger = cfg.trigger
    if trigger.idle:
        return [
            "trigger: not declared, so every scheduled cycle pays the whole roster whether or not "
            "anything moved; [trigger].min_move_bps is what makes a quiet hour cheap"
        ]
    said = []
    if trigger.min_move_bps is not None:
        said.append(f"{trigger.min_move_bps} bps of movement since the last look")
    if trigger.max_spread_bps is not None:
        said.append(f"a book no wider than {trigger.max_spread_bps} bps")
    if trigger.cooldown_s is not None:
        said.append(f"{trigger.cooldown_s}s since the last intent")
    if trigger.widen_after is not None:
        said.append(f"more movement after {trigger.widen_after} straight refusals")
    if trigger.min_move_bps is not None and trigger.look_valid_s is not None:
        said.append(f"a last look older than {trigger.look_valid_s}s counts as a first look")
    return ["trigger: asks the roster only on " + "; ".join(said)]
