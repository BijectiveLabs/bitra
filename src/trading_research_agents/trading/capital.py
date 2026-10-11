"""What the account can carry: equity, free margin, the margin an order ties up, and liquidation.

Everything here reads through ccxt's unified calls, so it works on any venue that answers them
and refuses by name on one that does not. Nothing here sends an order.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.records import append_line, write_json

BPS = Decimal(10_000)
EQUITY_LOG = "equity.jsonl"
EQUITY_PEAK = "equity_peak.json"


class CapitalError(ValueError):
    """The account cannot be read, or cannot carry what was asked of it."""


@dataclass(frozen=True)
class Account:
    """One settlement currency of one account, as the venue reports it right now."""

    currency: str
    wallet: Decimal
    free: Decimal
    unrealized: Decimal

    @property
    def equity(self) -> Decimal:
        # The wallet balance does not include open positions' unrealised result; equity does.
        return self.wallet + self.unrealized

    def record(self) -> dict[str, str]:
        return {
            "currency": self.currency,
            "wallet": str(self.wallet),
            "free": str(self.free),
            "unrealized": str(self.unrealized),
            "equity": str(self.equity),
        }


def _dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal(0)
    return Decimal(str(value))


def read_account(client: Any, currency: str) -> Account:
    """Wallet, free margin and unrealised result in `currency`, through ccxt's unified calls."""
    try:
        balance = client.fetch_balance()
        row = balance.get(currency) or {}
        unrealized = Decimal(0)
        positions = getattr(client, "fetch_positions", None)
        if positions is not None:
            for held in positions() or []:
                if str(held.get("settle") or currency) != currency:
                    continue
                unrealized += _dec(held.get("unrealizedPnl"))
    except Exception as exc:
        raise CapitalError(f"cannot read the account: {exc.__class__.__name__}: {exc}") from exc
    if not row:
        raise CapitalError(f"the account reports no {currency} balance")
    return Account(
        currency=currency,
        wallet=_dec(row.get("total")),
        free=_dec(row.get("free")),
        unrealized=unrealized,
    )


def sizing_equity(declared: Decimal | None, venue: Decimal | None) -> Decimal | None:
    """The equity sizing and risk are measured against: the smaller of what is declared and held.

    The declared figure is what the operator allows this desk; the venue figure is what is
    actually there. Sizing against the larger of the two would size against money that is either
    not allowed or not present.
    """
    known = [figure for figure in (declared, venue) if figure is not None and figure > 0]
    return min(known) if known else None


def margin_needed(notional: Decimal, leverage: Decimal, fee_bps: Decimal = Decimal(0)) -> Decimal:
    """Initial margin for a notional at a leverage, plus the fee of getting in."""
    if leverage <= 0:
        raise CapitalError("leverage is positive")
    return notional / leverage + notional * fee_bps / BPS


def liquidation_estimate(
    entry: Decimal, side: str, leverage: Decimal, maintenance: Decimal
) -> Decimal:
    """Where an isolated position at this leverage is liquidated, before fees and funding.

    It is the conservative figure on purpose: a cross position draws on the whole account and is
    liquidated later, so a stop that clears this estimate clears the real one too.
    """
    if leverage <= 0:
        raise CapitalError("leverage is positive")
    move = Decimal(1) / leverage - maintenance
    return entry * (Decimal(1) - move) if side == "buy" else entry * (Decimal(1) + move)


def maintenance_rate(client: Any, symbol: str, default: Decimal = Decimal("0.01")) -> Decimal:
    """The venue's maintenance margin rate for the first tier, or a deliberately high default."""
    tiers = getattr(client, "fetch_market_leverage_tiers", None)
    if tiers is None:
        return default
    try:
        first = (tiers(symbol) or [{}])[0]
        rate = first.get("maintenanceMarginRate")
        return default if rate is None else Decimal(str(rate))
    except Exception:
        return default


def day_loss_bps(root: Path, now: datetime, equity: Decimal) -> Decimal:
    """How far equity has fallen since the first reading of this UTC day, in bps; never negative.

    The first reading of a day is written down and every later one is measured against it. That
    counts realised and unrealised results together, and it needs nothing from the venue beyond
    the balance it already reports.
    """
    day = now.date().isoformat()
    path = Path(root) / EQUITY_LOG
    start: Decimal | None = None
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, Mapping) and row.get("day") == day:
                start = _dec(row.get("equity"))
                break
    if start is None:
        append_line(path, {"day": day, "equity": str(equity), "at": now.isoformat()}, durable=True)
        return Decimal(0)
    if start <= 0:
        return Decimal(0)
    return max(Decimal(0), (start - equity) / start * BPS).quantize(Decimal("0.01"))


def drawdown_bps(root: Path, now: datetime, equity: Decimal) -> tuple[Decimal, Decimal]:
    """How far equity sits below the highest reading this desk has seen, in bps, and that peak.

    The peak is kept in its own file and only ever rises. Deleting the file starts the measure
    again from the next reading, which is how an operator says the drawdown has been looked at.
    """
    path = Path(root) / EQUITY_PEAK
    peak: Decimal | None = None
    if path.is_file():
        try:
            peak = _dec(json.loads(path.read_text(encoding="utf-8")).get("peak"))
        except (ValueError, AttributeError):
            peak = None
    if peak is None or equity > peak:
        write_json(path, {"peak": str(equity), "at": now.isoformat()}, durable=True)
        return Decimal(0), equity
    if peak <= 0:
        return Decimal(0), peak
    return ((peak - equity) / peak * BPS).quantize(Decimal("0.01")), peak
