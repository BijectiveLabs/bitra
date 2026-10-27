"""What tra check says about the load a desk puts on its providers, its capital and its spend."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def _span_s(seconds: float) -> str:
    whole = int(seconds)
    for size, unit in ((86_400, "d"), (3_600, "h"), (60, "m")):
        if whole and whole % size == 0:
            return f"{whole // size}{unit}"
    return f"{whole}s"


def _load_notes(a: Any, roster: tuple[str, ...], screener: bool = False) -> list[str]:
    """How a cycle's calls land on providers, and what happens when one of them says no."""
    # The readings are read by code; the analyst and its team are the models a cycle asks.
    asked = ["technical_analyst", *a.team]
    reading = [name for name in (a.readings or roster) if name != "technical_analyst"]
    roles = [
        *asked,
        *(["validator"] if a.validators else []),
        *(["market_screener"] if screener else []),
    ]
    calls = len(asked) + len(a.validators) + (1 if screener else 0)
    on = sorted({a.for_role(role).provider for role in roles})
    said = [
        f"models: a full pass makes at most {calls} call(s) within a minute"
        + (" (the screener, then the cycle)" if screener else "")
        + f", on {', '.join(on)}; a provider that says wait up to {a.max_retry_wait_s:.0f}s is "
        "waited for once, a provider whose quota is spent rests 15 minutes, then the next link "
        "is tried"
    ]
    if reading:
        said.append(
            f"readers: {', '.join(reading)} are read by code against your thresholds and ask no "
            "model"
        )
    if "technical_peer" in a.team:
        lead, peer = a.for_role("technical_analyst"), a.for_role("technical_peer")
        if (lead.provider, lead.model) == (peer.provider, peer.model):
            said.append(
                "team: technical_peer runs on the same model as the technical analyst, and two "
                "reads from one model err together; give it another family in "
                "[agents.roles.technical_peer]"
            )
    # Vendors meter per model, so two models on one provider are two sets of limits.
    pairs = sorted({f"{a.for_role(r).provider}:{a.for_role(r).model}" for r in roles})
    if len(pairs) == 1 and calls >= 4:
        said.append(
            f"models: every role is on {pairs[0]}; if your tier limits tokens per minute or per "
            "day, give the table-reading roles another model or provider in [agents.roles.<role>]"
        )
    elif len(on) == 1:
        said.append(
            f"models: the roles share {on[0]} across {', '.join(pairs)}, each model on "
            "its own limits"
        )
    if a.validators:
        said.append(
            "models: a declared validator that no model answers "
            + (
                "refuses the trade"
                if a.on_missing_validator == "refuse"
                else "is left out and the trade goes on"
            )
            + " ([agents].on_missing_validator)"
        )
    return said


def _capital_notes(cfg: Any) -> list[str]:
    """What venue mode does with capital, said in the order it happens."""
    mandate = cfg.execution
    hold = {
        "timeout": f"after [exit].timeout_bars ({cfg.exit_rule.timeout_bars} bars)",
        "horizon": "after the analyst's own horizon, capped by [exit].timeout_bars",
        "none": "never by time; only its stop or take profit closes it",
    }[mandate.hold_limit]
    said = [
        f"capital: {mandate.leverage}x {mandate.margin_mode}, applied at the venue before the "
        "first order and read back; an order whose estimated liquidation sits inside the stop "
        f"plus {mandate.liquidation_buffer_bps} bps is refused",
        "capital: sizing uses the smaller of the account's equity and [run].equity; an order "
        f"may use at most {mandate.max_margin_share} of the free margin",
        f"capital: no new entry once equity is down {cfg.limits.daily_loss_limit_bps} bps on the "
        f"day; at most {cfg.limits.max_orders_per_minute} orders a minute",
        _drawdown_line(cfg.limits),
        f"orders: a taker entry is a limit IOC at most {mandate.max_slippage_bps} bps through the "
        "touch, never a naked market order",
        f"orders: a decision older than {mandate.max_decision_age_s:.0f}s is not sent; the book is "
        "read again just before sending"
        + (
            f", and an order is refused when the mid moved {mandate.max_drift_bps} bps against it"
            if mandate.max_drift_bps is not None
            else ""
        )
        + "; each order records its age, the venue's answer time and its slippage",
        f"positions: an open position is closed with a reduce-only order {hold}; the kill switch "
        "flattens every position before the run stops",
    ]
    if mandate.hold_limit == "none":
        said.append(
            "positions: hold_limit is none, so a position the market never takes to its stop or "
            "target stays open, and holds back new entries on correlated instruments"
        )
    return said


