"""The data commands: archives fetched and audited, live snapshots, the sources shelf, run records,
the universe and the venues.
"""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from trading_research_agents.agents.context import (
    TEXT_CLASSES,
    declared_in,
)
from trading_research_agents.cli.common import (
    CLEARED,
    REFUSED,
    _cycle_ui,
    _desk_data,
    _load_env,
    _refresh_news,
    emit,
    parse_month,
)
from trading_research_agents.cli.market import data_bars, verify_market
from trading_research_agents.cli.venue_access import _discovered_universe
from trading_research_agents.data import (
    NORMALISER_VERSION,
    MappingError,
    Rejection,
    Source,
    Trade,
    archive_venue,
    audit,
    load_mapping,
    manifest_from_audit,
    mapped_table,
    mapped_trades,
    open_archive_lines,
    sha256_of,
    write_manifest,
)
from trading_research_agents.data.fetch import fetch, fetch_checksum
from trading_research_agents.data.parquet import write_trades
from trading_research_agents.trading import (
    ConfigError,
    load_strategy_config,
)

DURATION = re.compile(r"^(\d+)([smhd])$")


DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(text: str) -> timedelta:
    match = DURATION.match(text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"{text!r} is not a duration like 30s, 5m, 1h or 2d")
    return timedelta(**{DURATION_UNITS[match.group(2)]: int(match.group(1))})


def data_fetch(args: argparse.Namespace) -> int:
    venue = archive_venue(args.venue)
    url = venue.archive_url(args.instrument, day=args.day, month=args.month)
    filename = url.rsplit("/", 1)[1]
    checksum_url = venue.checksum_url(url)
    expected = fetch_checksum(checksum_url, filename) if checksum_url else None
    dest = args.out / filename
    source = fetch(url, dest, expected_sha256=expected)
    payload = {"path": str(dest), "sha256": source.sha256, "size": source.size, "url": url}
    verified = "verified against the venue's checksum" if expected else "sha256 recorded"
    emit(
        payload,
        args.json,
        [f"{dest}", f"sha256 {source.sha256} ({verified})", f"{source.size} bytes"],
    )
    return CLEARED


def data_audit(args: argparse.Namespace) -> int:
    table = args.archive.suffix.lower() == ".parquet"
    if args.venue is not None:
        if table:
            raise MappingError(
                "a venue plugin reads the venue's own archive; a Parquet file of your own is read "
                "through a column map (--mapping mapping.toml)"
            )
        source_name = str(args.venue)
        plugin = archive_venue(source_name).normalise

        def read() -> Iterator[Trade | Rejection]:
            return plugin(open_archive_lines(args.archive), instrument=args.instrument)
    else:
        mapping = load_mapping(args.mapping)
        source_name = mapping.source

        def read() -> Iterator[Trade | Rejection]:
            if table:
                return mapped_table(args.archive, mapping, instrument=args.instrument)
            return mapped_trades(
                open_archive_lines(args.archive), mapping, instrument=args.instrument
            )

    records = read()
    report = audit(
        records,
        max_gap=args.max_gap,
        max_rejected_fraction=args.max_rejected,
        expected_venue=source_name,
        expected_instrument=args.instrument,
    )
    if args.manifest is not None and report.rows_accepted:
        source = Source(args.archive.name, sha256_of(args.archive), args.archive.stat().st_size)
        manifest = manifest_from_audit(
            report,
            sources=(source,),
            normaliser_version=NORMALISER_VERSION,
            created_at=datetime.now(UTC),
        )
        write_manifest(args.manifest, manifest)

    lines = [
        f"{report.venue or source_name} {report.instrument or args.instrument}: "
        f"{report.rows_accepted} trades, {report.rows_rejected} rejected",
    ]
    if report.first_ts and report.last_ts:
        lines.append(f"{report.first_ts:%Y-%m-%d %H:%M:%S}Z to {report.last_ts:%Y-%m-%d %H:%M:%S}Z")
    for finding in report.findings:
        lines.append(f"  {finding.severity.upper():4} {finding.check}: {finding.detail}")
        for example in report.examples.get(finding.check, ()):
            lines.append(f"         {example}")
    written = None
    if args.parquet is not None and report.passed:
        # Second pass over the file. The audit already told us the scales the source uses.
        again = read()
        result = write_trades(
            (r for r in again if isinstance(r, Trade)),
            args.parquet,
            price_scale=max(report.price_scales),
            qty_scale=max(report.qty_scales),
        )
        written = str(result.path)
        lines.append(
            f"wrote {result.rows} trades to {result.path} "
            f"(decimal scales {result.price_scale}/{result.qty_scale})"
        )
    lines.append("PASSED" if report.passed else "FAILED")
    payload = {
        "parquet": written,
        "passed": report.passed,
        "venue": report.venue,
        "instrument": report.instrument,
        "rows_accepted": report.rows_accepted,
        "rows_rejected": report.rows_rejected,
        "first_ts": report.first_ts,
        "last_ts": report.last_ts,
        "findings": [
            {"check": f.check, "severity": f.severity, "detail": f.detail} for f in report.findings
        ],
        "examples": report.examples,
    }
    emit(payload, args.json, lines)
    return CLEARED if report.passed else REFUSED


