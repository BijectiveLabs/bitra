"""An idea through the agent: the proposal, the research session, the evaluation, the trace and the
refusals.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from trading_research_agents.agents import Outcome, RunConfig, propose
from trading_research_agents.agents.pipeline import read_refusals
from trading_research_agents.cli.common import (
    CLEARED,
    REFUSED,
    _data_dir,
    _datasets,
    _load_env,
    _ui,
    _watching,
    emit,
)
from trading_research_agents.cli.research_commands import (
    _build_completer,
    _call_options,
    _observation_beside,
    _run_dir,
)
from trading_research_agents.envelope import load_envelope
from trading_research_agents.ledger import Ledger
from trading_research_agents.llm import Completer
from trading_research_agents.llm.providers import (
    provider,
)
from trading_research_agents.trading import (
    ConfigError,
    load_strategy_config,
)


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
