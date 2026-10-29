"""The auditors woken between cycles, on their own ledger. They report; code holds the risk."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import time as wall_clock
from typing import Any

from trading_research_agents.cli.common import (
    REPORT_HEADROOM,
    _audit_lines,
    _metered_roles,
    _ui,
    _watching,
    plain_text,
)


@dataclass
class Auditing:
    """Waking the auditors between cycles: on an alarm, on a count, and on a clock.

    Something breaking is an event and belongs to the technical auditor. Whether the desk is
    earning what it costs belongs to the quant auditor, which wakes on a count of cycles, on a
    clock, and when the account says so: the daily loss limit reached, or a run of losing round
    trips. Neither can act on what it finds, and neither may end a run: an auditor that fails is
    a line in the report.
    """

    policy: Any
    settings: Any
    config: Path
    runs: Path
    watch: Any = None
    clock: Callable[[], float] = wall_clock
    limits: Any = None
    kill_switch: Path | None = None

    def __post_init__(self) -> None:
        from trading_research_agents.agents.alarms import Watch

        if self.watch is None:
            self.watch = Watch(cooldown_s=self.policy.alarm_cooldown_s)

    def due(self, record: Mapping[str, Any], ran: int) -> tuple[tuple[str, ...], tuple[Any, ...]]:
        """Which auditors to ask after this cycle, and the alarms that asked for one."""
        from trading_research_agents.agents.alarms import (
            DAILY_KINDS,
            QUANT_KINDS,
            Reported,
            alarms_of,
        )

        now = self.clock()
        reported = Reported(self.runs.parent / "audits" / "reported.json")
        state = reported.read()
        who: list[str] = []
        fired: tuple[Any, ...] = ()
        if self.policy.on_alarm:
            found = [a for a in alarms_of(record) if self._first_time(a, state, now)]
            streak = self._streak(record, state)
            if streak is not None:
                found.append(streak)
            fired = self.watch.due(tuple(found), now)
            if any(a.kind not in QUANT_KINDS for a in fired):
                who.append("tech_auditor")
            if any(a.kind in QUANT_KINDS for a in fired):
                who.append("quant_auditor")
            for alarm in fired:
                if alarm.kind in DAILY_KINDS:
                    reported.write(**{f"{alarm.kind}_day": _utc_day(now)})
                elif alarm.kind == "losing_streak":
                    reported.write(streak_trip=alarm.at)
        every = self.policy.quant_every_cycles
        if every is not None and ran % every == 0 and "quant_auditor" not in who:
            who.append("quant_auditor")
        clock = self.policy.quant_every_s
        if clock is not None:
            last = state.get("quant_at")
            if not isinstance(last, int | float):
                # The first run starts the clock rather than paying for a look at nothing.
                reported.write(quant_at=now)
            elif now - last >= clock and "quant_auditor" not in who:
                who.append("quant_auditor")
        if "quant_auditor" in who and clock is not None:
            # Any look restarts the clock: a review that just happened is the one the clock wanted.
            reported.write(quant_at=now)
        return tuple(who), fired

    @staticmethod
    def _first_time(alarm: Any, state: Mapping[str, Any], now: float) -> bool:
        """A loss limit is reported once per UTC day; any other kind is left to the cooldown."""
        from trading_research_agents.agents.alarms import DAILY_KINDS

        return alarm.kind not in DAILY_KINDS or state.get(f"{alarm.kind}_day") != _utc_day(now)

    def _streak(self, record: Mapping[str, Any], state: Mapping[str, Any]) -> Any:
        """An alarm when the newest round trip took a losing run to the threshold or past it."""
        from trading_research_agents.agents.alarms import LOSING_STREAK, Alarm, losing_streak
        from trading_research_agents.trading.positions import ROUND_TRIPS

        threshold = self.policy.losing_streak
        if threshold < 1:
            return None
        count, newest = losing_streak(self.runs / ROUND_TRIPS)
        if count < threshold or newest == state.get("streak_trip"):
            return None
        return Alarm(
            kind=LOSING_STREAK,
            detail=f"{count} losing round trips in a row, the newest at {newest.partition('|')[0]}",
            at=newest,
        )

    def after(self, record: Mapping[str, Any], ran: int, *, as_json: bool = False) -> list[str]:
        """Ask whoever is due, and say what they found. Never raises into the run."""
        who, fired = self.due(record, ran)
        if not who:
            return []
        from trading_research_agents.agents.audit import AuditConfig, run_audit
        from trading_research_agents.llm.usage import UsageLedger

        out = self.runs.parent / "audits"
        said = [
            f"audit: {', '.join(who)} asked"
            + (f" on {', '.join(a.kind for a in fired)}" if fired else f" after {ran} cycle(s)")
        ]
        for alarm in fired:
            said.append(f"  alarm {alarm.kind}: {alarm.detail}")
        try:
            usage = UsageLedger(out / "usage.jsonl")
            audit = AuditConfig(
                runs=self.runs,
                completer_for=_metered_roles(self.settings, usage, floor=REPORT_HEADROOM),
                usage=usage,
                root=out,
                config=self.config,
                auditors=who,
            )
            if not as_json and _watching():
                with _ui().console().status(f"asking {', '.join(who)}", spinner="line"):
                    result = run_audit(audit)
            else:
                result = run_audit(audit)
        except Exception as exc:
            # A failing auditor is a thing to report, never a thing that ends a run.
            said.append(f"  the audit could not run: {exc.__class__.__name__}: {exc}")
            if not as_json:
                print(plain_text("\n".join(said)), flush=True)
            return said
        said.extend(f"  {line}" for line in _audit_lines(result, []))
        if not as_json:
            print(plain_text("\n".join(said)), flush=True)
        return said


def _utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, tz=UTC).date().isoformat()