def show_universe(args: argparse.Namespace) -> int:
    """The instruments a pass would look at now: declared, and found by discovery."""
    cfg = load_strategy_config(args.config)
    universe, found, said = _discovered_universe(cfg)
    lines = list(said)
    if found is None and not said:
        lines.append(
            "discovery is off ([universe].discover); the pass looks at "
            f"{', '.join(universe.instruments) or 'nothing'}"
        )
    if found is not None:
        dropped = ", ".join(f"{n} {why}" for why, n in sorted(found["dropped"].items()))
        lines.append(f"{len(found['kept'])} of {found['markets']} perpetuals kept ({dropped})")
        lines.append(
            f"{'instrument':<16}{'24h volume':>14} {'spread bps':>11} {'24h %':>9} "
            f"{'step':>12} {'max lev':>8} {'listed d':>9}"
        )
        for row in found["kept"]:
            volume = f"{Decimal(row['quote_volume_24h']) / 1_000_000:,.0f}M"
            step = format(Decimal(row["step"]).normalize(), "f")
            lines.append(
                f"{row['instrument']:<16}{volume:>14} {row['spread_bps']:>11} "
                f"{row['change_24h_pct'] or '-':>9} {step:>12} "
                f"{row['max_leverage'] or '-':>8} {row['listed_days'] or '-':>9}"
            )
        lines.append(f"this pass looks at: {', '.join(universe.instruments)}")
    emit({"instruments": list(universe.instruments), "discovered": found}, args.json, lines)
    return 0


def runs_verify(args: argparse.Namespace) -> int:
    """Whether the desk's records are what it wrote, and its briefs what its inputs give."""
    if args.market is not None:
        payload, lines, code = verify_market(args.market)
        emit(payload, args.json, lines)
        return code
    from trading_research_agents.agents.verify import verify_runs

    found = verify_runs(args.out or Path("runs") / "cycles", days=args.days)
    lines = [
        f"{found['checked']} of {found['cycles']} cycle(s) checked: {found['hashed']} against "
        f"their hashes, {found['recomputed']} recomputed from their inputs; "
        f"{found['pruned']} pruned (their index lines remain)"
    ]
    for failed in found["failed"]:
        lines.extend(f"FAILED {failed['cycle']}: {problem}" for problem in failed["problems"])
    if not found["failed"]:
        lines.append("no record differs from what the desk wrote")
    emit(found, args.json, lines)
    return 1 if found["failed"] else 0


