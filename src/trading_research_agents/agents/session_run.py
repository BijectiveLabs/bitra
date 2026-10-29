"""A research session from start to end: the screen, the context, a replay per instrument, risk,
allocation and audit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trading_research_agents.agents.session import (
    SessionConfig,
    SessionResult,
    _Session,
    figures_for,
)
from trading_research_agents.readings import MACRO, ONCHAIN
from trading_research_agents.trading import (
    Ladder,
    RunReport,
)


def run_session(cfg: SessionConfig) -> SessionResult:
    s = _Session(cfg)
    venue = cfg.strategy.run.venue_key.partition("/")[0]

    def tell(step: str, status: str, summary: str = "") -> None:
        if cfg.on_step is not None:
            cfg.on_step(step, status, summary)

    tell("figures", "running", f"{len(cfg.datasets)} datasets")
    figures = [
        figures_for(
            name,
            path,
            cfg.envelope,
            venue,
            cfg.magnitude_bps,
            screen=cfg.screen,
            observation=cfg.observation,
        )
        for name, path in cfg.datasets.items()
    ]
    (s.out / "figures.json").write_text(
        json.dumps([f.record() for f in figures], indent=2, sort_keys=True) + "\n", "utf-8"
    )
    tell("figures", "done", f"{len(figures)} measured")
    kept, excluded = s.screen(figures)
    tell("screen", "done", f"{len(kept)} kept, {len(excluded)} excluded, by rule")
    reference = next(iter(cfg.datasets.values()))
    macro = s.context(MACRO, cfg.macro_series, reference)
    tell(MACRO, *_read_by_code(macro))
    onchain = s.context(ONCHAIN, cfg.onchain_series, reference)
    tell(ONCHAIN, *_read_by_code(onchain))
    limits = cfg.strategy.limits
    ladder = Ladder(cfg.ladder_path)
    reports: dict[str, RunReport] = {}
    for k in kept:
        if s.stopped_by is not None:
            break
        name = str(k["instrument"])
        tell(f"run:{name}", "running")
        reports[name] = s.analyse(name, Path(cfg.datasets[name]), limits)
        r = reports[name]
        tell(f"run:{name}", "done", f"{r.round_trips} round trips, net {r.net_bps_total} bps")
    risk, limits_after = s.risk(reports, limits, ladder) if reports else (None, limits)
    tell("risk", "done" if reports else "skipped", "" if risk is None else risk.decision)
    allocation = s.allocate(kept, limits_after)
    tell("allocation", "done", f"{len(allocation)} equal shares")
    tell("auditor", "running")
    audit = s.audit(reports, risk, allocation, ladder)
    tell("auditor", "done", "refused" if audit is None else str(audit.get("verdict")))
    result = SessionResult(
        session_id=s.session_id,
        figures=figures,
        kept=kept,
        excluded=excluded,
        macro=macro,
        onchain=onchain,
        reports=reports,
        risk=None if risk is None else risk.record(),
        limits_after=limits_after,
        allocation=allocation,
        audit=audit,
        refusals=s.refusals,
        stopped_by=s.stopped_by,
    )
    summary = {
        "session_id": result.session_id,
        "started_at": s.started.isoformat(),
        "finished_at": cfg.clock().isoformat(),
        "kept": kept,
        "excluded": excluded,
        "macro": result.macro,
        "onchain": result.onchain,
        "runs": {
            name: {"round_trips": r.round_trips, "net_bps_total": str(r.net_bps_total)}
            for name, r in reports.items()
        },
        "risk": result.risk,
        "limits_after": {
            "max_risk_per_trade_bps": str(limits_after.max_risk_per_trade_bps),
            "max_position": str(limits_after.max_position),
            "max_orders_per_minute": limits_after.max_orders_per_minute,
            "daily_loss_limit_bps": str(limits_after.daily_loss_limit_bps),
        },
        "allocation": allocation,
        "audit_verdict": None if audit is None else audit["verdict"],
        "tokens_total": cfg.usage.tokens,
        "cost_total": str(cfg.usage.cost),
        "refusals": s.refusals,
        "stopped_by": s.stopped_by,
    }
    (s.out / "session.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return result


def _read_by_code(said: dict[str, Any] | None) -> tuple[str, str]:
    """The progress row of a reading: skipped without a series, else what code declared."""
    if said is None:
        return "skipped", ""
    return "done", f"read by the system: {said['declared_state']['declared']}"
