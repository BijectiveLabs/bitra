"""What tra check says about the gates a desk declared: each one, and what it means for a trade."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trading_research_agents.cli.check_load import (
    _audit_notes,
    _capital_notes,
    _desk_notes,
    _load_notes,
    _size_against_cap,
    _spend_notes,
    _universe_notes,
)
from trading_research_agents.cli.common import (
    _declared_environment,
)
from trading_research_agents.cli.fees import _bps
from trading_research_agents.llm.chain import parse_link
from trading_research_agents.llm.providers import (
    providers,
)
from trading_research_agents.trading.config import RunSettings


def _gate_lines(
    cfg: Any,
    roster: tuple[str, ...],
    *,
    project: Path,
    tier: str,
    env: Mapping[str, str],
    entry_role: str = "undeclared",
) -> tuple[list[str], list[str]]:
    """What the two declared gates will do, and where they contradict the rest of the project.

    A gate that can never pass is worth saying out loud here, before a cycle spends a token finding
    out: a required role nobody asks, or a vote count nobody can cast.
    """
    good: list[str] = []
    bad: list[str] = []
    matrix = cfg.confluence
    stances = () if cfg.agents is None else cfg.agents.validators
    if stances:
        from trading_research_agents.agents.cycle import judge_mode

        weight = cfg.conviction.validator_weight
        how = (
            "; the advocates build their cases apart and neutral judges them, its verdict the one "
            "that counts; with a case on one side only, code decides without asking it"
            if judge_mode(stances)
            else ""
        )
        good.append(
            f"validators: {', '.join(stances)}; asked once there is a proposal and the system "
            "has not already refused it"
            + how
            + (f"; each counted vote moves conviction by {weight}" if weight > 0 else "")
        )
    if matrix.idle:
        good.append("confluence: not declared; the context is recorded but gates nothing")
    else:
        good.append(
            f"confluence: needs {', '.join(matrix.required) or 'no role'}; "
            f"long {list(matrix.long_states)}, short {list(matrix.short_states)}; "
            f"a missing state means {matrix.on_missing}"
        )
        absent = sorted(set(matrix.required) - set(roster))
        if absent:
            bad.append(
                f"[confluence].required names {absent}, which the roster does not read; "
                "declare their sources or the cycle refuses every time"
            )
        if matrix.min_support > len(stances):
            bad.append(
                f"[confluence].min_support is {matrix.min_support} but "
                f"{len(stances)} validator(s) are asked; no cycle can clear it"
            )
    from trading_research_agents.execution.mandate import blocking

    mandate = cfg.execution
    if entry_role in ("maker", "taker"):
        crosses = "crosses the book at once" if entry_role == "taker" else "rests on the book"
        good.append(
            f"channel: {cfg.run.venue_key} enters as a {entry_role}, priced at that fee; "
            f"an order {crosses}"
        )
    if not mandate.places_orders:
        good.append(
            f"execution: {mandate.mode}; nothing leaves the machine"
            if mandate.mode == "sandbox"
            else "execution: signals; intents are written for your own executor"
        )
    else:
        from trading_research_agents.trading.dispatch import credentials_for, key_hint

        keys = tuple(credentials_for(mandate.venue).values())
        stops = blocking(
            mandate,
            project=project,
            tier=tier,
            missing_keys=[k for k in keys if not env.get(k)],
            practice=_declared_environment(cfg) in ("demo", "testnet"),
        )
        if stops:
            bad.extend(f"execution: {stop}" for stop in stops)
            hint = key_hint([k for k in keys if not env.get(k)], keys, env)
            if hint:
                bad.append(f"execution: {hint}")
        else:
            good.append(
                f"execution: venue {mandate.venue}, signed by {mandate.acknowledged_by} on "
                f"{mandate.acknowledged_on}, at most {mandate.max_notional} per order"
            )
            # Two declarations that only meet at the last gate, after a cycle has been paid for.
            # The price where they stop agreeing is arithmetic, and it can be said here instead.
            if mandate.max_notional is not None:
                _size_against_cap(cfg, mandate.max_notional, good, bad)
            good.extend(_capital_notes(cfg))
            if mandate.exits == "attached":
                good.append(
                    "execution: the stop and the take profit go to the venue with the entry, so a "
                    "fill is never left unattended"
                )
            elif mandate.exits == "separate":
                good.append(
                    "execution: the stop and the take profit go to the venue as reduce-only orders "
                    "right after the fill; a fill they cannot be placed for is closed at once"
                )
            else:
                bad.append(
                    '[execution].exits is "none": a fill would sit at the venue with no stop and '
                    "no take profit, and only you would be carrying them"
                )
            if entry_role == "maker":
                good.append(
                    "execution: an order that has not filled is cancelled after "
                    f"{mandate.limit_timeout_s}s; nothing is chased across the spread"
                )
            elif entry_role == "undeclared":
                bad.append(
                    f"channel {cfg.run.venue_key!r} does not say whether entry is maker or taker; "
                    "the fee you priced depends on it"
                )
    if cfg.agents is not None and cfg.agents.fallback:
        unknown = []
        for name in cfg.agents.fallback:
            named, _ = parse_link(name)
            if named not in providers():
                unknown.append(name)
        if unknown:
            bad.append(f"[agents].fallback names {unknown}, which no provider is registered for")
        else:
            good.append(
                f"models: fallback {', '.join(cfg.agents.fallback)}; "
                "tried in order when a provider cannot be reached"
            )
    if cfg.agents is not None:
        good.extend(_load_notes(cfg.agents, roster, screener=cfg.universe.screener))
    good.append(_regime_note(cfg))
    rule = cfg.conviction
    if rule.idle:
        good.append("conviction: not declared; every admitted cycle trades the base size")
    else:
        rungs = ", ".join(f"{t.at_least}+ -> {t.size_multiple}x" for t in rule.ladder)
        good.append(
            f"conviction: floor {rule.min_score}; ladder {rungs or 'none'}"
            + (
                f"; notional at most {rule.max_leverage}x the equity (a cap on exposure, not the "
                "venue's leverage, which is [execution].leverage)"
                if rule.max_leverage
                else ""
            )
        )
        good.append(
            "conviction: every voice counts as strong as it is - the deciding analyst by its "
            "confidence, a reading by how far its series sit past your lines - and the "
            "score is their net over what they expressed; opposition shrinks the size, and only "
            "opposition that outweighs the case refuses the trade"
        )
        if not rule.ladder:
            bad.append(
                "[conviction] has no ladder, so no score can size a trade; "
                "declare [[conviction.ladder]] rungs or drop the table"
            )
        above = [t for t in rule.ladder if t.size_multiple > 1]
        if above:
            good.append(
                f"conviction: {len(above)} rung(s) above 1x let agreement add size beyond the "
                "base; the risk per trade still caps the loss at the stop, and `tra hindsight` "
                "says whether the score has separated good trades from bad before you rely on it"
            )
        unweighted = sorted(set(roster) - set(rule.weights))
        if unweighted:
            good.append(
                f"conviction: {unweighted} is on the roster but carries no weight; "
                "it explains without moving the size"
            )
    asked = cfg.agents.validators if cfg.agents is not None else ()
    gated = cfg.confluence.min_support > 0 or cfg.confluence.max_opposition is not None
    if asked and not gated and rule.validator_weight <= 0:
        bad.append(
            f"validators: {', '.join(asked)} are asked every cycle and their votes move nothing; "
            "give them [conviction].validator_weight, or [confluence].min_support / "
            "max_opposition, or stop asking them"
        )
    targets = cfg.targets
    if targets.idle:
        good.append(
            "targets: not declared; only the cost gate stands between a claim and an intent"
        )
    else:
        said = []
        if targets.min_net_bps is not None:
            said.append(f"at least {targets.min_net_bps} bps net")
        if targets.min_edge_multiple is not None:
            said.append(f"at least {targets.min_edge_multiple}x the round trip")
        if targets.min_reward_risk is not None:
            said.append(f"reward-to-risk {targets.min_reward_risk}")
        if targets.max_research_bps is not None:
            said.append(f"at most {targets.max_research_bps} bps of notional in model calls")
        if targets.max_round_trips_per_day is not None:
            said.append(f"at most {targets.max_round_trips_per_day} round trips a day")
        good.append("targets: " + "; ".join(said))
    if targets.min_edge_multiple is None:
        good.append(
            "targets: no edge multiple declared, so a trade worth two basis points more than it "
            "costs can still pass; [targets].min_edge_multiple is what stops that"
        )
    priced = cfg.agents is not None and any(i > 0 or o > 0 for i, o in cfg.agents.pricing.values())
    if targets.max_research_bps is not None and not priced:
        bad.append(
            "[targets].max_research_bps is declared and no model price is, so the research cost "
            "reads as zero whatever the models spend; put what you were quoted in "
            "[agents.pricing.<model>] or drop the target"
        )
    good.extend(_exit_notes(cfg, bad))
    good.extend(_risk_notes(cfg, bad))
    good.extend(_trigger_notes(cfg))
    good.extend(_spend_notes(cfg))
    good.extend(_audit_notes(cfg))
    good.extend(_universe_notes(cfg, bad))
    good.extend(_size_notes(cfg))
    good.extend(_desk_notes(cfg, bad))
    good.append(_limit_line(cfg))
    good.append(
        "keys: this check reads which are set, not whether they work; `tra keys --probe` asks each "
        "provider and the venue itself, sends no prompt and places no order"
    )
    return good, bad


def _limit_line(cfg: Any) -> str:
    """Every daily line and switch the desk declared, in one sentence."""
    said = [f"daily loss {cfg.limits.daily_loss_limit_bps} bps"]
    if cfg.limits.max_drawdown_bps is not None:
        said.append(f"drawdown {cfg.limits.max_drawdown_bps} bps")
    if cfg.limits.max_trades_per_day is not None:
        said.append(f"{cfg.limits.max_trades_per_day} trades a day")
    if cfg.limits.max_losing_streak is not None:
        said.append(f"{cfg.limits.max_losing_streak} losers in a row")
    a = cfg.agents
    if a is not None and a.daily_token_budget is not None:
        said.append(f"{a.daily_token_budget} tokens a day")
    if a is not None and a.daily_cost_budget is not None:
        said.append(f"{a.daily_cost_budget} a day in model cost")
    switches = [f"`tra pause` ({cfg.pause_switch})"] if cfg.pause_switch is not None else []
    if cfg.kill_switch is not None:
        switches.append(f"`tra kill` ({cfg.kill_switch})")
    return "limits: " + ", ".join(said) + "; switches: " + (", ".join(switches) or "none")


def _one_sided(path: Path) -> list[str]:
    """Series on a shelf file whose threshold names only bullish or only bearish lines."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    found = []
    for entry in raw.get("series") or []:
        threshold = entry.get("threshold") if isinstance(entry, dict) else None
        if not isinstance(threshold, dict) or not threshold:
            continue
        bull = any(str(k).startswith("bullish_") for k in threshold)
        bear = any(str(k).startswith("bearish_") for k in threshold)
        if bull != bear:
            found.append(str(entry.get("name", "?")))
    return found


