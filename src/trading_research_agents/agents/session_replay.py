"""One instrument's run in a session: the replay with the analyst as its core, and a finished run
read back from disk.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.execution.cost import FundingSchedule
from trading_research_agents.trading import (
    Controls,
    KillSwitch,
    RecordedFeed,
    RunReport,
    SimulatedVenue,
    StrategyConfig,
    TradingConfig,
    run,
)
from trading_research_agents.trading.controls import Limits


def replay_instrument(
    s: StrategyConfig,
    channel: Any,
    *,
    instrument: str,
    path: Path,
    core: Any,
    limits: Limits,
    kill_file: Path | None,
    out: Path,
    label: str,
) -> RunReport:
    """The replay every session and eval runs: recorded prints, simulated venue, one core."""
    config = TradingConfig(
        feed=RecordedFeed.from_parquet(path, instrument=instrument),
        venue=SimulatedVenue(
            channel=channel,
            funding=s.funding or FundingSchedule(interval_s=s.run.settlement_interval_s, rates={}),
            latency_ns=int(s.run.latency_ms * 1_000_000),
            slippage_bps=s.run.slippage_bps,
        ),
        strategy=core,
        exit_rule=s.exit_rule,
        controls=Controls(limits, KillSwitch(kill_file)),
        out_dir=out,
        bar_interval_ns=s.run.bar_interval_s * 1_000_000_000,
        size=s.run.fixed_size(),
        spread_cost_bps=s.run.spread_cost_bps,
        equity=s.run.equity,
        label=label,
    )
    return run(config)


def _report_from_disk(out: Path, instrument: str) -> RunReport:
    summary = json.loads((out / "run.json").read_text("utf-8"))
    report = RunReport(
        label=str(summary.get("label", "session-replay")),
        instrument=instrument,
        started_at=datetime.fromisoformat(str(summary["started_at"])),
    )
    report.finished_at = datetime.fromisoformat(str(summary["finished_at"]))
    for key in ("prints", "bars", "intents", "refused", "fills", "round_trips"):
        setattr(report, key, int(summary.get(key, 0)))
    report.net_bps_total = Decimal(str(summary.get("net_bps_total", "0")))
    report.stopped_by = summary.get("stopped_by")
    report.open_position = summary.get("open_position")
    return report
