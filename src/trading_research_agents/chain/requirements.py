"""Layer 4: can the available data measure the premise at all?

A measurement taken with the wrong instrument returns a null that is indistinguishable from a
falsification. A recorder that only observed events where a second event happened to follow;
a scanner that saw position changes ninety seconds late. Both are cheap to detect here and
expensive to detect at layer 5, where they arrive dressed as results. A failure at this layer
is therefore never a death: it is `NOT PROVEN` with a blocking axis, and the hypothesis stays
open behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading_research_agents.chain.preregistration import Preregistration
from trading_research_agents.chain.seal import day_end_ns, day_start_ns
from trading_research_agents.data.manifest import Manifest
from trading_research_agents.ledger.entry import LedgerEntry
from trading_research_agents.ledger.vocabulary import Axis, Verdict

NS_PER_S = 1_000_000_000
LAYER = 4


@dataclass(frozen=True)
class Requirement:
    check: str
    ok: bool
    detail: str
    axis: Axis


@dataclass(frozen=True)
class Requirements:
    findings: tuple[Requirement, ...]

    @property
    def passed(self) -> bool:
        return all(f.ok for f in self.findings)

    @property
    def blocking_axis(self) -> Axis | None:
        for f in self.findings:
            if not f.ok:
                return f.axis
        return None


def check_requirements(
    registration: Preregistration,
    manifest: Manifest,
    *,
    resolution_margin: int = 10,
) -> Requirements:
    """Coverage, resolution and power feasibility, from the manifest alone.

    Resolution compares the horizon with the mean spacing between recorded trades; the horizon
    must be at least `resolution_margin` times that spacing, or the instrument cannot see what
    is claimed. Power feasibility counts how many non-overlapping horizons fit inside the
    research era, since that is the most independent observations the sample can hold.
    """
    if resolution_margin <= 0:
        raise ValueError("resolution_margin must be positive")
    findings: list[Requirement] = []

    era = registration.era
    lo, hi = day_start_ns(era.research_start), day_end_ns(era.research_end)
    first, last = _ns(manifest.first_ts), _ns(manifest.last_ts)
    covered = first <= lo and last >= hi - 1
    findings.append(
        Requirement(
            "coverage",
            covered,
            f"data spans {manifest.first_ts:%Y-%m-%d} to {manifest.last_ts:%Y-%m-%d}; research era "
            f"is {era.research_start} to {era.research_end}",
            Axis.DATA,
        )
    )

    in_universe = manifest.instrument in registration.universe.instruments
    findings.append(
        Requirement(
            "universe",
            in_universe,
            f"{manifest.instrument} is {'in' if in_universe else 'not in'} the declared universe",
            Axis.DATA,
        )
    )

    horizon_ns = int(registration.horizon.total_seconds() * NS_PER_S)
    if manifest.rows_accepted >= 2:
        mean_gap_ns = (last - first) // (manifest.rows_accepted - 1)
    else:
        mean_gap_ns = None
    fine_enough = mean_gap_ns is not None and horizon_ns >= resolution_margin * mean_gap_ns
    findings.append(
        Requirement(
            "resolution",
            fine_enough,
            (
                f"mean spacing between trades {mean_gap_ns / NS_PER_S:.3f}s against a horizon of "
                f"{registration.horizon}; margin {resolution_margin}x"
                if mean_gap_ns is not None
                else "fewer than two trades on record"
            ),
            Axis.CADENCE,
        )
    )

    independent = max(0, (min(last, hi) - max(first, lo)) // horizon_ns) if horizon_ns else 0
    enough = independent >= registration.power.required_sample
    findings.append(
        Requirement(
            "power_feasibility",
            enough,
            f"at most {independent} non-overlapping horizons in the covered research era "
            f"against a declared sample of {registration.power.required_sample}",
            Axis.POWER,
        )
    )
    return Requirements(tuple(findings))


def to_entry(
    requirements: Requirements,
    registration: Preregistration,
    *,
    entry_id: str,
    recorded_at: datetime,
) -> LedgerEntry:
    """A layer-4 failure is a blocked hypothesis, not a dead one."""
    if requirements.passed:
        raise ValueError("requirements passed; there is nothing to record at layer 4")
    axis = requirements.blocking_axis
    if axis is None:  # pragma: no cover - passed is False so an axis exists
        raise RuntimeError("no blocking axis")
    failed = "; ".join(f"{f.check}: {f.detail}" for f in requirements.findings if not f.ok)
    return LedgerEntry(
        entry_id=entry_id,
        subject=registration.name,
        verdict=Verdict.NOT_PROVEN,
        recorded_at=recorded_at,
        layer=LAYER,
        blocking_axis=axis,
        evidence=(f"preregistration:{registration.seal()}",),
        note=f"coverage-limited, not falsified: {failed}",
    )


def _ns(ts: datetime) -> int:
    return int(ts.timestamp() * NS_PER_S)
