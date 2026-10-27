"""The live venue for the canary tier, on the same ccxt client and hand venue mode sends with.

There is one way to a venue in this package. The runtime's `Venue` protocol sits on top of a
`VenueHand`: the hand builds the client from the environment, routes it to the declared demo,
testnet or mainnet, resolves the market, reads the clock, the account mode and the position. This
module adds what the runtime needs on top: one market order at a time under a client id from the
run, closes that are reduce-only, the fill confirmed from the venue's own order state, and the
position reconciled after every fill.

Nothing is retried blindly. An order whose send failed in transit is sent again under the same
client id on the next poll, so an order that landed is found rather than doubled.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_research_agents.data.schema import Direction, Trade
from trading_research_agents.execution.cost import CostBreakdown, funding_accrual_bps
from trading_research_agents.execution.fill import BPS
from trading_research_agents.trading.dispatch import VenueHand, hand_for
from trading_research_agents.trading.hand_state import ENVIRONMENTS, DispatchError, credentials_for
from trading_research_agents.trading.venue import Fill, Intent, VenueError
from trading_research_agents.trading.venues.accounts import account_refusal, key_refusal
from trading_research_agents.trading.venues.limits import client_id_prefix
from trading_research_agents.trading.venues.margin import route
from trading_research_agents.trading.venues.registry import LiveVenueSettings

SUBMIT_RETRIES = 3
NS_PER_MS = 1_000_000
CLIENT_ID_MAX = 32
FILLED = ("closed", "filled")
ENDED = ("canceled", "cancelled", "rejected", "expired")


@dataclass
class _Order:
    intent: Intent
    client_id: str
    reduce_only: bool
    order_id: str = ""
    attempts: int = 0
    accepted: bool = False


class CcxtLiveVenue:
    """Satisfies `Venue`. One market order at a time; closes are reduce-only."""

    routes_exits = True

    def __init__(
        self,
        hand: VenueHand,
        settings: LiveVenueSettings,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.hand = hand
        self.s = settings
        self.clock = clock
        self.label = f"{hand.venue}-{settings.environment}"
        self._pending: _Order | None = None
        self._seq = 0
        self._signed_size = Decimal(0)
        self._last: Trade | None = None
        self._last_poll = 0.0
        self._fees: dict[int, Decimal] = {}
        self._notionals: dict[int, Decimal] = {}
        self.events: list[str] = []
        self._checked = False

    # Before the first print

    def preflight(self) -> dict[str, Any]:
        """Clock, account mode, key, position. Each refusal names what to fix; none is skipped."""
        try:
            skew = self.hand.clock_skew()
            if skew:
                raise VenueError(skew)
            for refusal in (
                self.hand.hedged(),
                account_refusal(self.hand.venue, self.hand.client),
                key_refusal(
                    self.hand.venue,
                    self.hand.client,
                    ip_unbound_ok=self.s.key_ip_unbound_ok,
                    now=self.clock(),
                ),
            ):
                if refusal:
                    raise VenueError(refusal)
            market = self.hand.market()
            if market.label != "linear perpetual":
                raise VenueError(
                    f"{self.hand.symbol} is a {market.label} market; the live tier trades linear "
                    "perpetuals, and spot or prediction markets go through venue mode in tra run"
                )
            unit = market.unit()
            held = self._venue_position()
        except DispatchError as exc:
            raise VenueError(str(exc)) from None
        except VenueError:
            raise
        except Exception as exc:
            raise VenueError(self.hand.redact(f"{exc.__class__.__name__}: {exc}")) from None
        if held != 0 and not self.s.adopt_open_position:
            raise VenueError(
                f"the venue holds {held} {self.s.instrument} already; this run was not told to "
                "adopt it (set [live].adopt_open_position = true to take it over deliberately)"
            )
        self._signed_size = held
        self._checked = True
        return {
            "symbol": self.hand.symbol,
            "contract_size": f"{unit:f}",
            "position": str(held),
        }

    def _venue_position(self) -> Decimal:
        held = self.hand.position()
        if held is None:
            raise VenueError(f"{self.hand.venue} does not report positions through ccxt")
        return held

    # The Venue protocol

    @property
    def pending(self) -> Intent | None:
        return None if self._pending is None else self._pending.intent

    def observe(self, trade: Trade) -> None:
        self._last = trade

    def submit(self, intent: Intent) -> Fill | None:
        if not self._checked:
            raise VenueError("preflight() has not run; a live venue does not trade unchecked")
        if self._pending is not None:
            raise VenueError("an intent is already pending; one order at a time")
        if intent.size <= 0:
            raise VenueError("size must be positive")
        self._seq += 1
        # Letters and digits only: the narrowest client id rule among the venues holds for all.
        client_id = f"{client_id_prefix(self.hand.venue)}tra{self.s.run_id[:8]}{self._seq:06d}"
        if len(client_id) > CLIENT_ID_MAX:
            raise VenueError(f"the client id would exceed {CLIENT_ID_MAX} characters")
        held = self._signed_size
        opposite = (held > 0 and intent.direction is Direction.SHORT) or (
            held < 0 and intent.direction is Direction.LONG
        )
        order = _Order(intent, client_id, reduce_only=opposite and intent.size <= abs(held))
        self._pending = order
        self._send(order)
        return None

    def _send(self, order: _Order) -> None:
        import ccxt

        order.attempts += 1
        try:
            unit = self.hand.contract_size()
        except DispatchError as exc:
            self._pending = None
            raise VenueError(str(exc)) from None
        contracts = order.intent.size / unit
        # Sent as sized or not at all: ccxt would round an amount off the venue's step to a
        # different trade from the one the runtime priced and will reconcile against.
        off_step = self.hand.off_step(contracts, order.intent.size, unit)
        if off_step:
            self._pending = None
            raise VenueError(off_step)
        params: dict[str, Any] = {"clientOrderId": order.client_id, **self.hand.order_params()}
        if order.reduce_only:
            params["reduceOnly"] = True
        side = "buy" if order.intent.direction is Direction.LONG else "sell"
        try:
            placed = self.hand.client.create_order(
                self.hand.symbol, "market", side, float(contracts), None, params
            )
        except ccxt.DuplicateOrderId:
            # The venue already has this client id: the earlier attempt landed. Poll for it.
            order.accepted = True
            return
        except ccxt.NetworkError as exc:
            said = self.hand.redact(f"{exc.__class__.__name__}: {exc}")[:200]
            if order.attempts < SUBMIT_RETRIES:
                # The same client id goes out again, so an order that landed is found, not doubled.
                self.events.append(f"{order.client_id}: attempt {order.attempts} failed: {said}")
                return
            self._pending = None
            raise VenueError(
                f"{order.client_id}: {SUBMIT_RETRIES} attempts failed: {said}"
            ) from None
        except ccxt.BaseError as exc:
            self._pending = None
            said = self.hand.redact(f"{exc.__class__.__name__}: {exc}")[:200]
            self.events.append(f"{order.client_id}: refused: {said}")
            if order.reduce_only:
                # A reduce the venue will not take means its position is not the one this run holds.
                raise VenueError(
                    f"{order.client_id}: the venue refused a reduce-only close; the model position "
                    f"({self._signed_size}) does not match the venue's: {said}"
                ) from None
            return
        order.accepted = True
        order.order_id = str((placed or {}).get("id") or "")

    def poll(self, trade: Trade) -> Fill | None:
        self._last = trade
        order = self._pending
        if order is None:
            return None
        now = self.clock()
        if now - self._last_poll < self.s.poll_interval_ms / 1000:
            return None
        self._last_poll = now
        if not order.accepted:
            # The last send failed in transit. If it landed anyway, the venue knows the client id,
            # and sending again would be asking it to refuse a duplicate or, worse, to accept one.
            if self._status(order) is not None:
                order.accepted = True
            else:
                self._send(order)
                return None
        status = self._status(order)
        if status is None:
            return None
        state = str(status.get("status") or "").lower()
        if state not in (*FILLED, *ENDED):
            return None
        if status.get("filled") is None or (state in FILLED and status.get("average") is None):
            # Finished, but not yet saying with what; a fill read as nothing would be lost.
            return None
        self._pending = None
        try:
            done = Decimal(str(status.get("filled") or 0))
            average = Decimal(str(status.get("average") or 0))
            fee = Decimal(str((status.get("fee") or {}).get("cost") or 0))
            stamp = int(status.get("lastTradeTimestamp") or status.get("timestamp") or 0)
        except (InvalidOperation, ValueError, TypeError):
            raise VenueError(f"{order.client_id}: the order fields are not numbers") from None
        if done <= 0 or average <= 0:
            self.events.append(f"{order.client_id}: {state}, executed {done}")
            return None
        ts_ns = stamp * NS_PER_MS if stamp else trade.ts_ns
        self._signed_size += Decimal(order.intent.direction.sign) * done
        self._fees[ts_ns] = abs(fee)
        self._notionals[ts_ns] = average * done
        self._reconcile(order.client_id)
        if done < order.intent.size:
            self.events.append(f"{order.client_id}: partial {done} of {order.intent.size}")
        return Fill(ts_ns=ts_ns, price=average, size=done, direction=order.intent.direction)

    def _status(self, order: _Order) -> Mapping[str, Any] | None:
        """The venue's own account of the order, in the base, found by id or by client id."""
        if order.order_id:
            return self.hand.order_status(order.order_id)
        # Sent twice and acknowledged as a duplicate: the venue's id was never seen, so the order is
        # looked up by the client id this run gave it.
        unit = self.hand.contract_size()
        for way in ("fetch_open_orders", "fetch_closed_orders"):
            fetch = getattr(self.hand.client, way, None)
            if fetch is None:
                continue
            rows: list[Mapping[str, Any]] = []
            # A list this venue will not give is not an answer; the next poll asks again.
            with suppress(Exception):
                rows = list(fetch(self.hand.symbol) or [])
            for row in rows:
                if str(row.get("clientOrderId") or "") == order.client_id:
                    order.order_id = str(row.get("id") or "")
                    return self.hand.in_base(row, unit)
        return None

    def _reconcile(self, client_id: str) -> None:
        held = self._venue_position()
        if held != self._signed_size:
            raise VenueError(
                f"{client_id}: reconciliation failed: venue holds {held}, model holds "
                f"{self._signed_size}; stopping"
            )

    def cost_of(
        self, direction: Direction, entry_ts_ns: int, exit_ts_ns: int, spread_cost_bps: Decimal
    ) -> CostBreakdown:
        """Fees as charged, funding as scheduled. Spread and slippage are inside the fill prices."""
        fees = self._fees
        entry = self._notionals.get(entry_ts_ns)
        if entry_ts_ns in fees and exit_ts_ns in fees and entry:
            fee_bps = (fees[entry_ts_ns] + fees[exit_ts_ns]) / entry * BPS
        else:
            fee_bps = self.s.channel.entry_fee_bps + self.s.channel.exit_fee_bps
        return CostBreakdown(
            fee_round_trip_bps=fee_bps,
            spread_cost_bps=Decimal(0),
            slippage_bps=Decimal(0),
            funding_accrual_bps=funding_accrual_bps(
                direction, self.s.funding, entry_ts_ns, exit_ts_ns
            ),
        )