def _size_against_cap(cfg: Any, cap: Decimal, good: list[str], bad: list[str]) -> None:
    """Two declarations that only meet at the last gate, after a cycle has been paid for.

    Where they stop agreeing is arithmetic, so it is said here instead of at the venue.
    """
    run = cfg.run
    top = max((rung.size_multiple for rung in cfg.conviction.ladder), default=Decimal(1))
    if run.size_quote is None:
        if run.size > 0:
            ceiling = (cap / run.size).quantize(Decimal("0.01"))
            good.append(
                f"execution: {run.size} at a price above {ceiling} is more than {cap} of "
                "notional, and every order would be refused at the venue gate; that is the size "
                "and the cap talking to each other"
            )
        return
    if run.size_quote > cap:
        bad.append(
            f"[run].size_quote {run.size_quote} is above [execution].max_notional {cap}, so every "
            "order would be refused at the venue gate after the cycle had been paid for"
        )
    elif run.size_quote * top > cap:
        good.append(
            f"execution: the top rung of the ladder asks for {run.size_quote * top} of notional "
            f"and the cap is {cap}; orders at that rung are refused at the venue gate"
        )
    else:
        good.append(
            f"execution: at most {run.size_quote * top} of notional per order on any instrument, "
            f"inside the cap of {cap}"
        )


def _desk_notes(cfg: Any, bad: list[str]) -> list[str]:
    """What the whole book may carry at once, and whether that can actually be measured."""
    desk = cfg.desk
    if desk.idle:
        return [
            "desk: not declared, so nothing bounds the book as a whole; [desk].max_open_risk_bps "
            "is the sum of what every open position loses at its own stop"
        ]
    said = []
    if desk.max_open_risk_bps is not None:
        said.append(f"at most {desk.max_open_risk_bps} bps of equity at risk across the book")
    if desk.max_open_positions is not None:
        said.append(f"at most {desk.max_open_positions} position(s) open at once")
    if desk.correlation_above is not None:
        said.append(
            f"a candidate moving with an open position at {desk.correlation_above} or above is "
            "refused by the screen"
        )
    if desk.max_open_risk_bps is not None and cfg.limits.max_risk_per_trade_bps is None:
        bad.append(
            "[desk].max_open_risk_bps sums what each position risks, and "
            "[controls].max_risk_per_trade_bps is what puts that figure on a trade; declare it, or "
            "the sum has nothing to add up"
        )
    if desk.correlation_above is not None and cfg.universe.idle:
        bad.append(
            "[desk].correlation_above compares one instrument with another and [universe] names "
            "only one; declare the instruments this desk watches or drop the threshold"
        )
    return ["desk: " + "; ".join(said)]


def _universe_notes(cfg: Any, bad: list[str]) -> list[str]:
    """What this desk watches, and what a pass over it will do."""
    universe = cfg.universe
    if universe.idle:
        return [
            f"universe: not declared, so every cycle is {cfg.run.instrument} from [run]; "
            "[universe].instruments is where a desk names what else it watches"
        ]
    named = ", ".join(universe.instruments) or "nothing declared"
    said = [
        f"universe: {named}"
        + (f" always, plus up to {universe.top} discovered" if universe.discover else "")
        + f"; a pass looks at each for the price of a data pull and runs {universe.max_per_cycle} "
        "of them"
    ]
    if universe.discover:
        said.append(
            f"universe: discovery adds the {universe.top} most traded {universe.quote} "
            f"perpetuals each pass (at least {universe.min_quote_volume:,} a day, spread at most "
            f"{universe.max_spread_bps} bps, listed {universe.min_listed_days}+ days"
            + (", crypto only" if universe.crypto_only else "")
            + "); `tra universe` shows them now"
        )
    if universe.max_intents_per_day is not None:
        said.append(
            f"universe: at most {universe.max_intents_per_day} intent(s) a day across every "
            "instrument, counted from what is on disk"
        )
    if universe.screener:
        said.append(
            "universe: the screener picks and orders what the system admitted, citing each "
            "instrument's own figures; it cannot add an instrument, and the system's order "
            "is kept beside its own"
        )
    if cfg.trigger.idle:
        bad.append(
            "[universe] is declared and [trigger] is not, so every pass would ask the whole roster "
            "about every instrument; [trigger].min_move_bps is what makes a pass cheap"
        )
    return said


