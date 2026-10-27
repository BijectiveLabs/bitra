"""The lead's levels read against the instrument's own paths, after the exits are placed.

The structure lead names a side and where it is wrong and done. Code then draws the market forward
from its own closed candles and reads how often the take comes before the stop inside the hold.
The reading is a number the lead never sees, recorded on every cycle so the arena and hindsight
can say whether it separates good trades from bad. It refuses a trade only when the operator
declared `[targets].min_path_ev_bps`, and then a reading that cannot be made refuses too.
"""

from __future__ import annotations

import zlib
from decimal import Decimal
from typing import Any

from trading_research_agents.agents.cycle.pricing import _candles, _held_bars
from trading_research_agents.agents.cycle.record import _finish
from trading_research_agents.agents.cycle.state import Pass
from trading_research_agents.agents.progress import DONE, REFUSED, announce
from trading_research_agents.forecast.paths import PathError, PathSettings, read_paths
from trading_research_agents.records import write_json

BPS = Decimal(10_000)


def _distance_bps(level: str | None, mid: Decimal) -> float | None:
    if level is None or mid <= 0:
        return None
    return float(abs(Decimal(level) - mid) / mid * BPS)


def _ohlc(candles: list[Any]) -> list[list[float]]:
    return [[float(c.open), float(c.high), float(c.low), float(c.close)] for c in candles]


def _reading(p: Pass) -> dict[str, Any]:
    cfg, analyst = p.cfg, p.analyst
    mid = p.costs.cost.mid
    cost = float(p.costs.cost.round_trip_bps)
    reading = read_paths(
        _ohlc(_candles(p.snapshot)),
        side=analyst.target,
        take_bps=_distance_bps(p.exits.get("take_price"), mid),
        stop_bps=_distance_bps(p.exits.get("stop_price"), mid),
        horizon_bars=_held_bars(cfg, analyst.horizon_bars),
        # Seeded by the cycle, so the record replays to the same figures.
        settings=PathSettings(seed=zlib.crc32(p.where.path.name.encode("utf-8"))),
    )
    return {
        **reading.record(),
        "round_trip_bps": round(cost, 4),
        "expected_bps": round(reading.expected_bps(cost), 4),
    }


def read_the_paths(p: Pass) -> bool:
    """Record the paths' reading of the lead's levels; refuse below a declared floor."""
    cfg, result = p.cfg, p.result
    floor = cfg.targets.min_path_ev_bps
    try:
        said = _reading(p)
    except PathError as exc:
        result.briefs["paths"] = {"absent": str(exc)}
        if floor is None:
            announce(cfg.watch, "paths", DONE, f"not read: {exc}")
            return True
        return _refuse(p, f"paths: [targets].min_path_ev_bps is declared and {exc}")
    result.briefs["paths"] = said
    write_json(p.where.brief("paths"), said)
    detail = (
        f"take first {said['take_first']:.2f}, stop first {said['stop_first']:.2f}, "
        f"{said['expected_bps']:+.2f} bps net"
    )
    if floor is not None and Decimal(str(said["expected_bps"])) < floor:
        return _refuse(
            p,
            f"paths: {said['expected_bps']:+.2f} bps net over {said['paths']} paths at these "
            f"levels, under [targets].min_path_ev_bps {floor}",
        )
    announce(cfg.watch, "paths", DONE, detail)
    return True


def _refuse(p: Pass, why: str) -> bool:
    p.result.refusals.append(why)
    announce(p.cfg.watch, "paths", REFUSED, why)
    _finish(p.cfg, p.where, p.result)
    return False
