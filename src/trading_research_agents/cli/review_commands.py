"""The review commands: the auditors, hindsight, the sizing record, the arena, the recorder and the
judged text.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.cli.arena import arena_report
from trading_research_agents.cli.common import (
    CLEARED,
    REPORT_HEADROOM,
    _audit_lines,
    _desk_data,
    _load_env,
    _metered_roles,
    _ui,
    emit,
)
from trading_research_agents.cli.record import record_report, record_status
from trading_research_agents.cli.text import (
    text_judge,
)
from trading_research_agents.records import read_lines, write_json
from trading_research_agents.trading import (
    ConfigError,
    load_strategy_config,
)


def run_audit_command(args: argparse.Namespace) -> int:
    """Two auditors over the records this project already wrote. Nothing is applied."""
    from trading_research_agents.agents.audit import AuditConfig, run_audit
    from trading_research_agents.llm.usage import UsageLedger

    _load_env(args)
    cfg = load_strategy_config(args.config)
    if cfg.agents is None:
        raise ConfigError("an audit needs [agents] in strategy.toml; it asks a model")
    a = cfg.agents
    out = args.out or Path("audits")
    usage = UsageLedger(out / "usage.jsonl")
    completer_for = _metered_roles(a, usage, floor=REPORT_HEADROOM)

    who = ("tech_auditor", "quant_auditor") if args.who == "both" else (args.who,)
    # Only a declared cap can be too low; an undeclared one is the vendor's own maximum.
    raised = [
        r for r in who if (a.for_role(r).max_output_tokens or REPORT_HEADROOM) < REPORT_HEADROOM
    ]
    result = run_audit(
        AuditConfig(
            runs=args.runs,
            completer_for=completer_for,
            usage=usage,
            root=out,
            config=args.config,
            sessions=args.sessions,
            auditors=who,
        )
    )
    payload = result.record()
    payload["out"] = str(result.path)
    emit(payload, args.json, _audit_lines(result, raised), lambda: _ui().audit_view(payload))
    return CLEARED


FOUR_HOURS_S = 4 * 3600


def record_command(args: argparse.Namespace) -> int:
    """Keep a venue's markets in the archive while they happen."""
    payload, lines, code = record_report(args)
    emit(payload, args.json, lines)
    return code


def record_status_command(args: argparse.Namespace) -> int:
    """What the records say about coverage, gaps and latency over the last day."""
    payload, lines, code = record_status(args)
    emit(payload, args.json, lines)
    return code


def text_judge_command(args: argparse.Namespace) -> int:
    """The archived headlines of a range of days, read by the declared typed judge."""
    payload, lines, code = text_judge(args)
    emit(payload, args.json, lines)
    return code


def arena_command(args: argparse.Namespace) -> int:
    """Every row replayed over the market archive, settled the same way, side by side."""
    payload, lines, code = arena_report(args)
    emit(payload, args.json, lines)
    return code


def hindsight_command(args: argparse.Namespace) -> int:
    """Replay every directional decision on the candles that followed it, grouped by its gate."""
    from trading_research_agents.data.sources import SourceError
    from trading_research_agents.execution.hindsight import outcomes, read_decisions, report

    cfg = load_strategy_config(args.config)
    bar_s = int(args.bar)
    timeout = cfg.exit_rule.timeout_bars
    hold_s = (
        int(args.hold)
        if args.hold
        else (timeout * cfg.run.bar_interval_s if timeout else FOUR_HOURS_S)
    )
    if bar_s <= 0 or bar_s > hold_s:
        raise ConfigError(f"--bar is positive and no longer than the hold ({hold_s}s)")
    data = _desk_data(args.data_config, cfg)
    sources = data.chain("ohlcv").sources
    declared = data.for_kind("ohlcv").options
    params = {"params": declared["params"]} if "params" in declared else {}

    def fetch(instrument: str, since_ms: int, limit: int) -> list[Any]:
        for source in sources:
            try:
                got = source.fetch(
                    "ohlcv", instrument, interval_s=bar_s, limit=limit, since_ms=since_ms, **params
                )
            except SourceError:
                continue
            bars = [c for c in got.value if c.start_ns >= since_ms * 1_000_000]
            if bars:
                return bars
        return []

    since = None if args.days is None else datetime.now(UTC) - timedelta(days=args.days)
    decisions = read_decisions(args.runs, since=since)
    found, skipped = outcomes(
        decisions, fetch, hold_s=hold_s, bar_s=bar_s, cache=args.runs / "hindsight"
    )
    payload = report(found, hold_s=hold_s, bar_s=bar_s, skipped=skipped)
    write_json(args.runs / "hindsight.json", payload)
    emit(payload, args.json, _hindsight_lines(payload))
    return CLEARED


def sizing_command(args: argparse.Namespace) -> int:
    """What the round trips on record support, computed the way a cycle computes it. Read-only."""
    from dataclasses import replace

    from trading_research_agents.execution.excursion import summarise
    from trading_research_agents.execution.kelly import by_record
    from trading_research_agents.execution.reconcile import reconcile

    cfg = load_strategy_config(args.config)
    rule, declared = cfg.sizing, not cfg.sizing.idle
    if not declared:
        rule = replace(rule, kelly_fraction=args.fraction or Decimal("0.25"))
    rows = read_lines(args.runs / "round_trips.jsonl")
    room = cfg.limits.max_drawdown_bps
    sized = by_record(rule, rows, room_bps=room)
    excursions = [r["excursion"] for r in rows if isinstance(r.get("excursion"), dict)]
    payload = {
        "declared": declared,
        "round_trips": sum(1 for r in rows if "net" in r),
        "sizing": sized.record(),
        "room_bps": None if room is None else str(room),
        "declared_risk_bps": (
            None
            if cfg.limits.max_risk_per_trade_bps is None
            else str(cfg.limits.max_risk_per_trade_bps)
        ),
        "excursions": summarise(excursions) if excursions else {"measured": 0},
        "fills": reconcile(rows),
    }
    emit(payload, args.json, _sizing_lines(payload))
    return CLEARED


