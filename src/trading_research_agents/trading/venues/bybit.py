"""Bybit v5 as a live venue: signed REST, market IOC orders, fills confirmed, position reconciled.

Endpoints, headers, the signature string and the error codes were read from the venue's own
v5 documentation on 2026-09-13:

- sign = HMAC-SHA256(secret, timestamp + api_key + recv_window + payload), lowercase hex, where
  payload is the query string of a GET or the JSON body of a POST, exactly as sent;
- headers X-BAPI-API-KEY, X-BAPI-TIMESTAMP, X-BAPI-SIGN, X-BAPI-RECV-WINDOW;
- POST /v5/order/create with orderLinkId (36 characters, unique), reduceOnly for closes;
- GET /v5/order/realtime by orderLinkId; GET /v5/position/list; GET /v5/user/query-api;
- GET /v5/market/time; retCode 10006 and HTTP 429 for rate limits, 110030 for a duplicate
  orderLinkId, 110023 when only a reduce is allowed.

Nothing here decides anything. It sends what the runtime admitted, waits for the venue to say
what happened, and refuses to continue the moment the venue and the model disagree.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from trading_research_agents import __version__
from trading_research_agents.data.schema import Direction, Trade
from trading_research_agents.execution.cost import BPS, CostBreakdown, funding_accrual_bps
from trading_research_agents.trading.venue import Fill, Intent, VenueError
from trading_research_agents.trading.venues.registry import LiveVenueSettings, register

ENVIRONMENTS = {
    "demo": "https://api-demo.bybit.com",
    "testnet": "https://api-testnet.bybit.com",
    "mainnet": "https://api.bybit.com",
}
RECV_WINDOW_MS = 5000
MAX_CLOCK_SKEW_MS = 1000
MIN_KEY_LIFE_DAYS = 7
ORDER_LINK_MAX = 36
RATE_LIMIT_RETRIES = 3
SUBMIT_RETRIES = 3
NS_PER_MS = 1_000_000
CLOSED = {"Filled", "PartiallyFilledCanceled", "Cancelled", "Rejected", "Deactivated"}
FILLED = {"Filled", "PartiallyFilledCanceled"}


class BybitApiError(VenueError):
    def __init__(self, ret_code: int, message: str, path: str) -> None:
        super().__init__(f"{path}: retCode {ret_code}: {message}")
        self.ret_code = ret_code
        self.path = path


class BybitRest:
    """The signed transport. It never logs a header and its repr never shows the secret."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        timeout_s: float = 10.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key or not api_secret:
            raise VenueError("api key and secret are both required")
        if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1"):
            raise VenueError("the venue is reached over https")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._secret = api_secret.encode("utf-8")
        self.timeout_s = timeout_s
        self.clock = clock
        self.sleep = sleep

    def __repr__(self) -> str:
        tail = self.api_key[-4:] if len(self.api_key) >= 4 else ""
        return f"BybitRest(base_url={self.base_url!r}, key=...{tail})"

    def get(self, path: str, params: Mapping[str, str], *, signed: bool = True) -> dict[str, Any]:
        query = urlencode(list(params.items()))
        url = f"{self.base_url}{path}?{query}" if query else f"{self.base_url}{path}"
        headers = self._headers(query) if signed else {}
        request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
        return self._request(request, path)

    def post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        text = json.dumps(dict(body), separators=(",", ":"))
        headers = self._headers(text)
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(  # noqa: S310 - scheme checked in __init__
            f"{self.base_url}{path}", data=text.encode("utf-8"), headers=headers, method="POST"
        )
        return self._request(request, path)

    def _headers(self, payload: str) -> dict[str, str]:
        timestamp = str(int(self.clock() * 1000))
        message = f"{timestamp}{self.api_key}{RECV_WINDOW_MS}{payload}".encode()
        sign = hmac.new(self._secret, message, hashlib.sha256).hexdigest()
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": sign,
            "X-BAPI-RECV-WINDOW": str(RECV_WINDOW_MS),
            "User-Agent": f"trading-research-agents/{__version__}",
        }

    def _request(self, request: urllib.request.Request, path: str) -> dict[str, Any]:
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                    raw = response.read()
                    reset = response.headers.get("X-Bapi-Limit-Reset-Timestamp")
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < RATE_LIMIT_RETRIES:
                    self._wait(exc.headers.get("X-Bapi-Limit-Reset-Timestamp"))
                    continue
                raise VenueError(f"{path}: HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise VenueError(f"{path}: unreachable: {exc.__class__.__name__}") from None
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                raise VenueError(f"{path}: response is not JSON") from None
            if not isinstance(payload, dict) or "retCode" not in payload:
                raise VenueError(f"{path}: response has no retCode")
            code = int(payload["retCode"])
            if code == 10006 and attempt < RATE_LIMIT_RETRIES:
                self._wait(reset)
                continue
            if code != 0:
                raise BybitApiError(code, str(payload.get("retMsg", "")), path)
            result = payload.get("result")
            return result if isinstance(result, dict) else {}
        raise VenueError(f"{path}: rate limited {RATE_LIMIT_RETRIES} times; giving up")

    def _wait(self, reset_ms: str | None) -> None:
        now_ms = self.clock() * 1000
        try:
            wait_s = (float(reset_ms) - now_ms) / 1000 if reset_ms else 0.5
        except ValueError:
            wait_s = 0.5
        self.sleep(min(max(wait_s, 0.1), 5.0))


@dataclass
class _Order:
    intent: Intent
    link_id: str
    reduce_only: bool
    attempts: int = 0
    accepted: bool = False


class BybitLiveVenue:
    """Satisfies `Venue`. One order at a time, market IOC, closes reduce-only."""

    routes_exits = True

    def __init__(
        self,
        rest: BybitRest,
        settings: LiveVenueSettings,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if settings.environment not in ENVIRONMENTS:
            raise VenueError(f"environment must be one of {sorted(ENVIRONMENTS)}")
        self.rest = rest
        self.s = settings
        self.clock = clock
        self.label = f"bybit-{settings.environment}"
        self._pending: _Order | None = None
        self._seq = 0
        self._signed_size = Decimal(0)
        self._last: Trade | None = None
        self._last_poll: float = 0.0
        self._fees: dict[int, Decimal] = {}
        self._notionals: dict[int, Decimal] = {}
        self.events: list[str] = []
        self._checked = False

    # Before the first print

    def preflight(self) -> dict[str, Any]:
        """Clock, key, position. Each refusal names what to fix; none is skipped."""
        skew = self._clock_skew_ms()
        if abs(skew) > MAX_CLOCK_SKEW_MS:
            raise VenueError(
                f"local clock is {skew:+.0f} ms from the venue; more than {MAX_CLOCK_SKEW_MS} ms "
                "and signatures will fail mid-session; sync the clock first"
            )
        key = self._check_key()
        held = self._venue_position()
        if held != 0 and not self.s.adopt_open_position:
            raise VenueError(
                f"the venue holds {held} {self.s.instrument} already; this run was not told to "
                "adopt it (set [live].adopt_open_position = true to take it over deliberately)"
            )
        self._signed_size = held
        self._checked = True
        return {"clock_skew_ms": skew, "key": key, "position": str(held)}

    def _clock_skew_ms(self) -> float:
        result = self.rest.get("/v5/market/time", {}, signed=False)
        try:
            venue_ms = int(result["timeNano"]) / NS_PER_MS
        except (KeyError, ValueError, TypeError):
            raise VenueError("/v5/market/time: no timeNano in the response") from None
        return self.clock() * 1000 - venue_ms

    def _check_key(self) -> dict[str, Any]:
        info = self.rest.get("/v5/user/query-api", {})
        permissions = info.get("permissions") or {}
        wallet = list(permissions.get("Wallet") or [])
        contract = list(permissions.get("ContractTrade") or [])
        ips = list(info.get("ips") or [])
        if int(info.get("readOnly", 1)) == 1:
            raise VenueError("the key is read-only; a live venue needs a trade key")
        if wallet:
            raise VenueError(
                f"the key carries wallet permissions {wallet}; a trading key must carry none"
            )
        if "Order" not in contract or "Position" not in contract:
            raise VenueError(f"the key needs ContractTrade Order and Position; it has {contract}")
        if not ips and not self.s.key_ip_unbound_ok:
            raise VenueError(
                "the key is not bound to an IP; bind it, or set [live].key_ip_unbound_ok = true "
                "to accept that deliberately"
            )
        expires = str(info.get("expiredAt") or "")
        if expires and expires not in ("-1", "0"):
            days = self._days_until(expires)
            if days is not None and days < MIN_KEY_LIFE_DAYS:
                raise VenueError(f"the key expires in {days} days; rotate it before a live session")
        return {"readOnly": 0, "contract": contract, "ips_bound": bool(ips), "expiredAt": expires}

    def _days_until(self, text: str) -> float | None:
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return (when.timestamp() - self.clock()) / 86400

    def _venue_position(self) -> Decimal:
        result = self.rest.get(
            "/v5/position/list", {"category": "linear", "symbol": self.s.instrument}
        )
        rows = result.get("list") or []
        total = Decimal(0)
        for row in rows:
            if row.get("symbol") != self.s.instrument:
                continue
            side = str(row.get("side", ""))
            try:
                size = Decimal(str(row.get("size", "0")))
            except InvalidOperation:
                raise VenueError("/v5/position/list: size is not a number") from None
            if size == 0 or not side:
                continue
            total += size if side == "Buy" else -size
        return total

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
        link = f"tra-{self.s.run_id[:8]}-{self._seq:06d}"
        if len(link) > ORDER_LINK_MAX:
            raise VenueError("orderLinkId would exceed the venue's 36 characters")
        held = self._signed_size
        opposite = (held > 0 and intent.direction is Direction.SHORT) or (
            held < 0 and intent.direction is Direction.LONG
        )
        order = _Order(intent, link, reduce_only=opposite and intent.size <= abs(held))
        self._pending = order
        self._send(order)
        return None

    def _send(self, order: _Order) -> None:
        order.attempts += 1
        body: dict[str, Any] = {
            "category": "linear",
            "symbol": order.intent.instrument,
            "side": "Buy" if order.intent.direction is Direction.LONG else "Sell",
            "orderType": "Market",
            "qty": str(order.intent.size),
            "timeInForce": "IOC",
            "orderLinkId": order.link_id,
        }
        if order.reduce_only:
            body["reduceOnly"] = True
        try:
            self.rest.post("/v5/order/create", body)
        except BybitApiError as exc:
            if exc.ret_code == 110030:
                # The venue already has this orderLinkId: the earlier attempt landed. Poll it.
                order.accepted = True
                return
            self._pending = None
            self.events.append(f"{order.link_id}: refused: {exc}")
            if exc.ret_code == 110023:
                raise VenueError(
                    f"{order.link_id}: the venue allows only a reduce; the model position "
                    f"({self._signed_size}) does not match the venue's"
                ) from None
            return
        except VenueError as exc:
            if order.attempts < SUBMIT_RETRIES:
                # Unreachable: keep the same orderLinkId so a landed order is found, not doubled.
                self.events.append(f"{order.link_id}: attempt {order.attempts} failed: {exc}")
                return
            self._pending = None
            raise VenueError(f"{order.link_id}: {SUBMIT_RETRIES} attempts failed: {exc}") from None
        order.accepted = True

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
            self._send(order)
            return None
        status = self._status(order.link_id)
        if status is None:
            return None
        state = str(status.get("orderStatus", ""))
        if state not in CLOSED:
            return None
        self._pending = None
        try:
            done = Decimal(str(status.get("cumExecQty", "0")))
            avg = Decimal(str(status.get("avgPrice") or "0"))
            fee = Decimal(str(status.get("cumExecFee") or "0"))
            updated_ms = int(status.get("updatedTime") or 0)
        except (InvalidOperation, ValueError):
            raise VenueError(f"{order.link_id}: order fields are not numbers") from None
        if state not in FILLED or done <= 0 or avg <= 0:
            self.events.append(f"{order.link_id}: {state}, executed {done}")
            return None
        ts_ns = updated_ms * NS_PER_MS if updated_ms else trade.ts_ns
        sign = Decimal(order.intent.direction.sign)
        self._signed_size += sign * done
        self._fees[ts_ns] = fee
        self._notionals[ts_ns] = avg * done
        self._reconcile(order.link_id)
        if done < order.intent.size:
            self.events.append(f"{order.link_id}: partial {done} of {order.intent.size}")
        return Fill(ts_ns=ts_ns, price=avg, size=done, direction=order.intent.direction)

    def _status(self, link_id: str) -> dict[str, Any] | None:
        result = self.rest.get(
            "/v5/order/realtime",
            {"category": "linear", "symbol": self.s.instrument, "orderLinkId": link_id},
        )
        rows = result.get("list") or []
        for row in rows:
            if row.get("orderLinkId") == link_id:
                return dict(row)
        return None

    def _reconcile(self, link_id: str) -> None:
        held = self._venue_position()
        if held != self._signed_size:
            raise VenueError(
                f"{link_id}: reconciliation failed: venue holds {held}, model holds "
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


class BybitLiveFactory:
    name = "bybit"
    env_keys = ("BYBIT_API_KEY", "BYBIT_API_SECRET")
    environments = tuple(ENVIRONMENTS)

    def __call__(self, settings: LiveVenueSettings, env: Mapping[str, str]) -> BybitLiveVenue:
        missing = [k for k in self.env_keys if not env.get(k)]
        if missing:
            raise VenueError(f"missing {', '.join(missing)} in the environment")
        base = ENVIRONMENTS.get(settings.environment)
        if base is None:
            raise VenueError(f"environment must be one of {sorted(ENVIRONMENTS)}")
        options = settings.options or {}
        override = options.get("base_url")
        if isinstance(override, str) and override.startswith("http://127.0.0.1"):
            base = override  # a local test double; never a remote host over http
        rest = BybitRest(
            base_url=base, api_key=env["BYBIT_API_KEY"], api_secret=env["BYBIT_API_SECRET"]
        )
        return BybitLiveVenue(rest, settings)


register(BybitLiveFactory())
