"""The research commands: the falsification chain, ideas through the agent, replay, shadow and
canary, signals, the ladder and the ledger.
"""

from __future__ import annotations

import argparse
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from trading_research_agents.agents import Outcome, RunConfig, propose
from trading_research_agents.agents.pipeline import read_refusals
from trading_research_agents.chain import (
    NullMode,
    check_requirements,
    cost_floor,
    read_trades,
    validate,
)
from trading_research_agents.chain.preregistration import from_record as prereg_from_record
from trading_research_agents.chain.requirements import to_entry as requirements_entry
from trading_research_agents.chain.validation import to_entry as validation_entry
from trading_research_agents.chain.validation import to_record as validation_record
from trading_research_agents.cli.common import (
    CLEARED,
    REFUSED,
    _data_dir,
    _datasets,
    _environment,
    _load_env,
    _ui,
    _watching,
    emit,
)
from trading_research_agents.data import (
    read_manifest,
)
from trading_research_agents.envelope import load_envelope
from trading_research_agents.execution import FundingSchedule
from trading_research_agents.ledger import Ledger, Verdict
from trading_research_agents.ledger.store import to_record as ledger_to_record
from trading_research_agents.llm import Completer
from trading_research_agents.llm.providers import (
    CallOptions,
    provider,
)
from trading_research_agents.trading import (
    ConfigError,
    Controls,
    KillSwitch,
    Ladder,
    LadderError,
    RecordedFeed,
    SimulatedVenue,
    Tier,
    TradingConfig,
    Venue,
    load_strategy_config,
)
from trading_research_agents.trading import run as run_trading
from trading_research_agents.trading.canary import CanaryGuard
from trading_research_agents.trading.config import load_funding
from trading_research_agents.trading.feeds import live_feed
from trading_research_agents.trading.venues import (
    LiveVenueSettings,
    live_venue,
    live_venues,
)


def chain_floor(args: argparse.Namespace) -> int:
    envelope = load_envelope(args.envelope)
    check = cost_floor(envelope, args.magnitude, venue=args.venue)
    c = check.channel
    lines = [
        f"{check.outcome.value.upper()}: {args.magnitude} bps plausible against "
        f"{c.round_trip_bps} bps round trip on {c.key} (margin {check.margin_bps} bps)"
    ]
    if check.measured_margin_bps is not None:
        lines.append(
            f"measured round trip {c.measured_round_trip_bps} bps: "
            f"margin {check.measured_margin_bps} bps"
        )
    recorded = None
    if check.stopped and args.ledger is not None:
        if not args.entry_id:
            raise ValueError("--ledger needs --id for the new row")
        entry = check.to_entry(
            entry_id=args.entry_id,
            subject=args.subject,
            recorded_at=datetime.now(UTC),
            envelope=envelope,
        )
        Ledger(args.ledger).append(entry)
        recorded = entry.entry_id
        lines.append(f"recorded {entry.entry_id} as CLOSED on cost in {args.ledger}")
    payload = {
        "outcome": check.outcome.value,
        "plausible_magnitude_bps": check.plausible_magnitude_bps,
        "channel": c.key,
        "round_trip_bps": c.round_trip_bps,
        "margin_bps": check.margin_bps,
        "measured_round_trip_bps": c.measured_round_trip_bps,
        "measured_margin_bps": check.measured_margin_bps,
        "recorded": recorded,
    }
    emit(payload, args.json, lines)
    return REFUSED if check.stopped else CLEARED