def _size_notes(cfg: Any) -> list[str]:
    """What one unit of base size means on each instrument this desk can trade."""
    run = cfg.run
    stated = run.size_step == RunSettings.__dataclass_fields__["size_step"].default
    if run.size_steps:
        steps = "the step " + ", ".join(f"{name} {step}" for name, step in run.size_steps.items())
    elif stated:
        steps = "the step the venue states for each instrument, in the environment orders go to"
    else:
        steps = f"the step {run.size_step}"
    if run.size_quote is not None:
        found = (
            "; discovered instruments use the venue's own step"
            if cfg.universe.discover and not stated
            else ""
        )
        said = [
            f"size: {run.size_quote} of quote currency, converted at each cycle's own mid and "
            f"rounded down to {steps}{found}; a replay still needs [run].size",
        ]
        cap = cfg.limits.max_position_quote
        if cap is not None:
            said.append(
                f"size: every position is capped at {cap} of quote currency at its own mid; "
                f"[controls].max_position {cfg.limits.max_position} units applies to "
                f"{run.instrument} as well"
            )
        else:
            said.append(
                f"size: [controls].max_position {cfg.limits.max_position} is in base units, so it "
                "caps each instrument at that many units whatever they are worth; "
                "[controls].max_position_quote says it in quote currency"
            )
        return said
    said = [f"size: {run.size} in base units, at {steps}"]
    if not cfg.universe.idle:
        said.append(
            f"size: {run.size} is a different amount of money on each of "
            f"{', '.join(cfg.universe.instruments)}; [run].size_quote says it once, in quote "
            "currency"
        )
    return said