def _audit_notes(cfg: Any) -> list[str]:
    """When the auditors are asked without anyone typing the command."""
    policy = cfg.audit
    if policy.idle:
        return [
            "auditors: asked only by hand (`tra audit`); a provider that stops answering halfway "
            "through a run will not be looked at until you look"
        ]
    said = []
    if policy.on_alarm:
        said.append(
            f"the technical auditor wakes on an alarm, at most once per kind every "
            f"{policy.alarm_cooldown_s}s"
        )
    if policy.on_alarm:
        streak = (
            f" or after {policy.losing_streak} losing round trips in a row"
            if policy.losing_streak
            else ""
        )
        said.append(f"the quant auditor wakes when a loss limit is reached{streak}")
    if policy.quant_every_cycles is not None:
        said.append(f"the quant auditor runs every {policy.quant_every_cycles} cycles")
    if policy.quant_every_s is not None:
        said.append(f"the quant auditor runs every {_span_s(policy.quant_every_s)}")
    if policy.risk_manager:
        said.append(
            "the risk manager is asked with the quant auditor and may only cut size, pause an "
            "instrument or halt, each for a stated number of hours"
        )
    return ["auditors: " + "; ".join(said) + "; the auditors report and never act"]


def _spend_notes(cfg: Any) -> list[str]:
    """What the models may spend, what they are asked to spend it on, and who is counting."""
    a = cfg.agents
    if a is None:
        return []
    out: list[str] = []
    ceilings = []
    if a.token_budget is not None:
        ceilings.append(f"{a.token_budget} tokens for the whole process")
    if a.max_calls_per_hour is not None:
        ceilings.append(f"{a.max_calls_per_hour} calls an hour per role")
    if a.max_output_tokens is not None:
        ceilings.append(f"{a.max_output_tokens} output tokens a call")
    if ceilings:
        out.append(
            "spend: capped at " + "; ".join(ceilings) + "; a cycle that reaches a ceiling stops"
        )
    else:
        out.append(
            "spend: nothing is capped; every call is counted in usage.jsonl, each cycle reports "
            "what it cost, and the auditors read those records"
        )
    if a.effort is None:
        out.append(
            "effort: not declared, so each vendor's own default applies; [agents].effort sets it "
            "for every role and [agents.roles.<role>].effort overrides one"
        )
    else:
        roles = (
            "technical_analyst",
            "validator",
            "market_screener",
        )
        levels: dict[str, list[str]] = {}
        for role in roles:
            levels.setdefault(str(a.for_role(role).effort), []).append(role)
        out.append(
            "effort: "
            + "; ".join(f"{level} for {', '.join(who)}" for level, who in sorted(levels.items()))
        )
    quoted = sorted(m for m, (i, o) in a.pricing.items() if i > 0 or o > 0)
    free = sorted(m for m, (i, o) in a.pricing.items() if i == 0 and o == 0)
    used = {
        a.for_role(role).model or "default"
        for role in ("technical_analyst", "validator", "market_screener")
    }
    used |= {name.partition(":")[2] for name in a.fallback}
    unpriced = sorted(m for m in used if m not in a.pricing and m != "default")
    parts = []
    if quoted:
        parts.append(f"declared for {', '.join(quoted)}")
    if free:
        parts.append(f"declared free for {', '.join(free)}")
    if unpriced:
        parts.append(f"not declared for {', '.join(unpriced)}, so its calls are counted in tokens")
    out.append(
        "prices: "
        + ("; ".join(parts) if parts else "none quoted, so spend is counted in tokens only")
        + "; [agents.pricing.<model>] is where a quote goes"
    )
    return out


def _drawdown_line(limits: Any) -> str:
    if limits.max_drawdown_bps is None:
        return (
            "capital: no drawdown limit across days; [controls].max_drawdown_bps stops new entries "
            "once equity falls that far from its peak"
        )
    then = (
        "and the kill switch flattens and stops the run"
        if limits.on_drawdown == "flatten"
        else "until runs/cycles/equity_peak.json is cleared; open positions keep their exits"
    )
    return (
        f"capital: no new entry once equity is {limits.max_drawdown_bps} bps below its peak, "
        + then
    )
