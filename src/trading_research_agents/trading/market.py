"""One shape for every market on every venue, read from ccxt's unified market structure.

ccxt already describes a spot pair, a perpetual, a dated future, an option and a prediction
outcome with the same fields. `Market` keeps those fields as one frozen record, so the rest of the
package asks a market what it is (does it trade in contracts, is it margined, what does it settle
in) instead of growing a code path per market type or per venue.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_research_agents.data.sources.discover import asset_class_of

# ccxt's `type` values. "prediction" is the type its prediction namespace gives an outcome market.
KINDS = ("spot", "margin", "swap", "future", "option", "prediction")


class MarketError(ValueError):
    """The venue's description of a market is missing what an order needs."""


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        found = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return found if found.is_finite() else None


@dataclass(frozen=True)
class Market:
    venue: str
    symbol: str
    kind: str = ""
    base: str = ""
    quote: str = ""
    settle: str = ""
    contract: bool = False
    linear: bool | None = None
    inverse: bool | None = None
    # As ccxt reported it. Judged only when an order needs it, so a market refused for its kind
    # is refused for that and not for a field it was never going to use.
    reported_contract_size: Any = None
    expiry: int | None = None
    active: bool | None = None
    amount_min: Decimal | None = None
    maker: Decimal | None = None
    taker: Decimal | None = None
    described: bool = True
    # What the underlying is: crypto, equity (a stock perpetual or a tokenized stock), commodity,
    # forex. The order path is the same; what differs is when the underlying's own market trades.
    asset_class: str = "crypto"
    # A prediction outcome: a share priced between price_min and price_max that pays 1 if it wins.
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    question: str = ""
    outcome_label: str = ""

    @classmethod
    def from_ccxt(cls, venue: str, symbol: str, market: Mapping[str, Any] | None) -> Market:
        found = dict(market or {})
        if not found:
            return cls(venue=venue, symbol=symbol, described=False)
        kind = str(found.get("type") or "")
        # The boolean flags are what ccxt guarantees; `type` is sometimes left blank by an adapter.
        for flag in ("option", "spot", "swap", "future", "prediction"):
            if not kind and found.get(flag):
                kind = flag
        limits = found.get("limits") or {}
        expiry = _decimal(found.get("expiry"))
        info = found.get("info") or {}
        outcome = found.get("outcome") or {}
        return cls(
            venue=venue,
            symbol=str(found.get("symbol") or symbol),
            kind=kind,
            base=str(found.get("base") or ""),
            quote=str(found.get("quote") or ""),
            settle=str(found.get("settle") or ""),
            contract=bool(found.get("contract")),
            linear=found.get("linear"),
            inverse=found.get("inverse"),
            reported_contract_size=found.get("contractSize"),
            expiry=None if expiry is None else int(expiry),
            active=found.get("active"),
            amount_min=_decimal((limits.get("amount") or {}).get("min")),
            maker=_decimal(found.get("maker")),
            taker=_decimal(found.get("taker")),
            described=True,
            asset_class=asset_class_of(venue, found),
            price_min=_decimal((limits.get("price") or {}).get("min")),
            price_max=_decimal((limits.get("price") or {}).get("max")),
            question=str(info.get("question") or info.get("title") or ""),
            outcome_label=str(outcome.get("label") or "") if isinstance(outcome, dict) else "",
        )

    @property
    def label(self) -> str:
        """The market type in the words a record or a refusal uses."""
        if not self.described:
            return "unknown"
        if self.kind == "option":
            return "option"
        if self.kind in ("spot", "margin"):
            return "spot"
        if self.kind == "future":
            return "dated future"
        if self.kind == "swap":
            return "linear perpetual" if self.linear else "inverse perpetual"
        return self.kind or "unknown"

    @property
    def settlement(self) -> str:
        """The currency results and margin are counted in."""
        return self.settle or self.quote

    @property
    def margined(self) -> bool:
        """Leverage, margin and liquidation apply: true of every contract market."""
        return self.contract

    @property
    def held_as_balance(self) -> bool:
        """A position here is coins in the account rather than a position the venue reports."""
        return self.described and self.kind in ("spot", "margin")

    @property
    def outcome(self) -> bool:
        """A prediction outcome share: bought, held to a price or to resolution; never shorted."""
        return self.described and self.kind == "prediction"

    @property
    def shortable(self) -> bool:
        """A short sells what is not held: a contract can; a spot pair or an outcome cannot."""
        return not (self.held_as_balance or self.outcome)

    def unit(self) -> Decimal:
        """How much of the base one order unit is: a contract, or 1 for a market sized in the base.

        Orders and positions are in contracts, and this package sizes in the base. One venue's
        Bitcoin contract is 0.01 of a coin, another's is 0.0001, and a meme coin's can be ten
        million tokens, so the conversion is never assumed.
        """
        raw = self.reported_contract_size
        if raw in (None, ""):
            if self.contract:
                raise MarketError(
                    f"{self.venue} does not say how large one {self.symbol} contract is, so a size "
                    "in the base cannot be turned into an order"
                )
            return Decimal(1)
        unit = _decimal(raw)
        if unit is None or unit <= 0:
            raise MarketError(f"{self.venue} reports a contract size of {raw!r} for {self.symbol}")
        return unit