def _regime_note(cfg: Any) -> str:
    from trading_research_agents.agents.briefs import interval_name

    rule = cfg.regime
    frames = ", ".join(interval_name(s) for s in cfg.run.context_intervals)
    return (
        f"regime: measured on {interval_name(cfg.run.bar_interval_s)}"
        + (f" with {frames} as context" if frames else ", no higher timeframe declared")
        + f"; trending at {rule.trend_above}+, ranging at {rule.range_below} or less; "
        + (
            "a setup that does not fit is refused"
            if rule.setups_must_fit
            else "a setup that does not fit is noted and allowed"
        )
    )


def _exit_notes(cfg: Any, bad: list[str]) -> list[str]:
    """Where each trade's stop and take profit will sit, in words."""
    rule = cfg.exit_rule
    scale = rule.volatility
    if scale is None:
        take = (
            "no take profit"
            if rule.take_profit_frac is None
            else (f"take profit {_bps(rule.take_profit_frac * 10_000)} bps")
        )
        return [
            f"exits: fixed, stop {_bps(rule.stop_frac * 10_000)} bps and {take} on every trade; "
            '[exit].mode = "volatility" sizes them to the range of each market'
        ]
    agents = getattr(cfg, "agents", None)
    lookback = getattr(agents, "lookback_bars", None)
    if lookback is not None and lookback < scale.atr_bars + 1:
        bad.append(
            f"[exit].atr_bars is {scale.atr_bars} and [agents].lookback_bars is {lookback}; the "
            f"average true range needs {scale.atr_bars + 1} closed candles"
        )
    return [
        (
            f"exits: the analyst names its stop and target; the stop is held between "
            f"{_bps(scale.min_stop_frac * 10_000)} and {_bps(scale.max_stop_frac * 10_000)} bps "
            f"and never inside {scale.min_stop_atr} ATR, the target between "
            f"{scale.min_reward_to_risk}x and {scale.max_reward_to_risk}x the "
            f"stop; with no level named, {scale.stop_atr} x ATR({scale.atr_bars}) and "
            f"{scale.reward_to_risk}x"
        )
        if scale.analyst_levels
        else (
            f"exits: measured per trade, stop {scale.stop_atr} x ATR({scale.atr_bars}) grown with "
            f"the horizon, held between {_bps(scale.min_stop_frac * 10_000)} and "
            f"{_bps(scale.max_stop_frac * 10_000)} bps; take profit "
            f"{scale.reward_to_risk} x the stop"
        )
    ]


