"""One loop for replay, paper and shadow.

    print -> venue.poll (fills) -> bars -> exits on the open position -> strategy
          -> controls -> venue

The runtime knows the feed and the venue only as protocols. Entries fill on the first print at
or after the measured latency; stops are evaluated on every closed bar with the adverse
assumptions of layer 6; timeouts and signal exits go back through the venue like any other
intent. Every intent, every refusal, every fill and every closed round trip is appended to its
own file as it happens, and the closed round trips are `trades.jsonl`, which is what layer 5
reads. That is the whole loop between the two agents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agent.data.schema import Direction, Trade
from trading_research_agent.strategy.core import Bar, Signal, Strategy, Target
from trading_research_agent.strategy.exits import (
    ExitRule,
    simulate_with_bracket,
    simulate_with_stop,
)
from trading_research_agent.trading.controls import Controls
from trading_research_agent.trading.feed import BarBuilder, Feed
from trading_research_agent.trading.venue import Fill, Intent, SimulatedVenue

BPS = Decimal(10_000)


@dataclass(frozen=True)
class TradingConfig:
    feed: Feed
    venue: SimulatedVenue
    strategy: Strategy
    exit_rule: ExitRule
    controls: Controls
    out_dir: Path
    bar_interval_ns: int
    size: Decimal
    spread_cost_bps: Decimal
    equity: Decimal | None = None
    label: str = "replay"

    def __post_init__(self) -> None:
        if self.bar_interval_ns <= 0:
            raise ValueError("bar_interval_ns must be positive")
        if not isinstance(self.size, Decimal) or self.size <= 0:
            raise ValueError("size must be a positive Decimal")
        if not isinstance(self.spread_cost_bps, Decimal) or self.spread_cost_bps < 0:
            raise ValueError("spread_cost_bps must be a non-negative Decimal")
        if self.equity is not None and self.equity <= 0:
            raise ValueError("equity, when known, is positive")


@dataclass
class Position:
    direction: Direction
    entry_ts_ns: int
    entry_px: Decimal
    size: Decimal
    stop_px: Decimal
    tp_px: Decimal | None
    bars_held: int = 0


@dataclass
class RunReport:
    label: str
    instrument: str
    started_at: datetime
    finished_at: datetime | None = None
    prints: int = 0
    bars: int = 0
    intents: int = 0
    refused: int = 0
    fills: int = 0
    round_trips: int = 0
    stopped_by: str | None = None
    open_position: dict[str, Any] | None = None
    net_bps_total: Decimal = Decimal(0)
    refusals: list[str] = field(default_factory=list)


class _Writer:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")


class _Loop:
    def __init__(self, config: TradingConfig) -> None:
        self.c = config
        self.builder = BarBuilder(config.bar_interval_ns)
        self.position: Position | None = None
        self.pending_close: bool = False
        self.report = RunReport(
            label=config.label, instrument=config.feed.instrument, started_at=datetime.now(UTC)
        )
        self.intents = _Writer(config.out_dir / "intents.jsonl")
        self.fills = _Writer(config.out_dir / "fills.jsonl")
        self.trades = _Writer(config.out_dir / "trades.jsonl")
        self.last_price: Decimal | None = None

    # Prints

    def on_print(self, trade: Trade) -> None:
        self.report.prints += 1
        self.last_price = trade.price
        fill = self.c.venue.poll(trade)
        if fill is not None:
            self.on_fill(fill)
        for bar in self.builder.push(trade):
            self.on_bar(bar)

    def on_fill(self, fill: Fill) -> None:
        self.report.fills += 1
        self.fills.append(
            {
                "ts_ns": fill.ts_ns,
                "price": str(fill.price),
                "size": str(fill.size),
                "direction": fill.direction.value,
                "closes": self.pending_close,
            }
        )
        if self.pending_close and self.position is not None:
            self.close(self.position, fill.ts_ns, fill.price, cause="intent")
            self.pending_close = False
            return
        self.position = Position(
            direction=fill.direction,
            entry_ts_ns=fill.ts_ns,
            entry_px=fill.price,
            size=fill.size,
            stop_px=self.c.exit_rule.stop_for(fill.price, fill.direction),
            tp_px=self.c.exit_rule.take_profit_for(fill.price, fill.direction),
        )

    # Bars

    def on_bar(self, bar: Bar) -> None:
        self.report.bars += 1
        if self.position is not None and not self.pending_close:
            self.evaluate_exits(self.position, bar)
        if (
            self.c.controls.kill_switch.armed
            and self.position is not None
            and not self.pending_close
        ):
            self.request(bar.end_ns, Target.FLAT, "kill switch armed; flattening")
            self.report.stopped_by = "kill switch"
        signal = self.c.strategy.on_bar(bar)
        if signal is not None:
            self.on_signal(bar, signal)

    def evaluate_exits(self, position: Position, bar: Bar) -> None:
        position.bars_held += 1
        if bar.empty:
            return
        rule = self.c.exit_rule
        if position.tp_px is not None:
            bracket = simulate_with_bracket(
                position.entry_px,
                position.direction,
                position.tp_px,
                position.stop_px,
                [bar],
                bar.close or position.entry_px,
                rule.slip_bps,
            )
            if bracket.cause.value != "exit":
                self.close(position, bar.end_ns, bracket.exit_px, cause=bracket.cause.value)
                return
        else:
            outcome = simulate_with_stop(
                position.direction,
                position.stop_px,
                [bar],
                bar.close or position.entry_px,
                rule.slip_bps,
            )
            if outcome.stopped:
                self.close(position, bar.end_ns, outcome.exit_px, cause="sl")
                return
        if position.bars_held >= rule.timeout_bars:
            self.request(bar.end_ns, Target.FLAT, f"timeout after {position.bars_held} bars")

    def on_signal(self, bar: Bar, signal: Signal) -> None:
        if self.c.venue.pending is not None:
            return
        held = None if self.position is None else self.position.direction
        wanted = {Target.LONG: Direction.LONG, Target.SHORT: Direction.SHORT}.get(signal.target)
        if wanted is None:
            if held is not None:
                self.request(bar.end_ns, Target.FLAT, signal.reason)
            return
        if held is None:
            self.request(bar.end_ns, signal.target, signal.reason)
        elif held is not wanted:
            # Reversal is two orders: flatten now, and the strategy may ask again next bar.
            self.request(bar.end_ns, Target.FLAT, f"reversal: {signal.reason}")

    # Intents

    def request(self, ts_ns: int, target: Target, reason: str) -> None:
        risk = None
        equity_known = self.c.equity is not None
        if target is not Target.FLAT and equity_known and self.last_price is not None:
            notional = self.c.size * self.last_price
            risk = self.c.exit_rule.stop_sigma_frac * BPS * notional / (self.c.equity or Decimal(1))
        admission = self.c.controls.admit(
            ts_ns=ts_ns, target=target, size=self.c.size, risk_bps=risk, equity_known=equity_known
        )
        self.report.intents += 1
        self.intents.append(
            {
                "ts_ns": ts_ns,
                "target": target.value,
                "size": str(self.c.size),
                "reason": reason,
                "risk_bps": None if risk is None else str(risk),
                "admitted": admission.admitted,
                "decision": admission.reason,
            }
        )
        if not admission.admitted:
            self.report.refused += 1
            self.report.refusals.append(f"{ts_ns}: {target.value}: {admission.reason}")
            if self.c.controls.tripped and self.report.stopped_by is None:
                self.report.stopped_by = self.c.controls.tripped
            return
        if target is Target.FLAT:
            if self.position is None:
                return
            direction = (
                Direction.SHORT if self.position.direction is Direction.LONG else Direction.LONG
            )
            self.pending_close = True
            self.c.venue.submit(
                Intent(ts_ns, self.c.feed.instrument, direction, self.position.size, reason)
            )
            return
        direction = Direction.LONG if target is Target.LONG else Direction.SHORT
        self.c.venue.submit(Intent(ts_ns, self.c.feed.instrument, direction, self.c.size, reason))

    # Round trips

    def close(self, position: Position, ts_ns: int, exit_px: Decimal, *, cause: str) -> None:
        sign = Decimal(position.direction.sign)
        gross_bps = sign * (exit_px - position.entry_px) / position.entry_px * BPS
        cost = self.c.venue.cost_of(
            position.direction, position.entry_ts_ns, ts_ns, self.c.spread_cost_bps
        )
        net_bps = gross_bps - cost.round_trip_bps
        self.trades.append(
            {
                "instrument": self.c.feed.instrument,
                "entry_ts_ns": position.entry_ts_ns,
                "exit_ts_ns": ts_ns,
                "direction": position.direction.value,
                "weight": "1",
                "gross_return_bps": str(gross_bps),
                "cost_bps": str(cost.round_trip_bps),
                "net_bps": str(net_bps),
                "cause": cause,
                "entry_px": str(position.entry_px),
                "exit_px": str(exit_px),
                "bars_held": position.bars_held,
            }
        )
        self.report.round_trips += 1
        self.report.net_bps_total += net_bps
        self.c.controls.record_pnl(ts_ns, net_bps)
        self.position = None

    def finish(self) -> RunReport:
        last = self.builder.flush()
        if last is not None:
            self.on_bar(last)
        if self.position is not None:
            p = self.position
            self.report.open_position = {
                "direction": p.direction.value,
                "entry_ts_ns": p.entry_ts_ns,
                "entry_px": str(p.entry_px),
                "size": str(p.size),
                "bars_held": p.bars_held,
                "last_price": None if self.last_price is None else str(self.last_price),
            }
        self.report.finished_at = datetime.now(UTC)
        summary = {
            "label": self.report.label,
            "instrument": self.report.instrument,
            "venue": self.c.venue.label,
            "strategy": self.c.strategy.name,
            "started_at": self.report.started_at.isoformat(),
            "finished_at": self.report.finished_at.isoformat(),
            "prints": self.report.prints,
            "bars": self.report.bars,
            "intents": self.report.intents,
            "refused": self.report.refused,
            "fills": self.report.fills,
            "round_trips": self.report.round_trips,
            "net_bps_total": str(self.report.net_bps_total),
            "stopped_by": self.report.stopped_by,
            "open_position": self.report.open_position,
        }
        (self.c.out_dir / "run.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return self.report


def run(config: TradingConfig) -> RunReport:
    loop = _Loop(config)
    for trade in config.feed:
        loop.on_print(trade)
        if (
            loop.report.stopped_by is not None
            and loop.position is None
            and loop.c.venue.pending is None
        ):
            break
    return loop.finish()