def _sizing_lines(payload: dict[str, Any]) -> list[str]:
    sizing = payload["sizing"]
    kelly = sizing["kelly"]
    measured = kelly.get("measured")
    lines = [
        f"sizing: {payload['round_trips']} finished round trip(s) on record"
        + (
            ""
            if payload["declared"]
            else f"; [sizing] is not declared, shown at {kelly['fraction']}"
        )
    ]
    if measured:
        lines.append(
            f"  {measured['wins']} won, {measured['losses']} lost of {measured['trades']}: "
            f"win rate {measured['win_rate']} (at least {measured['win_rate_low_95']} at 95%), "
            "average win "
            f"{measured['avg_win_r']}R, average loss {measured['avg_loss_r']}R"
        )
    lines.append(f"  {sizing['reason']}")
    declared, cap = payload["declared_risk_bps"], sizing["cap_bps"]
    if declared is not None and cap is not None:
        lower = Decimal(declared) <= Decimal(cap)
        lines.append(
            f"  a cycle risks at most {declared if lower else cap} bps at the stop: "
            + (
                f"[controls].max_risk_per_trade_bps {declared} is the lower"
                if lower
                else f"the record's {cap} is below the declared {declared}"
            )
        )
    simulation = sizing.get("simulation")
    if simulation:
        reach = simulation.get("reach_share")
        lines.append(
            f"  {simulation['paths']} paths of {simulation['trades']} trades at "
            f"{simulation['risk_bps']} bps, seed {simulation['seed']}: drawdown median "
            f"{simulation['drawdown_median_bps']} bps, 95th {simulation['drawdown_p95_bps']} bps"
            + ("" if reach is None else f"; {reach} of paths reach the drawdown limit")
        )
        if payload["room_bps"] is not None:
            lines.append(
                f"  room is the whole [controls].max_drawdown_bps {payload['room_bps']}; a cycle "
                "takes the drawdown already on the account off it first"
            )
    fills = payload["fills"]
    if fills["entry_slippage"]["measured"]:
        slip = fills["entry_slippage"]
        lines.append(
            f"  fills: entry median {slip['median_bps']} bps against the touch it was priced at, "
            f"worse in {slip['worse_than_priced']} of {slip['measured']}; fees median "
            f"{fills['fees']['median_bps']} bps against {fills['fees']['priced_median_bps']} priced"
        )
    seen = payload["excursions"]
    if seen.get("measured"):
        lines.append(
            f"  excursions over {seen['measured']} trade(s): best median {seen['mfe_median_bps']} "
            f"bps, worst median {seen['mae_median_bps']} bps, worst {seen['mae_worst_bps']} bps"
        )
    return lines


def _hindsight_lines(payload: dict[str, Any]) -> list[str]:
    from trading_research_agents.agents.briefs import interval_name

    def row(name: str, s: dict[str, Any]) -> str:
        if not s["decisions"]:
            return f"  {name:<22} none"
        return (
            f"  {name:<22} {s['decisions']:>4}  mean {s['mean_net_bps']:>7} bps  median "
            f"{s['median_net_bps']:>7}  won {float(s['won']):>4.0%}  sum {s['sum_net_bps']:>8}"
        )

    side = payload["side"]
    held, bar = interval_name(payload["hold_s"]), interval_name(payload["bar_s"])
    lines = [
        f"hindsight: {payload['replayed']} decisions replayed over {payload['days']} day(s), "
        f"{payload['independent']} of them independent, each held {held} on {bar} candles, "
        "net of its round trip",
        row("admitted", payload["admitted"]),
    ]
    for gate in payload["gates"]:
        lines.append(row(f"refused by {gate['gate']}", gate) + f"  {gate['verdict']}")
    refused = payload["all_refused"]
    lines.append(row("all refused", refused) + f"  {refused['verdict']}")
    lines.append(
        f"  side: the model's {side['model_side']['mean_net_bps']} bps against "
        f"{side['opposite_side']['mean_net_bps']} on the reverse; {side['verdict']}"
    )
    levels = payload.get("levels") or {}
    if levels.get("applied", {}).get("decisions"):
        lines.append(
            f"  levels: code's {levels['applied']['mean_net_bps']} bps against the analyst's own "
            f"{levels['analyst_own']['mean_net_bps']}; {levels['verdict']}"
        )
    for name, said in (payload.get("calibration") or {}).items():
        if not said.get("decisions"):
            continue
        measured = (
            f" (rank correlation {said['rank_correlation']} over {said['decisions']} decisions)"
            if "rank_correlation" in said
            else ""
        )
        lines.append(f"  {name}: {said['verdict']}{measured}")
    left = {k: v for k, v in payload["skipped"].items() if v}
    if left:
        lines.append("  not replayed: " + ", ".join(f"{v} {k}" for k, v in left.items()))
    lines.append(f"  written to hindsight.json; {payload['basis']}")
    return lines