def runs_prune(args: argparse.Namespace) -> int:
    """Old cycle directories out, only when asked twice: once to see, once with --yes."""
    from trading_research_agents.store.prune import apply, plan

    root = args.out or Path("runs") / "cycles"
    chosen = plan(root, args.keep)
    found = chosen.record()
    megabytes = chosen.size / 1_000_000
    said = (
        f"{len(chosen.folders)} cycle director(ies) and {len(chosen.screens)} screen(s) older than "
        f"{chosen.cutoff}, {megabytes:.1f} MB"
    )
    if not args.yes:
        emit({**found, "applied": False}, args.json, [f"would remove {said}; add --yes to do it"])
        return 0
    kept = apply(root, chosen, archive=args.archive)
    where = f"; archived under {kept}" if kept else ""
    emit(
        {**found, "applied": True, "archive": str(kept) if kept else None},
        args.json,
        [f"removed {said}{where}; the index and the logs are kept"],
    )
    return 0


def _shelf_series(args: argparse.Namespace) -> list[Path]:
    shelf = declared_in(args.config.resolve().parent)
    return [path for name, path in sorted(shelf.classes.items()) if name not in TEXT_CLASSES]


def sources_show(args: argparse.Namespace) -> int:
    """Each declared series, so nobody has to take an analyst's word for what it read."""
    from trading_research_agents.data.pull import describe
    from trading_research_agents.data.series import load_series_map

    now = datetime.now(UTC)
    rows = [
        {"file": str(path.name), **describe(spec, path.resolve().parent, now)}
        for path in _shelf_series(args)
        for spec in load_series_map(path)
    ]
    lines = []
    for row in rows:
        if "error" in row:
            state = f"unreadable: {row['error']}"
        else:
            state = f"last {row['last'][:16]} = {row['value']}, {row['age_h']}h old" + (
                "" if row["counted"] else f", past max_age {row['max_age']}: not counted"
            )
        lines.append(f"{row['series']:<20} {row['from']}; {row['source'] or '-'}; {state}")
    if not rows:
        lines.append("no series declared under sources/; no reading is read")
    emit({"series": rows}, args.json, lines)
    return 0 if all(r.get("counted") for r in rows) else 1


def sources_pull(args: argparse.Namespace) -> int:
    """Refresh every fetched series now, whatever its refresh interval says."""
    from trading_research_agents.data.pull import PullError, pull
    from trading_research_agents.data.series import load_series_map

    _load_env(args)
    rows: list[dict[str, Any]] = []
    for path in _shelf_series(args):
        for spec in load_series_map(path):
            if spec.fetch is None:
                rows.append({"series": spec.name, "skipped": "kept by hand; no fetch declared"})
                continue
            try:
                got = pull(spec, path.resolve().parent)
            except PullError as exc:
                rows.append({"series": spec.name, "error": str(exc)})
            else:
                rows.append(
                    {"series": got.series, "added": got.added, "rows": got.rows, "last": got.last}
                )
    lines = [
        f"{r['series']:<20} "
        + (
            f"{r['added']} new, {r['rows']} rows, last {r['last']}"
            if "added" in r
            else r.get("skipped") or f"failed: {r['error']}"
        )
        for r in rows
    ]
    shelf = declared_in(args.config.resolve().parent)
    for name, path in sorted(shelf.classes.items()):
        if name in TEXT_CLASSES:
            lines.extend(_refresh_news(path, datetime.now(UTC), every=True))
    emit({"pulled": rows}, args.json, lines or ["no series declared under sources/"])
    return 1 if any("error" in r for r in rows) else 0


