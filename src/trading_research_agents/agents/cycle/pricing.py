"""Prices, exits and sizes: arithmetic over the snapshot and the operator's bounds."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from trading_research_agents.agents.cycle.config import CycleConfig, CycleError
from trading_research_agents.agents.cycle.result import CycleResult
from trading_research_agents.envelope.model import Channel
from trading_research_agents.execution.desk import Open
from trading_research_agents.execution.stops import Placement, StopError
from trading_research_agents.store.snapshot import (
    Snapshot,
)


def _held(cfg: CycleConfig, result: CycleResult) -> tuple[tuple[Open, ...], str]:
    """What this desk is already carrying, from wherever the caller said to look.

    A reader that cannot answer is not a reason to lose the cycle, but it is a reason to refuse the
    trade: a desk limit measured against an unknown book is not a limit. The failure is recorded and
    the book comes back empty with the reason in its place.
    """
    if cfg.held is None:
        return (), "nothing was asked; no reader was given to this cycle"
    try:
        return cfg.held()
    except Exception as exc:
        result.refusals.append(
            f"desk: could not read what is open: {exc.__class__.__name__}: {exc}"
        )
        return (Open(instrument="unknown"),), f"unreadable: {exc.__class__.__name__}"


def _exits(
    cfg: CycleConfig,
    mid: Decimal,
    target: str,
    *,
    snapshot: Snapshot | None = None,
    horizon_bars: int = 1,
    stop_price: Decimal | None = None,
    target_price: Decimal | None = None,
) -> tuple[dict[str, str], Placement | None]:
    """Where the stop and the take profit sit, and what measured them when the market did.

    A position without an exit is not a trade, so these travel with the intent and, in venue mode,
    go to the venue attached to the entry. Nothing here asks a model where a stop belongs. Fixed
    mode uses the operator's fractions, each half on its own; volatility mode measures both from
    the decision timeframe's closed candles.
    """
    if mid <= 0:
        return {}, None
    stop_frac, take_frac, placement = cfg.stop_frac, cfg.take_frac, None
    if cfg.volatility_exit is not None:
        candles = _candles(snapshot)
        if not candles:
            raise StopError("no closed candles in this snapshot to measure a stop from")
        placement = cfg.volatility_exit.place(
            candles,
            horizon_bars=horizon_bars,
            mid=mid,
            side=target,
            stop_price=stop_price,
            target_price=target_price,
        )
        stop_frac, take_frac = placement.stop_frac, placement.take_frac
    away = Decimal(1) if target == "long" else Decimal(-1)
    out: dict[str, str] = {}
    if stop_frac is not None:
        out["stop_price"] = str((mid * (Decimal(1) - away * stop_frac)).quantize(mid))
    if take_frac is not None:
        out["take_price"] = str((mid * (Decimal(1) + away * take_frac)).quantize(mid))
    return out, placement


def _candles(snapshot: Snapshot | None) -> list[Any]:
    """The decision timeframe's closed candles, oldest first, or none."""
    if snapshot is None:
        return []
    try:
        value = snapshot.get("ohlcv").value
    except KeyError:
        return []
    return list(value) if isinstance(value, list | tuple) else []


def _position_cap(cfg: CycleConfig, mid: Decimal, base: Decimal) -> Decimal:
    """The most this cycle may hold, in units: the unit cap, the quote cap at this mid, or both."""
    caps = [cfg.max_position] if cfg.max_position is not None else []
    if cfg.max_position_quote is not None and mid > 0:
        caps.append(_quantized(cfg.max_position_quote / mid, cfg.size_step))
    return min(caps) if caps else base


def _claimed_edge(cfg: CycleConfig, placement: Placement | None) -> tuple[Decimal, str]:
    """The move the trade claims, for the cost gate and the targets, and who claimed it.

    When the analyst named a target and it survived the bounds, that target is the claim: it is the
    analyst's own reading of where the idea is done, already held to max_reward_to_risk and to the
    break-even bound. A take profit the system placed as a multiple of the stop claims nothing, so
    without a named target the operator's declared magnitude stands.
    """
    if placement is not None and placement.target_named:
        take = (placement.take_frac * Decimal(10_000)).quantize(Decimal("0.01"))
        return take, "the analyst's own target, held inside the [exit] bounds"
    return (
        cfg.magnitude_bps,
        "[agents].magnitude_bps, declared by the operator and not measured here",
    )


