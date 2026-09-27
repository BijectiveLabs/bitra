"""Is this dataset safe to backtest on?

One pass over the canonical stream, bounded memory, and a list of findings a reader can act
on. A `fail` finding means the dataset should not carry a verdict as it stands. A `warn`
finding is something the reader must look at and decide about; the report does not decide
for them, and the manifest keeps the finding either way.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from trading_research_agent.data.schema import Rejection, Trade, scale_of

Severity = Literal["fail", "warn"]

DUPLICATE_WINDOW = 10_000
EXAMPLES = 5


@dataclass(frozen=True)
class Finding:
    check: str
    severity: Severity
    detail: str


@dataclass
class AuditReport:
    venue: str | None = None
    instrument: str | None = None
    rows_accepted: int = 0
    rows_rejected: int = 0
    first_ts_ns: int | None = None
    last_ts_ns: int | None = None
    price_scales: tuple[int, ...] = ()
    qty_scales: tuple[int, ...] = ()
    findings: tuple[Finding, ...] = ()
    examples: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.rows_accepted > 0 and not any(f.severity == "fail" for f in self.findings)

    @property
    def first_ts(self) -> datetime | None:
        return _dt(self.first_ts_ns)

    @property
    def last_ts(self) -> datetime | None:
        return _dt(self.last_ts_ns)


def _dt(ns: int | None) -> datetime | None:
    return None if ns is None else datetime.fromtimestamp(ns / 1_000_000_000, tz=UTC)


def audit(
    records: Iterable[Trade | Rejection],
    *,
    max_gap: timedelta,
    max_rejected_fraction: Decimal = Decimal("0.001"),
    expected_venue: str | None = None,
    expected_instrument: str | None = None,
) -> AuditReport:
    """Walk the stream once and report what would make a verdict on it untrustworthy.

    `max_gap` is declared by the caller because it belongs to the instrument and the venue,
    not to this function. A quiet hour on a thin listing is normal; on the largest perpetual
    it is a dead feed, and the file cannot tell the two apart.
    """
    if max_gap <= timedelta(0):
        raise ValueError("max_gap must be positive")
    max_gap_ns = int(max_gap.total_seconds() * 1_000_000_000)

    report = AuditReport(venue=expected_venue, instrument=expected_instrument)
    examples: dict[str, list[str]] = {}

    def note(check: str, text: str) -> None:
        bucket = examples.setdefault(check, [])
        if len(bucket) < EXAMPLES:
            bucket.append(text)

    counts: Counter[str] = Counter()
    recent_ids: deque[str] = deque(maxlen=DUPLICATE_WINDOW)
    recent_set: set[str] = set()
    per_day: Counter[str] = Counter()
    price_scales: dict[int, int] = {}
    qty_scales: dict[int, int] = {}
    largest_gaps: list[tuple[int, int]] = []
    prev: Trade | None = None

    for record in records:
        if isinstance(record, Rejection):
            report.rows_rejected += 1
            note("rejected_rows", f"line {record.line}: {record.reason}")
            continue

        trade = record
        report.rows_accepted += 1
        if report.venue is None:
            report.venue = trade.venue
        if report.instrument is None:
            report.instrument = trade.instrument
        if trade.venue != report.venue or trade.instrument != report.instrument:
            counts["mismatch"] += 1
            note("mismatch", f"{trade.trade_id}: {trade.venue}/{trade.instrument}")

        if report.first_ts_ns is None:
            report.first_ts_ns = trade.ts_ns
        report.last_ts_ns = trade.ts_ns

        if trade.trade_id in recent_set:
            counts["duplicates"] += 1
            note("duplicates", trade.trade_id)
        else:
            if len(recent_ids) == recent_ids.maxlen:
                recent_set.discard(recent_ids[0])
            recent_ids.append(trade.trade_id)
            recent_set.add(trade.trade_id)

        if prev is not None:
            if trade.ts_ns < prev.ts_ns:
                counts["out_of_order"] += 1
                note("out_of_order", f"{trade.trade_id} at {trade.ts} after {prev.ts}")
            gap = trade.ts_ns - prev.ts_ns
            if gap > max_gap_ns:
                counts["gaps"] += 1
                largest_gaps.append((gap, prev.ts_ns))
                largest_gaps.sort(reverse=True)
                del largest_gaps[EXAMPLES:]
            if trade.seq is not None and prev.seq is not None and trade.seq <= prev.seq:
                counts["sequence_resets"] += 1
                note("sequence_resets", f"seq {trade.seq} after {prev.seq}")

        _track_scale(price_scales, scale_of(trade.price), report.rows_accepted, "price", note)
        _track_scale(qty_scales, scale_of(trade.qty), report.rows_accepted, "qty", note)
        per_day[trade.ts.date().isoformat()] += 1
        prev = trade

    findings: list[Finding] = []
    total = report.rows_accepted + report.rows_rejected
    if report.rows_accepted == 0:
        findings.append(Finding("empty", "fail", "no row became a trade"))
    if report.rows_rejected:
        fraction = Decimal(report.rows_rejected) / Decimal(total)
        severity: Severity = "fail" if fraction > max_rejected_fraction else "warn"
        findings.append(
            Finding(
                "rejected_rows",
                severity,
                f"{report.rows_rejected} of {total} rows could not become trades",
            )
        )
    for check, severity in (
        ("mismatch", "fail"),
        ("duplicates", "fail"),
        ("out_of_order", "fail"),
        ("sequence_resets", "fail"),
    ):
        if counts[check]:
            findings.append(Finding(check, severity, f"{counts[check]} occurrences"))
    if counts["gaps"]:
        worst = ", ".join(
            f"{g / 1_000_000_000:.1f}s after {_dt(at):%Y-%m-%d %H:%M:%S}Z" for g, at in largest_gaps
        )
        findings.append(
            Finding("gaps", "warn", f"{counts['gaps']} intervals above {max_gap}; largest: {worst}")
        )
    if len(price_scales) > 1:
        findings.append(Finding("price_scale", "warn", _scale_text(price_scales)))
    if len(qty_scales) > 1:
        findings.append(Finding("qty_scale", "warn", _scale_text(qty_scales)))
    if len(per_day) >= 2:
        day, busiest = per_day.most_common(1)[0]
        share = Decimal(busiest) / Decimal(report.rows_accepted)
        if share > Decimal("0.5"):
            findings.append(
                Finding("concentration", "warn", f"{day} carries {share:.0%} of all rows")
            )

    report.price_scales = tuple(sorted(price_scales))
    report.qty_scales = tuple(sorted(qty_scales))
    report.findings = tuple(findings)
    report.examples = {k: tuple(v) for k, v in examples.items()}
    return report


def _track_scale(
    seen: dict[int, int], scale: int, row: int, label: str, note: Callable[[str, str], None]
) -> None:
    if scale not in seen:
        seen[scale] = row
        if len(seen) > 1:
            note(f"{label}_scale", f"scale {scale} first seen at accepted row {row}")


def _scale_text(seen: dict[int, int]) -> str:
    parts = ", ".join(f"{scale} (from row {row})" for scale, row in sorted(seen.items()))
    return f"decimal places change within the file: {parts}"
