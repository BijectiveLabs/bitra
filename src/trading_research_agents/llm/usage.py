"""Every model call written down, and the ceilings an operator may put on top of that.

Token cost is the number that decides whether an agent is worth running on an instrument at all, so
it is recorded the way fills are: one line per call, appended as it happens, with the role, the
provider, the model, the tokens, the latency and the price the operator declared.

Counting is the default and capping is not. A role is never cut off mid-thought by a ceiling this
software chose for it: an answer truncated halfway is a wasted call and a worse decision, not a
saving. What a desk spends is measured, reported per cycle and read by the auditors, and an operator
who wants a hard ceiling declares one - `token_budget`, `max_calls_per_hour`, `min_interval_s` -
knowing that it stops the desk rather than slows it. A budget is checked before each call: once
the tokens recorded reach it, the next call is not made. What a call will cost is not known until
it returns, so the last admitted call - or the calls a cycle has in flight at once - can carry the
total past the figure. The rate ceilings are counted across every role in one run, not per role.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.llm.completer import Completer, Completion
from trading_research_agents.records import append_line, read_lines


class BudgetError(ValueError):
    """A budget figure is missing or not a positive number."""


class BudgetSpentError(RuntimeError):
    """The session has spent what the operator allowed. Nothing more is asked of a model."""


@dataclass(frozen=True)
class Pricing:
    """What the operator declared a model costs, per million tokens. Never a constant here."""

    input_per_million: Decimal
    output_per_million: Decimal

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        million = Decimal(1_000_000)
        return (
            self.input_per_million * input_tokens + self.output_per_million * output_tokens
        ) / million


@dataclass(frozen=True)
class BudgetLimits:
    """The ceilings, each one optional. Undeclared means counted and never refused."""

    token_budget: int | None = None
    max_calls_per_hour: int | None = None
    min_interval_s: int = 0
    call_timeout_s: int = 60

    def __post_init__(self) -> None:
        for label in ("token_budget", "max_calls_per_hour"):
            declared = getattr(self, label)
            if declared is not None and declared <= 0:
                raise BudgetError(f"{label} is positive when it is declared at all")
        if self.call_timeout_s <= 0:
            raise BudgetError("call_timeout_s must be positive")
        if self.min_interval_s < 0:
            raise BudgetError("min_interval_s cannot be negative")

    @property
    def capped(self) -> bool:
        return self.token_budget is not None or self.max_calls_per_hour is not None


@dataclass(frozen=True)
class UsageRecord:
    at: float
    role: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost: Decimal | None
    outcome: str
    cached_tokens: int = 0
    cache_write_tokens: int = 0

    def record(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "latency_ms": self.latency_ms,
            "cost": None if self.cost is None else str(self.cost),
            "outcome": self.outcome,
        }


class UsageLedger:
    """`usage.jsonl`: appended per call, and totalled on demand."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.records: list[UsageRecord] = []
        # Analysts and validators answer on several threads at once, and an append-mode file on
        # Windows is not safe against two writers: lines could land inside each other.
        self._lock = threading.Lock()

    def append(self, record: UsageRecord) -> None:
        with self._lock:
            self.records.append(record)
            append_line(self.path, record.record())

    @property
    def tokens(self) -> int:
        return sum(r.input_tokens + r.output_tokens for r in self.records)

    @property
    def cost(self) -> Decimal:
        return sum((r.cost for r in self.records if r.cost is not None), Decimal(0))

    def by_role(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in self.records:
            row = out.setdefault(
                r.role, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}
            )
            row["calls"] += 1
            row["input_tokens"] += r.input_tokens
            row["output_tokens"] += r.output_tokens
            row["latency_ms"] += r.latency_ms
        return out

    @classmethod
    def read(cls, path: Path) -> list[dict[str, Any]]:
        return read_lines(path)


@dataclass
class RateWindow:
    """The calls one run has made, shared by every role in it.

    `max_calls_per_hour` and `min_interval_s` are what the operator allows the desk, not each role:
    a run with six roles each holding its own window would make six times the calls declared.
    """

    recent: deque[float] = field(default_factory=deque)
    last: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class MeteredCompleter:
    """Wraps any completer: admits by budget, times the call, writes the usage line."""

    def __init__(
        self,
        inner: Completer,
        *,
        role: str,
        provider: str,
        limits: BudgetLimits,
        ledger: UsageLedger,
        pricing: Mapping[str, Pricing] | None = None,
        clock: Callable[[], float] = time.time,
        window: RateWindow | None = None,
    ) -> None:
        self.inner = inner
        self.role = role
        self.provider = provider
        self.limits = limits
        self.ledger = ledger
        self.pricing = dict(pricing or {})
        self.clock = clock
        self.window = window if window is not None else RateWindow()

    def refuse(self, now: float) -> str | None:
        """Why the next call would not be admitted, or None. Only what the operator declared."""
        budget = self.limits.token_budget
        if budget is not None and self.ledger.tokens >= budget:
            return f"token budget spent: {self.ledger.tokens} of {budget}"
        seen = self.window
        if seen.last is not None and now - seen.last < self.limits.min_interval_s:
            return f"last call {now - seen.last:.0f}s ago; minimum {self.limits.min_interval_s}s"
        while seen.recent and now - seen.recent[0] >= 3600:
            seen.recent.popleft()
        hourly = self.limits.max_calls_per_hour
        if hourly is not None and len(seen.recent) >= hourly:
            return f"{len(seen.recent)} calls this hour; cap {hourly}"
        return None

    def complete(self, *, system: str, user: str, schema: Mapping[str, Any]) -> Completion:
        now = self.clock()
        # Admitting is one step under the window's lock, so two roles asking at the same instant
        # cannot both take the last call the hour allows.
        with self.window.lock:
            why = self.refuse(now)
            if why is not None:
                raise BudgetSpentError(f"{self.role}: {why}")
            self.window.recent.append(now)
            self.window.last = now
        started = time.perf_counter()
        try:
            completion = self.inner.complete(system=system, user=user, schema=schema)
        except Exception as exc:
            self.ledger.append(
                UsageRecord(
                    at=now,
                    role=self.role,
                    provider=self.provider,
                    model="",
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    cost=None,
                    outcome=f"error: {exc.__class__.__name__}",
                )
            )
            raise
        price = self.pricing.get(completion.model)
        counted = completion.input_tokens + completion.output_tokens > 0
        self.ledger.append(
            UsageRecord(
                at=now,
                role=self.role,
                provider=self.provider,
                model=completion.model,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                cached_tokens=completion.cached_tokens,
                cache_write_tokens=completion.cache_write_tokens,
                latency_ms=int((time.perf_counter() - started) * 1000),
                cost=(
                    None
                    if price is None
                    else price.cost(completion.input_tokens, completion.output_tokens)
                ),
                outcome="ok" if counted else "ok, no usage reported",
            )
        )
        return completion
