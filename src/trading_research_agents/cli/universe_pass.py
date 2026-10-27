"""A pass over the universe: the instruments found, screened and ordered, and who holds what."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_research_agents.cli.common import (
    _cycle_ui,
    _watching,
)
from trading_research_agents.cli.venue_access import (
    _discovered_universe,
    _venue_hand,
)
from trading_research_agents.records import write_json


def screen_options_for(cycle_cfg: Any) -> dict[str, dict[str, Any]]:
    """What the pass pulls per instrument: data.toml's options, on the cycle's own bars.

    Read off data.toml's candle settings, the pass judged a 15-minute desk's market from four hours
    of one-minute bars, and the screener and the analyst saw two regimes for one instrument.
    """
    options = {kind: dict(opts) for kind, opts in cycle_cfg.data_options.items()}
    options.setdefault("ohlcv", {}).update(
        {"interval_s": cycle_cfg.interval_s, "limit": max(cycle_cfg.lookback_bars, 2)}
    )
    return options


def _sweep(
    cfg: Any,
    cycle_cfg: Any,
    args: argparse.Namespace,
    *,
    chains: Mapping[str, Any],
    out: Path,
    ask: Callable[[str], Any],
) -> tuple[tuple[str, ...], Any]:
    """Which instruments this pass runs a cycle on, and the screen that chose them.

    A desk that watches one instrument runs it, as it always did. A desk that declared a universe
    takes the cheap look first - one data pull each, the same trigger applied per instrument - and
    spends its cycles on what that admitted.
    """
    from trading_research_agents.agents.screen import asking, sweep
    from trading_research_agents.execution.trigger import Trigger
    from trading_research_agents.store.snapshot import cycle_id

    universe = cfg.universe
    if args.instrument or universe.idle:
        return (cycle_cfg.instrument,), None
    universe, discovered, discovery_said = _discovered_universe(cfg)
    now = cycle_cfg.clock()
    # --now overrides the trigger for the cycle, so it overrides it for the pass that feeds the
    # cycle too: an operator who asked anyway should not be told nothing was worth looking at.
    trigger = Trigger() if args.now else cfg.trigger
    screener = None
    if universe.screener:
        screener = asking(ask("market_screener"), mechanism=cycle_cfg.mechanism, at=cycle_id(now))

    open_now: set[str] = set()
    if cfg.execution.places_orders and not cfg.execution.adopt_open_position:
        reader = cycle_cfg.held
        if reader is not None:
            try:
                rows, _source = reader()
                open_now = {row.instrument for row in rows}
            except Exception:
                # The cycle reads the book again and refuses on its own if it cannot; the pass only
                # uses this to avoid asking the roster about something already open.
                open_now = set()

    cap = cfg.desk.max_open_positions
    full = (
        f"the desk holds {len(open_now)} position(s), its declared [desk].max_open_positions"
        if cap is not None and open_now and len(open_now) >= cap
        else ""
    )

    screen_options = screen_options_for(cycle_cfg)

    def look(watch: Any) -> Any:
        return sweep(
            universe,
            chains,
            trigger=trigger,
            root=out,
            now=now,
            max_round_trips_per_day=cfg.targets.max_round_trips_per_day,
            options=screen_options,
            ask=screener,
            desk=cfg.desk,
            held=cycle_cfg.held,
            watch=watch,
            open_now=open_now,
            full=full,
            listed={row["instrument"]: row for row in (discovered or {}).get("kept", [])},
            rule=cfg.regime,
            screens=out.parent / "screens",
        )

    if args.json or not _watching():
        screened = look(lambda _step: None)
    else:
        # A pass pulls one snapshot per instrument before any cycle starts; on a slow venue that
        # is seconds per name, and a blank terminal for that long reads as a hung process.
        steps = [*universe.instruments, *(["market_screener"] if universe.screener else [])]
        with _cycle_ui().CycleView(cycle_cfg.usage, steps, title="screen") as view:
            screened = look(view.on_step)
    screened.discovered = discovered
    screened.refusals[:0] = discovery_said
    where = out.parent / "screens"
    where.mkdir(parents=True, exist_ok=True)
    write_json(where / f"{screened.at}.json", screened.record())
    return screened.chosen, screened


def _screen_report(screened: Any) -> tuple[dict[str, Any], list[str]]:
    """One pass over the universe as a record and as the lines a person reads."""
    from trading_research_agents.execution.universe import why_no_cycle

    payload = screened.record()
    # Admitted by the system's own measures, before the screener narrowed them.
    admitted = sum(1 for c in screened.candidates if c.admitted)
    lines = [
        f"screen {screened.at}: {len(screened.candidates)} instrument(s) looked at, "
        f"{admitted} admitted, {len(screened.chosen)} taken"
    ]
    for candidate in screened.candidates:
        state = (
            "taken"
            if candidate.instrument in screened.chosen
            else ("admitted" if candidate.taken else "held")
        )
        lines.append(
            f"  {candidate.instrument:<12} {state:<9} "
            f"moved {candidate.moved_bps if candidate.moved_bps is not None else '-'} bps, "
            f"spread {candidate.spread_bps if candidate.spread_bps is not None else '-'} bps, "
            f"pressure {candidate.pressure if candidate.pressure is not None else '-'}"
        )
    for refusal in screened.refusals[:8]:
        lines.append(f"  held {refusal}")
    if not screened.chosen:
        lines.append(
            f"no cycle this pass: {why_no_cycle(payload.get('candidates') or [])}"
            + (
                "; only the screener was asked"
                if screened.asked_screener
                else "; no model was asked"
            )
        )
    return payload, lines


def _screen_view(payload: Mapping[str, Any]) -> Any:
    return _cycle_ui().screen_view(payload)


def _keeper(
    args: argparse.Namespace, cfg: Any, project: Path, out: Path, instrument: str, rate: Any
) -> Any:
    """What looks after positions between decisions: time exits, venue exits, the kill switch."""
    from trading_research_agents.trading.positions import Keeper

    hands: dict[str, Any] = {}

    def hand_for(name: str) -> Any:
        if name not in hands:
            hands[name] = _venue_hand(args, cfg, project, name, rate)
        return hands[name]

    return Keeper(
        root=out,
        hand_for=hand_for,
        instruments=tuple(cfg.universe.instruments) or (instrument,),
        bar_interval_s=cfg.run.bar_interval_s,
        # Discovered instruments come and go from the list; a position on one is looked after
        # for as long as the records say it was entered.
        follow_records=True,
        timeout_bars=cfg.exit_rule.timeout_bars,
        hold_limit=cfg.execution.hold_limit,
    )


def _held_reader(cfg: Any, out: Path, mode: str, args: argparse.Namespace, project: Path) -> Any:
    """Where a cycle looks to find out what this desk is already carrying.

    In venue mode the venue is the book and nothing else is: it knows what filled, and what the
    attached exits closed while nobody was watching. Everywhere else the intents on disk are all
    there is, and the verdict says so rather than implying a venue was asked.
    """
    from trading_research_agents.execution.desk import Open, filled_from_records, from_records

    if cfg.desk.idle:
        return None
    if mode != "venue":
        return lambda: from_records(out, at=datetime.now(UTC))

    def ask_the_venue() -> tuple[tuple[Open, ...], str]:
        ours = filled_from_records(out)
        declared = cfg.universe.instruments or (args.instrument or cfg.run.instrument,)
        # An instrument discovery once took, and that this desk filled, is still the desk's to
        # count even when this pass did not list it.
        names = tuple(dict.fromkeys([*declared, *ours]))
        held: list[Open] = []
        for name in names:
            size = _venue_hand(args, cfg, project, name).position()
            if not size:
                continue
            # The venue says the position is there; this desk's own intent says what it has at
            # risk, which the venue has no way to know.
            mine = ours.get(name)
            side = "buy" if size > 0 else "sell"
            if mine is not None and mine.side and mine.side != side:
                # The venue holds the other side from what this desk filled last: not ours to state.
                mine = None
            held.append(
                Open(
                    instrument=name,
                    side=side,
                    risk_bps=None if mine is None else mine.risk_bps,
                    notional=None if mine is None else mine.notional,
                )
            )
        return tuple(held), "the venue, with what each one risks from this desk's own intents"

    return ask_the_venue
