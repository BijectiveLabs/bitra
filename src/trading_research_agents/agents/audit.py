"""Two auditors, asked in parallel, reporting to the operator and changing nothing.

One reads the machinery: what failed, what was slow, what the thinking cost and what it bought. The
other reads the desk: what it decided, what those decisions cost, and what came of them. Both read
records this project wrote itself, both answer with findings tied to figures in their brief, and
both stop there. A proposal is a sentence and a configuration key; applying it is a person editing
their own file. Nothing here writes a config, promotes a ladder or touches a venue.

The report goes to `audits/<time>/`: the briefs as they were given, the answers as they came back,
what code discarded and why, and a short `proposals.md` a person can read without a JSON viewer.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_research_agents.agents import quant_auditor, tech_auditor
from trading_research_agents.agents.auditor_report import Report, checked
from trading_research_agents.agents.audits import AuditError, Cycles, read_cycles
from trading_research_agents.agents.cited import figures_of
from trading_research_agents.agents.parallel import FanOut, Task, run_tasks
from trading_research_agents.agents.roles import prompt
from trading_research_agents.llm.completer import Completer
from trading_research_agents.llm.usage import UsageLedger
from trading_research_agents.store.snapshot import cycle_id

AUDITORS = {tech_auditor.ROLE: tech_auditor, quant_auditor.ROLE: quant_auditor}


@dataclass(frozen=True)
class AuditConfig:
    """What an audit needs: where the records are, who to ask, and where the report goes."""

    runs: Path
    completer_for: Callable[[str], Completer]
    usage: UsageLedger
    root: Path
    config: Path | None = None
    sessions: Path | None = None
    auditors: tuple[str, ...] = (tech_auditor.ROLE, quant_auditor.ROLE)
    limit: int = 500
    max_workers: int = 2
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        unknown = sorted(set(self.auditors) - set(AUDITORS))
        if unknown:
            raise AuditError(f"{unknown} is not an auditor; have {sorted(AUDITORS)}")
        if not self.auditors:
            raise AuditError("an audit asks at least one auditor")


@dataclass
class AuditResult:
    at: str
    path: Path
    cycles: int
    briefs: dict[str, dict[str, Any]] = field(default_factory=dict)
    reports: dict[str, Report] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    fan: FanOut = field(default_factory=FanOut)

    @property
    def findings(self) -> int:
        return sum(len(r.findings) for r in self.reports.values())

    def record(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "cycles": self.cycles,
            "reports": {role: report.record() for role, report in sorted(self.reports.items())},
            "failed": self.failed,
            "agents": self.fan.record(),
        }


def tables_in(config: Path | None) -> tuple[str, ...]:
    """The top-level tables this project's configuration has, so a proposal can be checked."""
    if config is None or not config.is_file():
        return ()
    try:
        raw = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ()
    return tuple(sorted(str(key) for key in raw))


def run_audit(cfg: AuditConfig) -> AuditResult:
    """Ask the auditors what the record says, check what they answer, and write it down."""
    cycles: Cycles = read_cycles(cfg.runs, limit=cfg.limit)
    at = cycle_id(cfg.clock())
    where = cfg.root / at
    where.mkdir(parents=True, exist_ok=True)
    result = AuditResult(at=at, path=where, cycles=cycles.count)

    briefs = {
        tech_auditor.ROLE: tech_auditor.tech_brief(cycles, usage=cfg.runs / "usage.jsonl"),
        quant_auditor.ROLE: quant_auditor.quant_brief(cycles, runs=cfg.sessions, desk=cfg.runs),
    }
    tasks = []
    for role in cfg.auditors:
        brief = briefs[role]
        result.briefs[role] = brief
        (where / f"brief-{role}.json").write_text(
            json.dumps(brief, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        tasks.append(
            Task(name=role, work=_ask(cfg, role, brief), path=where / f"answer-{role}.json")
        )

    fan = run_tasks(tasks, max_workers=cfg.max_workers)
    result.fan = fan
    result.failed = dict(fan.failed)
    tables = tables_in(cfg.config)
    for role, answer in fan.answers.items():
        try:
            said = AUDITORS[role].parse(str(answer))
        except ValueError as exc:
            result.failed[role] = str(exc)
            continue
        result.reports[role] = checked(said, figures=figures_of(result.briefs[role]), tables=tables)

    payload = result.record()
    payload["tokens"] = cfg.usage.tokens
    (where / "audit.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    (where / "proposals.md").write_text(as_markdown(result), encoding="utf-8")
    return result


def as_markdown(result: AuditResult) -> str:
    """The report a person reads. Proposals only; nothing here has been applied."""
    lines = [
        f"# Audit {result.at}",
        "",
        f"Read {result.cycles} cycle record(s). Nothing in this file has been applied: every",
        "proposal is a change an operator makes, or does not.",
        "",
    ]
    for role, report in sorted(result.reports.items()):
        lines.append(f"## {role.replace('_', ' ')}")
        lines.append("")
        lines.append(report.summary)
        lines.append("")
        if report.nothing_to_report and not report.findings:
            lines.extend(["Nothing to report.", ""])
        for number, finding in enumerate(report.findings, start=1):
            cited = ", ".join(f"`{f}` = {v}" for f, v in finding.cites)
            lines.append(f"{number}. **{finding.what}** ({finding.confidence} confidence)")
            lines.append(f"   - from: {cited}")
            lines.append(f"   - proposal: {finding.proposal}")
            if finding.change is not None:
                change = finding.change
                lines.append(
                    f"   - change: `{change.key}` from `{change.from_value}` to `{change.to_value}`"
                )
            lines.append("")
        if report.discarded:
            lines.append("Discarded by the citation check:")
            lines.extend(f"- {reason}" for reason in report.discarded)
            lines.append("")
    for role, error in sorted(result.failed.items()):
        lines.append(f"## {role.replace('_', ' ')}")
        lines.extend(["", f"Did not report: {error}", ""])
    return "\n".join(lines).rstrip() + "\n"


def _ask(cfg: AuditConfig, role: str, brief: Mapping[str, Any]) -> Callable[[], str]:
    def ask() -> str:
        done = cfg.completer_for(role).complete(
            system=prompt(role),
            user=json.dumps(brief, sort_keys=True, default=str),
            schema=dict(AUDITORS[role].SCHEMA),
        )
        return done.text

    return ask
