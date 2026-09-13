"""Every model call written down, and a budget that ends the session when it is spent.

Token cost is the number that decides whether an agent is worth running on an instrument at
all, so it is recorded the way fills are: one line per call, appended as it happens, with the
role, the provider, the model, the tokens, the latency and the price the operator declared.
The budget refuses before the call, never after: a call that would overrun is not made.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agent.agent.completer import Completer, Completion


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
    token_budget: int
    max_calls_per_hour: int
    min_interval_s: int
    call_timeout_s: int = 60

    def __post_init__(self) -> None:
        for label in ("token_budget", "max_calls_per_hour", "call_timeout_s"):
            if getattr(self, label) <= 0:
                raise BudgetError(f"{label} must be positive")
        if self.min_interval_s < 0:
            raise BudgetError("min_interval_s cannot be negative")


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

    def record(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
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

    def append(self, record: UsageRecord) -> None:
        self.records.append(record)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.record(), sort_keys=True) + "\n")

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
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


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
    ) -> None:
        self.inner = inner
        self.role = role
        self.provider = provider
        self.limits = limits
        self.ledger = ledger
        self.pricing = dict(pricing or {})
        self.clock = clock
        self._recent: deque[float] = deque()
        self._last: float | None = None

    def refuse(self, now: float) -> str | None:
        """Why the next call would not be admitted, or None."""
        if self.ledger.tokens >= self.limits.token_budget:
            return f"token budget spent: {self.ledger.tokens} of {self.limits.token_budget}"
        if self._last is not None and now - self._last < self.limits.min_interval_s:
            return f"last call {now - self._last:.0f}s ago; minimum {self.limits.min_interval_s}s"
        while self._recent and now - self._recent[0] >= 3600:
            self._recent.popleft()
        if len(self._recent) >= self.limits.max_calls_per_hour:
            return f"{len(self._recent)} calls this hour; cap {self.limits.max_calls_per_hour}"
        return None

    def complete(self, *, system: str, user: str, schema: Mapping[str, Any]) -> Completion:
        now = self.clock()
        why = self.refuse(now)
        if why is not None:
            raise BudgetSpentError(f"{self.role}: {why}")
        self._recent.append(now)
        self._last = now
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
        self.ledger.append(
            UsageRecord(
                at=now,
                role=self.role,
                provider=self.provider,
                model=completion.model,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                latency_ms=int((time.perf_counter() - started) * 1000),
                cost=(
                    None
                    if price is None
                    else price.cost(completion.input_tokens, completion.output_tokens)
                ),
                outcome="ok",
            )
        )
        return completion