class CcxtLiveFactory:
    """Any venue ccxt knows, as a live venue: the keys it signs with, read from `env`."""

    environments = ENVIRONMENTS

    def __init__(self, venue: str) -> None:
        self.name = venue.strip().lower()

    @property
    def env_keys(self) -> tuple[str, ...]:
        return tuple(credentials_for(self.name).values())

    def __call__(self, settings: LiveVenueSettings, env: Mapping[str, str]) -> CcxtLiveVenue:
        missing = [k for k in self.env_keys if not env.get(k)]
        if missing:
            raise VenueError(f"missing {', '.join(missing)} in the environment")
        if settings.environment not in ENVIRONMENTS:
            raise VenueError(f"environment must be one of {list(ENVIRONMENTS)}")
        try:
            # The hand's cap guards its own single order, which the live tier never sends; the
            # canary caps in front of the runtime are what bound this session.
            hand = hand_for(
                venue=self.name,
                symbol=settings.instrument,
                max_notional=Decimal(1),
                environment=settings.environment,
                env=env,
            )
            hand.symbol = _resolved(hand, settings.instrument)
            route(hand.venue, hand.client, hand.symbol)
        except DispatchError as exc:
            raise VenueError(str(exc)) from None
        return CcxtLiveVenue(hand, settings)


def _resolved(hand: VenueHand, instrument: str) -> str:
    """The unified symbol of the instrument's linear perpetual on this venue."""
    from trading_research_agents.data.sources.resolve import ResolveError, resolve

    try:
        hand.client.load_markets()
        return resolve(
            instrument,
            getattr(hand.client, "markets_by_id", None) or {},
            (getattr(hand.client, "markets", None) or {}).values(),
        )
    except ResolveError as exc:
        raise VenueError(str(exc)) from None