def chain_observe(args: argparse.Namespace) -> int:
    from trading_research_agents.chain.observation import (
        load_observation,
        observe,
        write_report,
    )
    from trading_research_agents.data.parquet import read_trades as read_prints

    observation = load_observation(args.observation)
    dataset = args.dataset
    if dataset is None:
        found = _datasets(args.observation)
        if len(found) != 1:
            where = _data_dir(args.observation)
            raise ValueError(f"{len(found)} instruments in {where}; say which: --dataset PATH")
        (dataset,) = found.values()
    report = observe(observation, read_prints(dataset), dataset=str(dataset))
    r = report.response_bps
    lines = [
        f"{report.recurrence.value.upper()}: {report.events} events over {report.bars} bars "
        f"({report.measured} measured, {report.skipped} skipped), per subperiod "
        f"{list(report.events_per_subperiod)}",
    ]
    if r["n"]:
        lines.append(
            f"response at {observation.horizon_bars} bars: median {r['median']} bps, mean "
            f"{r['mean']} bps, q25 {r['q25']}, q75 {r['q75']}, positive {r['fraction_positive']}, "
            f"median |response| {r['median_abs']} bps"
        )
    for fired in report.fired:
        lines.append(f"  fired {fired}")
    note = report.record()["magnitude"]
    if note:
        lines.append(note)
    recorded = None
    if not report.recurs and args.ledger is not None:
        if not args.entry_id:
            raise ValueError("--ledger needs --id for the new row")
        entry = report.to_entry(
            entry_id=args.entry_id, recorded_at=datetime.now(UTC), evidence=(str(dataset),)
        )
        Ledger(args.ledger).append(entry)
        recorded = entry.entry_id
        lines.append(f"recorded {entry.entry_id} as {entry.verdict.value} in {args.ledger}")
    if args.out is not None:
        write_report(report, args.out)
        lines.append(f"observation.json in {args.out.parent}")
    payload = {**report.record(), "recorded": recorded}
    emit(payload, args.json, lines, lambda: _ui().observe_view(payload))
    return CLEARED if report.recurs else REFUSED


def chain_check(args: argparse.Namespace) -> int:
    record = json.loads(args.file.read_text(encoding="utf-8"))
    prereg = prereg_from_record(record)
    criteria = [
        f"  {k.metric} {k.comparison} {k.threshold} -> {k.verdict.value}"
        for k in prereg.kill_criteria
    ]
    lines = [
        f"{prereg.name}: valid, seal {prereg.seal()}",
        f"horizon {prereg.horizon}, {len(prereg.universe.instruments)} instruments, "
        f"power n={prereg.power.required_sample} for {prereg.power.effect_bps} bps",
        "kill criteria:",
        *criteria,
    ]
    if prereg.amends:
        lines.append(f"amends {prereg.amends}: {prereg.amendment_reason}")
    payload = {"name": prereg.name, "seal": prereg.seal(), "amends": prereg.amends}
    emit(payload, args.json, lines)
    return CLEARED


def chain_requirements(args: argparse.Namespace) -> int:
    registration = prereg_from_record(json.loads(args.registration.read_text(encoding="utf-8")))
    manifest = read_manifest(args.manifest)
    requirements = check_requirements(registration, manifest)
    lines = [
        f"{f.check}: {'ok' if f.ok else 'BLOCKED'} - {f.detail}" for f in requirements.findings
    ]
    recorded = None
    if not requirements.passed and args.ledger is not None:
        if not args.entry_id:
            raise ValueError("--ledger needs --id for the new row")
        entry = requirements_entry(
            requirements, registration, entry_id=args.entry_id, recorded_at=datetime.now(UTC)
        )
        Ledger(args.ledger).append(entry)
        recorded = entry.entry_id
        lines.append(f"recorded {entry.entry_id} as NOT PROVEN in {args.ledger}")
    lines.append("PASSED" if requirements.passed else f"BLOCKED on {requirements.blocking_axis}")
    payload = {
        "passed": requirements.passed,
        "blocking_axis": (
            None if requirements.blocking_axis is None else requirements.blocking_axis.value
        ),
        "findings": [
            {"check": f.check, "ok": f.ok, "detail": f.detail, "axis": f.axis.value}
            for f in requirements.findings
        ],
        "recorded": recorded,
    }
    emit(payload, args.json, lines)
    return CLEARED if requirements.passed else REFUSED


