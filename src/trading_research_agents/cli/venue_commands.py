"""The venue commands: watching a desk, pausing and killing it, the mandate and its signature, keys
and models.
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from time import sleep
from typing import Any

from trading_research_agents.cli.common import (
    CLEARED,
    PLACEHOLDER_PROVIDER,
    REFUSED,
    _declared_environment,
    _desk_ui,
    _environment,
    _has_live_plugin,
    _load_env,
    _watching,
    emit,
)
from trading_research_agents.cli.fees import _fill_account_fees
from trading_research_agents.cli.venue_access import _venue_hand
from trading_research_agents.execution import FundingSchedule
from trading_research_agents.extras import install_hint
from trading_research_agents.llm.completer import redact
from trading_research_agents.llm.judge import JudgeError
from trading_research_agents.llm.providers import (
    UnknownProviderError,
    provider,
    providers,
    typesafe,
)
from trading_research_agents.trading import (
    ConfigError,
    load_strategy_config,
)
from trading_research_agents.trading.venues import (
    LiveVenueSettings,
    live_venue,
)


def pause_command(args: argparse.Namespace) -> int:
    """Hold every new entry, or lift the hold. Open positions keep their exits either way."""
    cfg = load_strategy_config(args.config)
    switch = cfg.pause_switch
    if switch is None:
        raise ConfigError("[controls].pause_switch names no file")
    switch = switch if switch.is_absolute() else args.config.resolve().parent / switch
    return _switch(args, switch, "pause", "no new entry is taken; open positions keep their exits")


def kill_command(args: argparse.Namespace) -> int:
    """Arm the kill switch: a running desk flattens and stops at its next pass. `--off` disarms."""
    cfg = load_strategy_config(args.config)
    if cfg.kill_switch is None:
        raise ConfigError('declare [controls].kill_switch = "KILL" in strategy.toml first')
    switch = cfg.kill_switch
    switch = switch if switch.is_absolute() else args.config.resolve().parent / switch
    return _switch(
        args,
        switch,
        "kill switch",
        "a running desk cancels its resting exits, closes what is held and stops at its next pass; "
        "a desk started while it is armed does the same at once",
    )


def _switch(args: argparse.Namespace, switch: Path, name: str, effect: str) -> int:
    if args.off:
        if switch.exists():
            switch.unlink()
            said = f"{name}: lifted ({switch} removed)"
        else:
            said = f"{name}: was not set ({switch} does not exist)"
        emit({"switch": str(switch), "set": False}, args.json, [said])
        return CLEARED
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    switch.write_text(f"{args.reason or 'set by hand'} at {stamp}\n", encoding="utf-8")
    emit(
        {"switch": str(switch), "set": True, "reason": args.reason},
        args.json,
        [f"{name}: set ({switch}); {effect}", f"lift it with `tra {args.command} --off`"],
    )
    return CLEARED


def watch_desk(args: argparse.Namespace) -> int:
    """What a desk is doing, read from its own records. Nothing here can reach it.

    This is the answer to "can I watch a running desk without disturbing it": the command opens no
    socket, builds no client, holds no key and takes no lock. Closing it is closing a reader.
    """
    from trading_research_agents.cli.watch import look, short_time

    out = args.out or Path("runs") / "cycles"
    if not out.is_dir():
        raise ConfigError(f"{out}: no cycles have been written here yet; run `tra run` first")

    def desk() -> dict[str, Any]:
        return look(out, limit=max(1, args.limit)).record()

    if args.json or args.every is None or not _watching(args.json):
        payload = desk()
        lines = [
            f"{short_time(row['at'])}  {row['instrument']:<9} {row['outcome']:<11} {row['detail']}"[
                :120
            ]
            for row in payload["decisions"]
        ]
        lines.append(f"reading {out}; this view only reads, and changes nothing")
        emit(payload, args.json, lines, lambda: _desk_ui().watch_view(payload))
        return CLEARED

    with _desk_ui().WatchView(desk, every_s=args.every) as view:
        try:
            while True:
                sleep(args.every)
                view.draw()
        except KeyboardInterrupt:
            pass
    print(f"stopped watching {out}; nothing about the desk changed")
    return CLEARED


def show_keys(args: argparse.Namespace) -> int:
    """Every credential this configuration would reach, and whether it is in a position to work."""
    from trading_research_agents.cli.credentials import (
        Readiness,
        endpoints_in_play,
        in_play,
        provider_row,
        venue_row,
    )

    _load_env(args)
    beside = args.config.resolve().parent
    env = _environment(args.env_file, beside=beside)
    out = Readiness()
    cfg = None
    if args.config.is_file():
        try:
            cfg = load_strategy_config(args.config)
        except (ConfigError, OSError, ValueError) as exc:
            print(f"tra: {args.config}: {exc}", file=sys.stderr)
    wanted: list[tuple[str, str, str | None]] = []
    if cfg is not None and cfg.agents is not None and not args.every:
        wanted = endpoints_in_play(cfg.agents)
    else:
        # No project, no agents in it, or --all: say what this machine is holding keys for.
        reached = dict(in_play(cfg.agents)) if cfg is not None and cfg.agents is not None else {}
        wanted = [
            (name, reached.get(name, "a key is named for it"), None)
            for name in providers()
            if _named(name, env)
        ]
    for name, why, base_url in wanted:
        try:
            p = provider(name)
        except UnknownProviderError as exc:
            out.rows.append(_unknown_provider(name, why, str(exc)))
            continue
        at = f" at {base_url}" if base_url else ""
        out.rows.append(
            provider_row(
                p,
                env,
                label=f"provider {name}{at} ({why})",
                verify=bool(args.probe),
                base_url=base_url,
            )
        )
    probe_asked = bool(args.probe or getattr(args, "prepare", False))
    judge = _judge_row(beside / "data.toml", env, every=bool(args.every), probe=probe_asked)
    if judge is not None:
        out.rows.append(judge)
    if cfg is not None and cfg.live is not None and _has_live_plugin(cfg.live.venue):
        venue = live_venue(cfg.live.venue)
        out.rows.append(
            venue_row(
                venue,
                env,
                label=f"venue {cfg.live.venue} ({cfg.live.environment}), for `tra live`",
                preflight=_venue_preflight(cfg, env) if args.probe else None,
            )
        )
    prepared: list[str] = []
    probe = bool(args.probe or getattr(args, "prepare", False))
    if probe and cfg is not None and _venue_to_read(cfg, env):
        # A desk fresh from `tra init` has blank fee lines until they are read, and the venue cannot
        # be probed through a channel with no fee; the fill `tra check` does comes first.
        filled, _ = _fill_account_fees(args.envelope, cfg, env, datetime.now(UTC))
        prepared.extend(f"      {line}" for line in filled)
    if cfg is not None and _venue_to_read(cfg, env):
        # The cycle sends through a different client than a live session does, so a probe that only
        # checked the session's client would be checking a path no order takes.
        out.rows.append(
            venue_row(
                _DispatchKeys(cfg.execution.venue),
                env,
                label=f"venue {cfg.execution.venue} ({_declared_environment(cfg)}), for `tra run`"
                + ("" if cfg.execution.places_orders else ", read only while in sandbox"),
                preflight=_dispatch_ready(args, cfg, prepared) if probe else None,
            )
        )
    lines = []
    for row in out.rows:
        state = "ok" if row.ready else "todo"
        held = ", ".join(
            f"{name} {fp}" for name, fp in zip(row.present, row.fingerprints, strict=False)
        )
        lines.append(f"{state:<5} {row.what}: {row.verdict}" + (f" [{held}]" if held else ""))
    lines.extend(prepared)
    lines.append(
        "every key above is shown as a fingerprint of itself, never as its value"
        if out.rows
        else "no credential is declared or set for this project"
    )
    if not probe and out.rows:
        lines.append("`tra keys --probe` proves each one works: no prompt is sent, no order placed")
    emit(out.record(), args.json, lines, lambda: _desk_ui().keys_table(out.record()))
    return CLEARED if out.ready else REFUSED


def _judge_row(data_config: Path, env: Mapping[str, str], *, every: bool, probe: bool) -> Any:
    """The typed judge's key: when data.toml declares one, or with --all when the key is set.

    The probe is the vendor's model list, which sends no question and spends no token.
    """
    from trading_research_agents.cli.credentials import Credential, fingerprint, scrub
    from trading_research_agents.perception.config import JudgeConfigError, load_judge_config

    try:
        cfg = load_judge_config(data_config) if data_config.is_file() else None
    except JudgeConfigError:
        cfg = None
    if cfg is None and not (every and env.get(typesafe.KEY_ENV)):
        return None
    key_env = cfg.key_env if cfg is not None else typesafe.KEY_ENV
    why = f"[judge] in {data_config.name}, {cfg.model}" if cfg is not None else "a key is set"
    held = bool(env.get(key_env))
    row = Credential(
        what=f"judge {typesafe.NAME} ({why})",
        env=(key_env,),
        present=(key_env,) if held else (),
        missing=() if held else (key_env,),
        fingerprints=(fingerprint(env[key_env]),) if held else (),
    )
    if not probe or not held:
        return row
    try:
        typesafe.model_names(env[key_env])
    except JudgeError as exc:
        return Credential(**{**vars(row), "verified": scrub(str(exc), [env[key_env]])[:240]})
    return Credential(**{**vars(row), "verified": ""})


def _named(name: str, env: Mapping[str, str]) -> bool:
    """Whether this machine has a variable for this provider at all, filled in or not.

    An empty one is worth showing rather than hiding: somebody meant to use that provider and has
    not finished, and a list that quietly leaves it out is how they find out mid-cycle.
    """
    try:
        p = provider(name)
    except UnknownProviderError:
        return False
    return bool(p.key_env and p.key_env in env)


def _unknown_provider(name: str, why: str, said: str) -> Any:
    from trading_research_agents.cli.credentials import Credential

    return Credential(what=f"provider {name} ({why})", env=(), missing=(name,), note=said)


@dataclass(frozen=True)
class _DispatchKeys:
    """The key names the cycle's own hand reads, named the way that hand names them."""

    venue: str

    @property
    def env_keys(self) -> tuple[str, ...]:
        from trading_research_agents.trading.hand_state import credentials_for

        return tuple(credentials_for(self.venue).values())


