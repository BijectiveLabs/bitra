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

from trading_research_agents.cli.common import (
    CLEARED,
    REFUSED,
    _data_dir,
    _datasets,
    _desk_ui,
    _environment,
    _watching,
    emit,
)
from trading_research_agents.envelope import load_envelope
from trading_research_agents.execution import FundingSchedule
from trading_research_agents.ledger import Ledger
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
    emit(payload, args.json, lines, lambda: _desk_ui().run_panel(report, str(args.out)))
    return CLEARED if report.stopped_by is None else REFUSED


def _run_watched(args: argparse.Namespace, build: Callable[[Any], TradingConfig]) -> Any:
    """Run a trading config; when a person is watching, a live panel follows every bar."""
    if not _watching(args.json):
        return run_trading(build(None))
    with _desk_ui().RunView(str(args.out)) as view:
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
        lambda: _desk_ui().ladder_table(f"{ladder.tier.value} {ladder.tier.name}", ladder.history),
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
        lambda: _desk_ui().ledger_table(payload["rows"], payload["counts"]),
    )
    return CLEARED