def chain_validate(args: argparse.Namespace) -> int:
    registration = prereg_from_record(json.loads(args.registration.read_text(encoding="utf-8")))
    trades = read_trades(args.trades)
    cost = args.cost_bps
    result = validate(
        registration,
        trades,
        lambda _trade: cost,
        block_length=args.block_length,
        n_resamples=args.n_resamples,
        n_perm=args.n_perm,
        seed=args.seed,
        null_mode=NullMode(args.null),
    )
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(validation_record(result), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    d = result.decision
    m = result.metrics
    lines = [
        f"{registration.name}: {result.n_trades} trades, {result.n_timestamps} timestamps, "
        f"power {'achieved' if result.power_achieved else 'NOT achieved'} "
        f"({result.n_timestamps} of {result.power_required})",
        f"net {m['net_return_bps']} bps, CI95 [{m['net_return_ci95_lower_bps']}, "
        f"{m['net_return_ci95_upper_bps']}], gross {m['gross_return_bps']} bps (never a verdict)",
        f"null ({result.null.mode.value}): mean {m['null_mean_bps']} bps, "
        f"p {m['permutation_p_value']}, {m['sigma_above_null']} sigma above",
        f"subperiods: {', '.join(f'{x:.2f}' for x in result.subperiods)} bps",
    ]
    for f in d.fired:
        lines.append(
            f"  fired {f.criterion.metric} {f.criterion.comparison} {f.criterion.threshold} "
            f"(observed {f.value}) -> {f.effective.value}"
        )
    recorded = None
    if args.ledger is not None:
        if not args.entry_id:
            raise ValueError("--ledger needs --id for the new row")
        entry = validation_entry(
            result, registration, entry_id=args.entry_id, recorded_at=datetime.now(UTC)
        )
        Ledger(args.ledger).append(entry)
        recorded = entry.entry_id
        lines.append(f"recorded {entry.entry_id} in {args.ledger}")
    lines.append(f"{d.verdict.value} ({d.axis.value}): {d.note}")
    payload = {**validation_record(result), "recorded": recorded}
    emit(payload, args.json, lines)
    return CLEARED if d.verdict is Verdict.OPEN else REFUSED


def _channel(args: argparse.Namespace, cfg: Any) -> Any:
    envelope = load_envelope(args.envelope)
    channel = next((c for c in envelope.channels if c.key == cfg.run.venue_key), None)
    if channel is None:
        raise ConfigError(f"venue_key {cfg.run.venue_key!r} is not a channel in {args.envelope}")
    return channel


def _agent_core(cfg: Any, out_dir: Path) -> Any:
    """The technical analyst as the core: a metered completer over the declared provider."""
    from trading_research_agents.agents.strategy import AgentStrategy
    from trading_research_agents.llm.usage import (
        BudgetLimits,
        MeteredCompleter,
        Pricing,
        UsageLedger,
    )

    a = cfg.agents
    completer = _build_completer(a)
    metered = MeteredCompleter(
        completer,
        role=a.role,
        provider=a.provider,
        limits=BudgetLimits(
            token_budget=a.token_budget,
            max_calls_per_hour=a.max_calls_per_hour,
            min_interval_s=a.min_interval_s,
            call_timeout_s=a.call_timeout_s,
        ),
        ledger=UsageLedger(out_dir / "usage.jsonl"),
        pricing={m: Pricing(i, o) for m, (i, o) in a.pricing.items()},
    )
    return AgentStrategy(
        metered,
        mechanism=a.mechanism,
        lookback_bars=a.lookback_bars,
        min_confidence=a.min_confidence,
        decide_every_bars=a.decide_every_bars,
    )


def _build_completer(a: Any) -> Completer:
    p = provider(a.provider)
    if p.build_with is not None:
        return p.build_with(a.model, a.base_url, _call_options(a))
    return p.build(a.model, a.base_url)


def _call_options(a: Any) -> CallOptions:
    return CallOptions(
        max_output_tokens=a.max_output_tokens,
        timeout_s=float(a.call_timeout_s),
        max_retries=a.max_retries,
        effort=a.effort,
    )


def _trading_config(
    args: argparse.Namespace,
    feed: Any,
    label: str,
    *,
    venue: Venue | None = None,
    canary: CanaryGuard | None = None,
    observer: Any = None,
) -> TradingConfig:
    cfg = load_strategy_config(args.config)
    strategy = cfg.strategy if cfg.agents is None else _agent_core(cfg, args.out)
    funding = cfg.funding
    if funding is None:
        beside = _data_dir(args.config) / f"{cfg.run.instrument}-funding.json"
        if beside.is_file():
            funding = load_funding(beside, cfg.run.settlement_interval_s)
    if venue is None:
        venue = SimulatedVenue(
            channel=_channel(args, cfg),
            funding=funding or FundingSchedule(interval_s=cfg.run.settlement_interval_s, rates={}),
            latency_ns=int(cfg.run.latency_ms * 1_000_000),
            slippage_bps=cfg.run.slippage_bps,
        )
    controls = Controls(cfg.limits, KillSwitch(cfg.kill_switch))
    return TradingConfig(
        feed=feed,
        venue=venue,
        strategy=strategy,
        exit_rule=cfg.exit_rule,
        controls=controls,
        out_dir=args.out,
        bar_interval_ns=cfg.run.bar_interval_s * 1_000_000_000,
        size=cfg.run.fixed_size(),
        spread_cost_bps=cfg.run.spread_cost_bps,
        equity=cfg.run.equity,
        label=label,
        canary=canary,
        observer=observer,
    )


def _report(args: argparse.Namespace, report: Any) -> int:
    lines = [
        f"{report.label} {report.instrument}: {report.prints} prints, {report.bars} bars, "
        f"{report.intents} intents ({report.refused} refused), {report.fills} fills, "
        f"{report.round_trips} round trips, net {report.net_bps_total} bps",
    ]
    for refusal in report.refusals[:5]:
        lines.append(f"  refused {refusal}")
    if report.open_position:
        lines.append(f"open position at end: {report.open_position}")
    if report.stopped_by:
        lines.append(f"stopped by: {report.stopped_by}")
    lines.append(f"trades.jsonl, intents.jsonl, fills.jsonl, run.json in {args.out}")
    payload = {
        "label": report.label,
        "prints": report.prints,
        "bars": report.bars,
        "intents": report.intents,
        "refused": report.refused,
        "fills": report.fills,
        "round_trips": report.round_trips,
        "net_bps_total": str(report.net_bps_total),
        "stopped_by": report.stopped_by,
        "open_position": report.open_position,
        "out": str(args.out),
    }
    emit(payload, args.json, lines, lambda: _ui().run_panel(report, str(args.out)))
    return CLEARED if report.stopped_by is None else REFUSED


def _run_watched(args: argparse.Namespace, build: Callable[[Any], TradingConfig]) -> Any:
    """Run a trading config; when a person is watching, a live panel follows every bar."""
    if not _watching(args.json):
        return run_trading(build(None))
    with _ui().RunView(str(args.out)) as view:
        return run_trading(build(view.observe))


def _observation_beside(config: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    beside = config.resolve().parent / "observation.toml"
    return beside if beside.is_file() else None


def _run_dir(args: argparse.Namespace, label: str) -> Path:
    if args.out is None:
        args.out = Path("runs") / f"{label}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    out: Path = args.out
    return out


def trade_replay(args: argparse.Namespace) -> int:
    cfg = load_strategy_config(args.config)
    _run_dir(args, "backtest")
    parquet = args.parquet
    if parquet is None:
        parquet = _datasets(args.config).get(cfg.run.instrument)
        if parquet is None:
            raise ValueError(
                f"no dataset for {cfg.run.instrument} in {_data_dir(args.config)}; "
                f"run `tra data get {cfg.run.instrument} YYYY-MM-DD` or pass --parquet"
            )
    feed = RecordedFeed.from_parquet(parquet, instrument=cfg.run.instrument)
    return _report(
        args, _run_watched(args, lambda o: _trading_config(args, feed, "replay", observer=o))
    )


def trade_shadow(args: argparse.Namespace) -> int:
    cfg = load_strategy_config(args.config)
    _run_dir(args, "paper")
    # The channel key is venue/channel; the venue half names the feed unless told otherwise.
    feed = _live_feed(args, cfg, args.feed or cfg.run.venue_key.partition("/")[0])
    return _report(
        args, _run_watched(args, lambda o: _trading_config(args, feed, "shadow", observer=o))
    )


def _live_feed(args: argparse.Namespace, cfg: Any, name: str) -> Any:
    """The public feed, and the clock that ends the session when the operator named one.

    A shadow session runs until it is stopped, which is right for a desk and wrong for the first
    one anybody runs: `--minutes` gives it an end without anybody having to reach for Ctrl-C.
    """
    stop = threading.Event()
    minutes = getattr(args, "minutes", None)
    if minutes:
        ending = threading.Timer(float(minutes) * 60.0, stop.set)
        ending.daemon = True
        ending.start()
    return live_feed(name)(cfg.run.instrument, stop=stop, max_reconnects=args.max_reconnects)


def trade_live(args: argparse.Namespace) -> int:
    cfg = load_strategy_config(args.config)
    _run_dir(args, "live")
    if cfg.live is None or cfg.canary is None:
        raise ConfigError("a live session needs both [live] and [canary] in strategy.toml")
    ladder = Ladder(args.ladder, live_available=bool(live_venues()))
    tier = ladder.tier
    if tier is not Tier.CANARY:
        raise LadderError(
            f"live sessions run at {Tier.CANARY.name}; {args.ladder} is at {tier.name}. "
            "Promote with evidence and a signature first."
        )
    run_id = uuid4().hex
    settings = LiveVenueSettings(
        instrument=cfg.run.instrument,
        channel=_channel(args, cfg),
        funding=cfg.funding or FundingSchedule(interval_s=cfg.run.settlement_interval_s, rates={}),
        environment=cfg.live.environment,
        run_id=run_id,
        key_ip_unbound_ok=cfg.live.key_ip_unbound_ok,
        adopt_open_position=cfg.live.adopt_open_position,
        poll_interval_ms=cfg.live.poll_interval_ms,
    )
    venue = live_venue(cfg.live.venue)(
        settings, _environment(args.env_file, beside=args.config.resolve().parent)
    )
    checks = venue.preflight()
    feed = _live_feed(args, cfg, args.feed or cfg.live.venue)
    guard = CanaryGuard(cfg.canary)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "preflight.json").write_text(
        json.dumps({"run_id": run_id, "venue": venue.label, **checks}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    report = _run_watched(
        args,
        lambda o: _trading_config(args, feed, "live-canary", venue=venue, canary=guard, observer=o),
    )
    events = getattr(venue, "events", [])
    if events:
        (args.out / "venue_events.txt").write_text("\n".join(events) + "\n", encoding="utf-8")
    return _report(args, report)


def agent_session(args: argparse.Namespace) -> int:
    from trading_research_agents.agents.session import ScreenSettings, SessionConfig, run_session
    from trading_research_agents.llm.usage import (
        BudgetLimits,
        MeteredCompleter,
        Pricing,
        UsageLedger,
    )

    _load_env(args)
    cfg = load_strategy_config(args.config)
    if cfg.agents is None:
        raise ConfigError("a session needs [strategy].agent and [agents] in strategy.toml")
    datasets: dict[str, Path] = {}
    for item in args.dataset or ():
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            raise ValueError(f"--dataset takes INSTRUMENT=PATH, got {item!r}")
        datasets[name] = Path(path)
    if not datasets:
        datasets = _datasets(args.config)
    if not datasets:
        raise ValueError(f"no datasets in {_data_dir(args.config)}; run `tra data get` first")
    a = cfg.agents
    magnitude = args.magnitude if args.magnitude is not None else a.magnitude_bps
    if magnitude is None:
        raise ConfigError("the claimed edge: --magnitude, or [agents].magnitude_bps")
    _run_dir(args, "session")
    args.out.mkdir(parents=True, exist_ok=True)
    usage = UsageLedger(args.out / "usage.jsonl")
    limits = BudgetLimits(
        token_budget=a.token_budget,
        max_calls_per_hour=a.max_calls_per_hour,
        min_interval_s=a.min_interval_s,
        call_timeout_s=a.call_timeout_s,
    )
    pricing = {m: Pricing(i, o) for m, (i, o) in a.pricing.items()}
    inner = _build_completer(a)
    metered: dict[str, MeteredCompleter] = {}

    def completer_for(role: str) -> MeteredCompleter:
        # One budget for the whole session; the ledger is shared, the cadence is per role.
        if role not in metered:
            metered[role] = MeteredCompleter(
                inner, role=role, provider=a.provider, limits=limits, ledger=usage, pricing=pricing
            )
        return metered[role]

    def session(on_step: Any) -> Any:
        return run_session(
            SessionConfig(
                strategy=cfg,
                envelope=load_envelope(args.envelope),
                datasets=datasets,
                out_dir=args.out,
                completer_for=completer_for,
                usage=usage,
                magnitude_bps=magnitude,
                ladder_path=args.ladder,
                kill_file=cfg.kill_switch,
                macro_series=args.macro,
                onchain_series=args.onchain,
                events=args.events,
                screen=ScreenSettings(**a.screen) if a.screen else ScreenSettings(),
                observation=_observation_beside(args.config, args.observation),
                resume=bool(args.resume),
                on_step=on_step,
            )
        )

    if _watching(args.json):
        with _ui().SessionView(usage) as view:
            result = session(view.on_step)
    else:
        result = session(None)
    lines = [
        f"session {result.session_id}: {len(result.kept)} kept, {len(result.excluded)} excluded, "
        f"{sum(r.round_trips for r in result.reports.values())} round trips, "
        f"{usage.tokens} tokens, cost {usage.cost}",
    ]
    for name, r in result.reports.items():
        lines.append(f"  {name}: {r.round_trips} round trips, net {r.net_bps_total} bps")
    if result.risk is not None:
        lines.append(f"risk manager: {result.risk['decision']}: {result.risk['reason']}")
    if result.audit is not None:
        lines.append(f"auditor: {result.audit['verdict']}: {result.audit['summary']}")
    for refusal in result.refusals[:5]:
        lines.append(f"  refused {refusal}")
    if result.stopped_by:
        lines.append(f"stopped by: {result.stopped_by}")
    lines.append(f"session.json, audit.json, usage.jsonl in {args.out}")
    payload = {
        "session_id": result.session_id,
        "kept": result.kept,
        "excluded": result.excluded,
        "runs": {n: r.round_trips for n, r in result.reports.items()},
        "risk": result.risk,
        "allocation": result.allocation,
        "audit": result.audit,
        "tokens": usage.tokens,
        "cost": str(usage.cost),
        "refusals": result.refusals,
        "stopped_by": result.stopped_by,
        "out": str(args.out),
    }
    emit(payload, args.json, lines, lambda: _ui().session_report(result, usage, str(args.out)))
    # A session that never reached its audit verdict (a role refused, a vendor limit) is not
    # cleared, whatever the runs earned.
    return CLEARED if result.stopped_by is None and result.audit is not None else REFUSED


def agent_eval(args: argparse.Namespace) -> int:
    from trading_research_agents.agents.evals import EvalConfig, ModelSpec, evaluate, table

    _load_env(args)
    cfg = load_strategy_config(args.config)
    if cfg.agents is None:
        raise ConfigError("an eval needs [strategy].agent and [agents] in strategy.toml")
    models = tuple(ModelSpec.parse(text) for text in args.model)
    if args.dataset:
        name, sep, path = args.dataset.partition("=")
        if not sep or not name or not path:
            raise ValueError(f"--dataset takes INSTRUMENT=PATH, got {args.dataset!r}")
        instrument, dataset = name, Path(path)
    else:
        found = _datasets(args.config)
        wanted = cfg.run.instrument
        if wanted in found:
            instrument, dataset = wanted, found[wanted]
        elif len(found) == 1:
            ((instrument, dataset),) = found.items()
        else:
            raise ValueError(
                f"no dataset for {wanted!r} in {_data_dir(args.config)}; --dataset INSTRUMENT=PATH"
            )
    _run_dir(args, "eval")
    a = cfg.agents

    def completer_for(spec: ModelSpec) -> Completer:
        p = provider(spec.provider)
        base_url = args.base_url or a.base_url
        if p.build_with is not None:
            return p.build_with(spec.model, base_url, _call_options(a))
        return p.build(spec.model, base_url)

    rows = evaluate(
        EvalConfig(
            strategy=cfg,
            envelope=load_envelope(args.envelope),
            instrument=instrument,
            dataset=dataset,
            models=models,
            completer_for=completer_for,
            out_dir=args.out,
            kill_file=cfg.kill_switch,
        )
    )
    lines = [f"eval {instrument} over {dataset}: {len(rows)} models, same bars, same controls"]
    lines.extend(table(rows))
    for r in rows:
        if r.stopped_by:
            lines.append(f"  {r.provider}:{r.model} stopped by: {r.stopped_by}")
    lines.append(f"eval.json and one run per model in {args.out}")
    payload = {
        "instrument": instrument,
        "dataset": str(dataset),
        "rows": [r.record() for r in rows],
        "out": str(args.out),
    }
    emit(payload, args.json, lines, lambda: _ui().eval_table(rows, instrument, str(dataset)))
    return CLEARED


def agent_trace(args: argparse.Namespace) -> int:
    from trading_research_agents.agents.trace import lines as trace_lines
    from trading_research_agents.agents.trace import trace

    t = trace(args.path)
    emit(t.record(), args.json, trace_lines(t), lambda: _ui().trace_view(t))
    return CLEARED


def signals_export(args: argparse.Namespace) -> int:
    from trading_research_agents.agents.executor.tools import export_signals

    summary = args.run / "run.json"
    if not summary.is_file():
        raise ValueError(f"{args.run} is not a run directory (no run.json)")
    instrument = str(json.loads(summary.read_text("utf-8")).get("instrument", ""))
    out = args.out or (args.run / f"signals.{args.format}")
    n = export_signals(args.run, instrument=instrument, out=out, fmt=args.format)
    emit({"signals": n, "out": str(out)}, args.json, [f"{n} signals -> {out}"])
    return CLEARED


def mcp_serve(args: argparse.Namespace) -> int:
    from trading_research_agents.mcp import serve

    serve(getattr(args, "root", None))
    return CLEARED


def ladder_show(args: argparse.Namespace) -> int:
    ladder = Ladder(args.path)
    lines = [f"tier {ladder.tier.value} {ladder.tier.name}"]
    for h in ladder.history:
        detail = h.get("evidence") or h.get("reason")
        when = str(h["at"])[:19].replace("T", " ")
        lines.append(f"  {when} UTC  {h['kind']} {h['from']} -> {h['to']}: {detail}")
    emit(
        {"tier": ladder.tier.name, "history": list(ladder.history)},
        args.json,
        lines,
        lambda: _ui().ladder_table(f"{ladder.tier.value} {ladder.tier.name}", ladder.history),
    )
    return CLEARED


def ladder_promote(args: argparse.Namespace) -> int:
    ladder = Ladder(args.path, live_available=bool(live_venues()))
    receipt = ladder.promote(evidence=args.evidence, signed_by=args.signed_by)
    payload = {"from": receipt.from_tier.name, "to": receipt.to_tier.name, "at": receipt.at}
    emit(payload, args.json, [f"{receipt.from_tier.name} -> {receipt.to_tier.name}"])
    return CLEARED


def ladder_demote(args: argparse.Namespace) -> int:
    to = None if args.to is None else Tier[args.to.upper()]
    demotion = Ladder(args.path).demote(reason=args.reason, to=to)
    payload = {"from": demotion.from_tier.name, "to": demotion.to_tier.name, "at": demotion.at}
    emit(payload, args.json, [f"{demotion.from_tier.name} -> {demotion.to_tier.name}"])
    return CLEARED


def ledger_show(args: argparse.Namespace) -> int:
    ledger = Ledger(args.path)
    rows = ledger.effective()
    counts = ledger.counts()
    lines = ["  ".join(f"{v.value} {n}" for v, n in counts.items() if n)] if rows else ["empty"]
    for e in rows:
        axis = ", ".join(a.value for a in e.axis_of_death) or (
            e.blocking_axis.value if e.blocking_axis else "-"
        )
        lines.append(
            f"{e.entry_id:12} {e.verdict.value:10} layer {e.layer!s:4} {axis:20} {e.subject}"
        )
    payload = {
        "counts": {v.value: n for v, n in counts.items()},
        "rows": [ledger_to_record(e) for e in rows],
    }
    emit(
        payload,
        args.json,
        lines,
        lambda: _ui().ledger_table(payload["rows"], payload["counts"]),
    )
    return CLEARED


def _completer(provider_name: str, model: str | None, base_url: str | None = None) -> Completer:
    return provider(provider_name).build(model, base_url)


def agent_propose(args: argparse.Namespace) -> int:
    config = RunConfig(
        envelope=load_envelope(args.envelope),
        completer=_completer(args.provider, args.model, args.base_url),
        venue=args.venue,
        magnitude_bps=args.magnitude,
        max_revisions=args.max_revisions,
        ledger=Ledger(args.ledger),
        refusals=args.refusals,
        runs_dir=args.runs,
    )
    result = propose(args.idea, config)
    lines = [f"{result.run_id}: {result.outcome.value}"]
    if result.floor is not None:
        c = result.floor.channel
        lines.append(
            f"layer 0: {result.floor.plausible_magnitude_bps} bps against "
            f"{c.round_trip_bps} bps on {c.key} ({result.floor.outcome.value})"
        )
    if result.refusal is not None:
        lines.append(
            f"refused at layer {result.refusal.layer} after {result.refusal.revisions} "
            f"revisions: {result.refusal.reason}"
        )
    if result.preregistration is not None:
        lines.append(
            f"registered: {result.preregistration.name}, seal {result.preregistration.seal()}"
        )
    if result.ledger_entry_id is not None:
        lines.append(f"ledger: {result.ledger_entry_id} in {args.ledger}")
    lines.append(
        f"{len(result.exchanges)} model calls, {result.input_tokens} in / "
        f"{result.output_tokens} out tokens; run written to {args.runs / result.run_id}"
    )
    payload = {
        "run_id": result.run_id,
        "outcome": result.outcome.value,
        "ledger_entry_id": result.ledger_entry_id,
        "seal": None if result.preregistration is None else result.preregistration.seal(),
        "refusal": None if result.refusal is None else result.refusal.reason,
        "exchanges": len(result.exchanges),
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "run_dir": str(args.runs / result.run_id),
    }
    emit(payload, args.json, lines)
    return CLEARED if result.outcome is Outcome.REGISTERED else REFUSED


def agent_refusals(args: argparse.Namespace) -> int:
    refusals = read_refusals(args.path)
    lines = [f"{len(refusals)} refused"] if refusals else ["none"]
    for r in refusals:
        lines.append(
            f"{r.run_id:22} layer {r.layer} after {r.revisions}: {r.reason[:60]}  | {r.idea[:50]}"
        )
    payload = {
        "count": len(refusals),
        "refusals": [
            {
                "run_id": r.run_id,
                "at": r.at,
                "layer": r.layer,
                "revisions": r.revisions,
                "reason": r.reason,
                "idea": r.idea,
            }
            for r in refusals
        ],
    }
    emit(payload, args.json, lines)
    return CLEARED
