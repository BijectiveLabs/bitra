from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from trading_research_agents.risk import (
    RiskLimits,
    RiskState,
    approval_problem,
    evaluate,
)

NOW = 1_800_000_000.0

LONG: dict[str, Any] = {
    "instrument": "BTCUSDT",
    "target": "long",
    "size": "0.01",
    "mid": "100000",
    "stop_price": "99000",
    "take_price": "102000",
    "priced_round_trip_bps": "10",
    "at": "2026-10-27T09-00-00Z",
}

CLEAR = RiskState(equity=Decimal(10_000), trades_today=0)


def reasons(intent: dict[str, Any], limits: RiskLimits, state: RiskState = CLEAR) -> list[str]:
    decision = evaluate(intent, limits=limits, state=state, now=NOW)
    assert decision.approved == (not decision.reasons)
    return list(decision.reasons)


def test_an_intent_inside_every_limit_is_approved_and_carries_a_bound_approval() -> None:
    decision = evaluate(LONG, limits=RiskLimits(), state=CLEAR, now=NOW)
    assert decision.approved and decision.approval is not None
    sent = decision.approved_intent(LONG)
    assert approval_problem(sent, now=NOW + 1) == ""


def test_an_approval_is_void_for_any_other_intent_after_it_expires_or_without_it() -> None:
    sent = evaluate(LONG, limits=RiskLimits(valid_for_s=60), state=CLEAR, now=NOW).approved_intent(
        LONG
    )
    assert "no approval" in approval_problem(LONG, now=NOW)
    assert "does not match" in approval_problem({**sent, "size": "0.02"}, now=NOW)
    assert "does not match" in approval_problem({**sent, "target": "short"}, now=NOW)
    assert "expired" in approval_problem(sent, now=NOW + 61)
    forged = {**sent, "risk_gate": {**sent["risk_gate"], "signature": "0" * 64}}
    assert "does not match" in approval_problem(forged, now=NOW)


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"target": "flat"}, "not an order"),
        ({"size": "0"}, "size"),
        ({"stop_price": "101000"}, "stop"),
        ({"take_price": "99500"}, "take profit"),
    ],
)
def test_an_intent_that_is_not_a_well_formed_order_is_refused(
    change: dict[str, Any], said: str
) -> None:
    assert any(said in r for r in reasons({**LONG, **change}, RiskLimits()))


def test_the_hard_caps_hold_whatever_the_cycle_sized() -> None:
    assert any("max_notional" in r for r in reasons(LONG, RiskLimits(max_notional=Decimal(500))))
    assert any(
        "max_position " in r for r in reasons(LONG, RiskLimits(max_position=Decimal("0.005")))
    )
    capped = RiskLimits(max_position_quote=Decimal(900))
    assert any("max_position_quote" in r for r in reasons(LONG, capped))


def test_the_loss_at_the_stop_is_recomputed_from_the_intent_itself() -> None:
    # 0.01 x (1000 + 100000 x 10 bps) = 11 of a 10 000 equity: 11 bps at the stop.
    assert reasons(LONG, RiskLimits(max_risk_per_trade_bps=Decimal(11))) == []
    assert any(
        "bps of equity" in r for r in reasons(LONG, RiskLimits(max_risk_per_trade_bps=Decimal(10)))
    )
    no_stop = {k: v for k, v in LONG.items() if k != "stop_price"}
    assert any(
        "no stop" in r for r in reasons(no_stop, RiskLimits(max_risk_per_trade_bps=Decimal(50)))
    )


def test_a_limit_whose_state_is_missing_fails_closed() -> None:
    blind = RiskState(equity=None, trades_today=0, places_orders=True)
    assert any(
        "equity" in r for r in reasons(LONG, RiskLimits(max_risk_per_trade_bps=Decimal(50)), blind)
    )
    assert any(
        "day loss" in r for r in reasons(LONG, RiskLimits(daily_loss_limit_bps=Decimal(100)))
    )
    assert any("drawdown" in r for r in reasons(LONG, RiskLimits(max_drawdown_bps=Decimal(100))))


def test_the_desk_state_holds_new_entries() -> None:
    state = RiskState(
        equity=Decimal(10_000),
        trades_today=3,
        day_loss_bps=Decimal(150),
        drawdown_bps=Decimal(40),
        held_by="the kill switch at runs/KILL",
        unknown_orders="order tra1 on BTCUSDT is UNKNOWN",
    )
    limits = RiskLimits(
        max_trades_per_day=3, daily_loss_limit_bps=Decimal(100), max_drawdown_bps=Decimal(500)
    )
    said = reasons(LONG, limits, state)
    assert any("kill switch" in r for r in said)
    assert any("UNKNOWN" in r for r in said)
    assert any("max_trades_per_day" in r for r in said)
    assert any("daily_loss_limit_bps" in r for r in said)
    assert not any("max_drawdown_bps" in r for r in said)


def test_a_gate_that_cannot_evaluate_refuses() -> None:
    decision = evaluate({**LONG, "size": "a lot"}, limits=RiskLimits(), state=CLEAR, now=NOW)
    assert not decision.approved and decision.reasons[0].startswith("the risk gate could not")