def _venue_to_read(cfg: Any, env: Mapping[str, str]) -> bool:
    """Whether `tra keys` checks the execution venue: in venue mode always, in sandbox once its
    keys are set, so they can be proved before venue mode is turned on."""
    from trading_research_agents.trading.hand_state import credentials_for

    if cfg.execution.places_orders:
        return True
    venue = str(cfg.execution.venue or "").strip()
    if not venue:
        return False
    try:
        wanted = credentials_for(venue).values()
    except Exception:
        return False
    return bool(wanted) and all(env.get(name) for name in wanted)


def _dispatch_ready(
    args: argparse.Namespace, cfg: Any, prepared: list[str] | None = None
) -> Callable[[], Any]:
    """Read the account through the very client an order would go through. Nothing is sent.

    With --prepare, on a demo or testnet API, an account in long/short mode with nothing open on
    the instrument is switched to one-way first, the one setting every desk needs and every
    venue's own page hides somewhere different. On mainnet it is never changed for you.
    """

    def run() -> Any:
        # Reading the account sends nothing, so it does not wait for the mandate and the ladder:
        # a key that cannot reach the account is worth knowing before either is signed.
        hand = _venue_hand(args, cfg, args.config.resolve().parent, authorise=False)
        if getattr(args, "prepare", False) and prepared is not None:
            said = hand.make_one_way()
            if said:
                prepared.append(f"      {cfg.execution.venue}: {said}")
        said = hand.ready()
        if said:
            raise RuntimeError(said)
        return said

    return run


