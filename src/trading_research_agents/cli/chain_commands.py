"""The falsification chain from a shell: the cost floor, the observation, the registration, the
requirements and the verdict.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

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
    _desk_ui,
    emit,
)
from trading_research_agents.data import (
    read_manifest,
)
from trading_research_agents.envelope import load_envelope
from trading_research_agents.ledger import Ledger, Verdict


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
    emit(payload, args.json, lines, lambda: _desk_ui().observe_view(payload))
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