def _risk_notes(cfg: Any, bad: list[str]) -> list[str]:
    """Whether the risk per trade the operator declared can actually be applied to a size."""
    allowed = cfg.limits.max_risk_per_trade_bps
    if allowed is None:
        return [
            "risk per trade: not declared; the conviction ladder and [controls].max_position are "
            "all that bound a size"
        ]
    if cfg.exit_rule.stop_frac is None:
        bad.append(
            f"[controls].max_risk_per_trade_bps is {allowed} and no stop is declared, so the loss "
            "at the stop cannot be computed; declare [exit].stop_frac"
        )
        return []
    if cfg.run.equity is None:
        bad.append(
            f"[controls].max_risk_per_trade_bps is {allowed} and [run].equity is not set, so there "
            "is nothing to measure the loss against"
        )
        return []
    return [
        f"risk per trade: {allowed} bps of equity at the stop, applied to every size the ladder "
        "produces and cut to fit rather than sent"
    ]


def _trigger_notes(cfg: Any) -> list[str]:
    """What a quiet market costs this desk, which is the whole point of the trigger."""
    trigger = cfg.trigger
    if trigger.idle:
        return [
            "trigger: not declared, so every scheduled cycle pays the whole roster whether or not "
            "anything moved; [trigger].min_move_bps is what makes a quiet hour cheap"
        ]
    said = []
    if trigger.min_move_bps is not None:
        said.append(f"{trigger.min_move_bps} bps of movement since the last look")
    if trigger.max_spread_bps is not None:
        said.append(f"a book no wider than {trigger.max_spread_bps} bps")
    if trigger.cooldown_s is not None:
        said.append(f"{trigger.cooldown_s}s since the last intent")
    if trigger.widen_after is not None:
        said.append(f"more movement after {trigger.widen_after} straight refusals")
    if trigger.min_move_bps is not None and trigger.look_valid_s is not None:
        said.append(f"a last look older than {trigger.look_valid_s}s counts as a first look")
    return ["trigger: asks the roster only on " + "; ".join(said)]