def _venue_preflight(cfg: Any, env: Mapping[str, str]) -> Callable[[], Any]:
    """The venue's own preflight, as the readiness check runs it: no order, no session."""

    def run() -> Any:
        settings = LiveVenueSettings(
            instrument=cfg.run.instrument,
            channel=cfg.run.venue_key.partition("/")[2] or cfg.run.venue_key,
            funding=cfg.funding
            or FundingSchedule(interval_s=cfg.run.settlement_interval_s, rates={}),
            environment=cfg.live.environment,
            run_id="readiness",
            key_ip_unbound_ok=cfg.live.key_ip_unbound_ok,
            adopt_open_position=True,
            poll_interval_ms=cfg.live.poll_interval_ms,
        )
        return live_venue(cfg.live.venue)(settings, env).preflight()

    return run


def show_models(args: argparse.Namespace) -> int:
    """What a provider will serve this key, read from the vendor. No prompt, no spend."""
    from trading_research_agents.llm.providers.registry import model_ids

    _load_env(args)
    name = args.provider
    if not name and args.config.is_file():
        raw = tomllib.loads(args.config.read_text(encoding="utf-8"))
        name = str((raw.get("agents") or {}).get("provider") or "")
    if not name or name == PLACEHOLDER_PROVIDER:
        raise ConfigError(f"name a provider: {', '.join(providers())}")
    if name.strip().lower() == typesafe.NAME:
        return _judge_models(args)
    p = provider(name)
    if p.key_env and not os.environ.get(p.key_env):
        raise ConfigError(f"{p.key_env} is not set; put it in .env beside strategy.toml")
    if not p.installed:
        module, extra = p.needs
        raise ConfigError(f"{name}: the {module} SDK is not installed; {install_hint(extra)}")
    try:
        found = model_ids(name, base_url=args.base_url)
    except UnknownProviderError:
        raise
    except Exception as exc:
        raise ConfigError(
            f"{name} did not list its models: {exc.__class__.__name__}: {exc}"
        ) from exc
    wanted = args.filter.lower()
    shown = [m for m in found if wanted in m.lower()]
    lines = [*shown, f"{len(shown)} of {len(found)} model(s) on {name}; no prompt was sent"]
    lines.append(f'use one as [agents].model, with provider = "{name}"')
    emit({"provider": name, "models": shown}, args.json, lines)
    return CLEARED