def sources_add(args: argparse.Namespace) -> int:
    """One series onto the shelf in one step: pulled now, lines drawn from its own history."""
    from trading_research_agents.data.pull import PullError
    from trading_research_agents.data.shelf import ShelfError, add_series

    _load_env(args)
    fetch: dict[str, Any] = {"from": args.origin}
    for key, value in (
        ("id", args.id),
        ("minus", args.minus),
        ("asset", args.fetch_asset),
        ("url", args.url),
        ("rows", args.rows),
        ("ts", args.ts),
        ("ts_unit", args.ts_unit),
        ("value", args.value),
        ("key_env", args.key_env),
        ("key_param", args.key_param),
        ("key_header", args.key_header),
        ("key_prefix", args.key_prefix),
    ):
        if value is not None:
            fetch[key] = value
    if args.param:
        pairs = [item.partition("=") for item in args.param]
        if any(not field or not sep for field, sep, _ in pairs):
            raise ConfigError("--param is FIELD=VALUE, such as symbol=BTCUSDT")
        fetch["params"] = {field.strip(): value.strip() for field, _, value in pairs}
    try:
        added = add_series(
            args.config.resolve().parent,
            args.kind,
            args.name,
            fetch,
            high=args.high,
            band=args.band,
            days=args.days,
            asset=args.asset,
            unit=args.unit,
            now=datetime.now(UTC),
        )
    except (ShelfError, PullError) as exc:
        raise ConfigError(str(exc)) from exc
    lines = [
        f"{added.series}: {added.rows} rows pulled, lines drawn from {added.window} of them",
        f"appended to {added.path}:",
        "",
        *added.block.rstrip().splitlines(),
        "",
        "the lines are a starting point drawn from the series itself; move them if you know better",
    ]
    emit(
        {
            "series": added.series,
            "path": str(added.path),
            "rows": added.rows,
            "lower": str(added.lower),
            "upper": str(added.upper),
        },
        args.json,
        lines,
    )
    return CLEARED


def data_get(args: argparse.Namespace) -> int:
    """One day of one instrument, from the venue to a dataset the runs can use."""
    venue_name = args.venue
    max_gap = args.max_gap
    if args.config.is_file():
        raw = tomllib.loads(args.config.read_text(encoding="utf-8"))
        venue_name = venue_name or str(raw.get("run", {}).get("venue_key", "")).partition("/")[0]
        declared = raw.get("data", {}).get("max_gap")
        if max_gap is None and declared:
            max_gap = parse_duration(str(declared))
    if not venue_name:
        raise ValueError("no strategy.toml here to take the venue from; pass --venue")
    if max_gap is None:
        max_gap = timedelta(minutes=5)
    day = month = None
    try:
        day = date.fromisoformat(args.when)
    except ValueError:
        month = parse_month(args.when)
    venue = archive_venue(venue_name)
    args.out.mkdir(parents=True, exist_ok=True)
    fetched = argparse.Namespace(
        venue=venue_name, instrument=args.instrument, day=day, month=month, out=args.out, json=False
    )
    data_fetch(fetched)
    url = venue.archive_url(args.instrument, day=day, month=month)
    archive = args.out / url.rsplit("/", 1)[1]
    stem = f"{args.instrument}-{args.when}"
    audited = argparse.Namespace(
        archive=archive,
        venue=venue_name,
        mapping=None,
        instrument=args.instrument,
        max_gap=max_gap,
        max_rejected=Decimal("0.001"),
        manifest=args.out / f"{stem}.manifest.json",
        parquet=args.out / f"{stem}.parquet",
        json=False,
    )
    code = data_audit(audited)
    if code != CLEARED:
        return code
    if getattr(venue, "funding_rates", None) is not None:
        if day is not None:
            start, end = day - timedelta(days=1), day + timedelta(days=1)
        else:
            year, mon = month or (1970, 1)
            start = date(year, mon, 1) - timedelta(days=1)
            end = start + timedelta(days=33)
        funded = argparse.Namespace(
            venue=venue_name,
            instrument=args.instrument,
            start=start,
            end=end,
            out=args.out / f"{args.instrument}-funding.json",
            json=False,
        )
        data_funding(funded)
    print(f"ready: {audited.parquet}  (max gap {max_gap}; now `tra backtest`)")
    return CLEARED


def data_bars_command(args: argparse.Namespace) -> int:
    """Flow bars and derivatives history for a range of days, into the market archive."""
    payload, lines, code = data_bars(args, fetch=data_get)
    emit(payload, args.json, lines)
    return code