def _held_bars(cfg: CycleConfig, horizon: int) -> int:
    """The bars a position can stay open: the analyst's horizon capped by the timeout when the
    desk closes on the horizon, otherwise as long as the timeout allows."""
    if cfg.timeout_bars is None:
        return horizon
    if cfg.hold_limit == "horizon":
        return min(horizon, cfg.timeout_bars)
    return max(horizon, cfg.timeout_bars)


def _break_even(exits: Mapping[str, Any], round_trip_bps: Decimal) -> str | None:
    """The share of trades that must reach the take profit for the stop, the take and the round
    trip to come out even: (stop + cost) / (take + stop). Arithmetic on figures already priced,
    with no guess at the odds."""
    take, stop = exits.get("take_pnl_bps"), exits.get("stop_pnl_bps")
    if take is None or stop is None:
        return None
    gain, loss = Decimal(take), -Decimal(stop)
    if gain <= 0 or loss <= 0:
        return None
    return str(((loss + round_trip_bps) / (gain + loss)).quantize(Decimal("0.001")))


def _drawdown_room(cfg: CycleConfig, result: CycleResult) -> Decimal | None:
    """What is left under [controls].max_drawdown_bps after the drawdown already taken."""
    if cfg.max_drawdown_bps is None:
        return None
    taken = (result.briefs.get("equity") or {}).get("drawdown_bps")
    return cfg.max_drawdown_bps - (Decimal(str(taken)) if taken is not None else Decimal(0))


def _loss_at_stop(mid: Decimal, exits: Mapping[str, Any], round_trip_bps: Decimal) -> str | None:
    """What a stop hit loses, in bps of the notional: the distance and the round trip paid on it.

    Written on the intent so a finished trade can be read as a multiple of what it planned to lose,
    which is the unit the sizing measures a desk's record in.
    """
    stop = exits.get("stop_price")
    if stop is None or mid <= 0:
        return None
    distance = abs(Decimal(stop) - mid) / mid * 10_000
    return str((distance + max(round_trip_bps, Decimal(0))).quantize(Decimal("0.01")))


def _exit_view(mid: Decimal, target: str, exits: Mapping[str, str]) -> dict[str, str]:
    """The exits as a person reads them: a price and how far it is from the entry, signed."""
    out = {"side": target, "entry": str(mid)}
    for name in ("stop", "take"):
        price = exits.get(f"{name}_price")
        if price is None or mid <= 0:
            continue
        moved = ((Decimal(price) / mid - 1) * 10_000).quantize(Decimal("0.01"))
        out[f"{name}_price"] = price
        # The price's move, and what that move does to this position: a short gains as it falls.
        out[f"{name}_bps"] = str(moved)
        out[f"{name}_pnl_bps"] = str(moved if target == "long" else -moved)
    return out


def _quantized(size: Decimal, step: Decimal) -> Decimal:
    """Down to the venue's own increment, never up: a size it would refuse is not a size."""
    if step <= 0:
        return size
    return (size / step).to_integral_value(rounding="ROUND_DOWN") * step


def _prices(snapshot: Snapshot) -> tuple[Decimal | None, Decimal | None]:
    """The best bid and ask of this snapshot's book, as numbers, when it has one."""
    try:
        book = snapshot.get("orderbook").value
    except KeyError:
        return None, None
    bids, asks = getattr(book, "bids", ()), getattr(book, "asks", ())
    if not bids or not asks:
        return None, None
    return Decimal(str(bids[0].price)), Decimal(str(asks[0].price))


def _touch(snapshot: Snapshot) -> dict[str, str]:
    """The best bid and ask of this snapshot's book, when it has one."""
    bid, ask = _prices(snapshot)
    if bid is None or ask is None:
        return {}
    return {"bid": str(bid), "ask": str(ask)}


def _channel(cfg: CycleConfig) -> Channel:
    channel = cfg.channel
    if channel is None:  # pragma: no cover - CycleConfig refuses this
        raise CycleError(f"venue_key {cfg.venue_key!r} is not a channel in the envelope")
    return channel