def _judge_models(args: argparse.Namespace) -> int:
    """TypeSafe asked for by name: the versions its key may use, and where the judge is declared."""
    key = os.environ.get(typesafe.KEY_ENV, "")
    if not key:
        raise ConfigError(
            f"{typesafe.KEY_ENV} is not set; make a key at {typesafe.KEYS_URL} and put it in .env "
            f"beside strategy.toml. {typesafe.WHERE}"
        )
    try:
        found = typesafe.model_names(key)
    except JudgeError as exc:
        raise ConfigError(f"typesafe did not list its models: {redact(str(exc))}") from exc
    shown = [m for m in found if args.filter.lower() in m.lower()]
    lines = [
        *(
            m if typesafe.pinned(m) else f"{m}  (an alias that moves; the judge refuses it)"
            for m in shown
        ),
        f"{len(shown)} of {len(found)} model(s) on typesafe; no question was sent",
        typesafe.WHERE,
        f'declare it as [judge] provider = "{typesafe.NAME}", model = one pinned version above',
    ]
    emit({"provider": typesafe.NAME, "judge": True, "models": shown}, args.json, lines)
    return CLEARED


def show_mandate(args: argparse.Namespace) -> int:
    """The text venue mode needs, printed so an operator can read it and keep it."""
    from trading_research_agents.execution.mandate import DISCLAIMER, MANDATE_FILE

    if args.sign is not None:
        return sign_mandate(args)
    if args.json:
        emit({"mandate": DISCLAIMER, "file": MANDATE_FILE}, True, [])
        return CLEARED
    sys.stdout.write(DISCLAIMER)
    return CLEARED


def sign_mandate(args: argparse.Namespace) -> int:
    """The mandate printed for reading, then kept in the project and signed in one step."""
    from trading_research_agents.cli.sign import SignError, sign
    from trading_research_agents.execution.mandate import DISCLAIMER

    on = args.on or date.today()
    turn_on = bool(getattr(args, "venue_mode", False))
    try:
        done = sign(args.config, args.sign, on, venue_mode=turn_on)
    except SignError as exc:
        print(f"tra mandate: {exc}", file=sys.stderr)
        return REFUSED
    lines = [] if args.json else [DISCLAIMER.rstrip(), ""]
    lines += [
        f"{done.mandate}: " + ("written" if done.wrote_mandate else "already there, kept"),
        f"{done.config}: [execution] signed by {args.sign.strip()} on {on.isoformat()}",
        'venue mode is on: [execution].mode = "venue"; `tra check` says what else stands in the way'
        if turn_on
        else 'venue mode is still off; [execution].mode = "venue" turns it on, or sign again with '
        "--venue-mode; then `tra check`",
    ]
    emit(
        {
            "mandate": str(done.mandate),
            "wrote_mandate": done.wrote_mandate,
            "signed_by": args.sign.strip(),
            "signed_on": on.isoformat(),
            "venue_mode": turn_on,
        },
        args.json,
        lines,
    )
    return CLEARED