def data_pull(args: argparse.Namespace) -> int:
    """One snapshot: walk each declared chain once, write what answered and what did not."""
    from trading_research_agents.data.sources import load_data_config
    from trading_research_agents.store import cycle_id, take_snapshot

    _load_env(args)
    desk = load_strategy_config(args.config) if args.config.is_file() else None
    cfg = (
        _desk_data(args.data_config, desk)
        if desk is not None
        else load_data_config(args.data_config).on_venue("")
    )
    instrument = args.instrument
    if instrument is None:
        if desk is None:
            raise ValueError("name an instrument, or run inside a project with strategy.toml")
        instrument = desk.run.instrument
    kinds = list(args.kind) if args.kind else [k.kind for k in cfg.kinds]
    chains = {kind: cfg.chain(kind) for kind in kinds}
    options = {k.kind: dict(k.options) for k in cfg.kinds}
    cycle = cycle_id()
    snapshot = take_snapshot(instrument, chains, cycle=cycle, kinds=kinds, options=options)
    out = args.out
    if out is None:
        # Beside the cycles, not among them: a pull decided nothing, and everything that walks
        # runs/cycles - the trigger, the desk, the auditors, tra watch - reads only cycles.
        out = Path("runs") / "pulls" / cycle / "snapshot.json"
        out.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write(out)
    payload = snapshot.record()
    lines = [f"snapshot {cycle} {instrument}: {len(snapshot.values)} of {len(kinds)} kinds"]
    for kind, fetched in sorted(snapshot.values.items()):
        row = fetched.record()
        lines.append(f"  {kind:<14} {row['source']:<18} {row['tier']:<5} age {row['age_s']}s")
    for attempt in snapshot.attempts:
        if attempt.outcome != "ok":
            lines.append(f"  tried {attempt.source} for {attempt.kind}: {attempt.detail}")
    for kind, reason in snapshot.refused.items():
        lines.append(f"  refused {kind}: {reason}")
    if snapshot.stale:
        lines.append(f"stale, and used because the config allows it: {', '.join(snapshot.stale)}")
    lines.append(f"snapshot.json in {out.parent}")
    emit(payload, args.json, lines, lambda: _cycle_ui().snapshot_view(payload))
    return CLEARED if snapshot.usable else REFUSED


def data_funding(args: argparse.Namespace) -> int:
    venue = archive_venue(args.venue)
    fetch_rates = getattr(venue, "funding_rates", None)
    if fetch_rates is None:
        raise ValueError(f"venue {args.venue!r} does not publish funding through its plugin")
    if args.end < args.start:
        raise ValueError("--to is before --from")
    start_ms = int(datetime.combine(args.start, time.min, tzinfo=UTC).timestamp() * 1000)
    end_ms = int(datetime.combine(args.end, time.max, tzinfo=UTC).timestamp() * 1000)
    rates = fetch_rates(args.instrument, start_ms=start_ms, end_ms=end_ms)
    if not rates:
        raise ValueError(
            f"no settlements for {args.instrument} between {args.start} and {args.end}"
        )
    gaps = sorted((b.ts_ns - a.ts_ns) // 1_000_000_000 for a, b in pairwise(rates))
    payload: dict[str, Any] = {
        "venue": args.venue,
        "instrument": args.instrument,
        "from": args.start.isoformat(),
        "to": args.end.isoformat(),
        "fetched_at": datetime.now(UTC).isoformat(),
        "rates": {str(r.ts_ns): str(r.rate) for r in rates},
    }
    if gaps:
        # The settlement clock is measured from the data, never assumed.
        payload["interval_s"] = gaps[len(gaps) // 2]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    first = datetime.fromtimestamp(rates[0].ts_ns / 1e9, tz=UTC)
    last = datetime.fromtimestamp(rates[-1].ts_ns / 1e9, tz=UTC)
    lines = [
        f"{args.venue} {args.instrument}: {len(rates)} settlements, "
        f"{first:%Y-%m-%d %H:%M}Z to {last:%Y-%m-%d %H:%M}Z"
        + (f", every {payload['interval_s']} s" if gaps else ""),
        f"wrote {args.out}",
    ]
    emit({**payload, "count": len(rates), "out": str(args.out)}, args.json, lines)
    return CLEARED
