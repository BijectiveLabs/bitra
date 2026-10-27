"""The structure lead as a decision core: a model decides, the runtime and controls execute.

It satisfies the `Strategy` protocol like any code core, so replay, paper, shadow and canary
run it unchanged. What differs is the cadence and the budget: the model is asked once per
closed bar, at most as often as the budget allows, and only on bars a minute or longer, since
a call takes seconds. A call that fails, times out or comes back malformed is no signal, and
is counted. A spent budget asks the runtime to end the session; it never asks it to hold on.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from trading_research_agents.agents.cited import figures_of, survived
from trading_research_agents.agents.roles import RoleError, prompt
from trading_research_agents.agents.structure_lead import ROLE, SCHEMA, parse
from trading_research_agents.agents.structure_lead.tools import features
from trading_research_agents.llm.completer import Completer
from trading_research_agents.llm.usage import BudgetSpentError
from trading_research_agents.strategy.core import Bar, Signal, Target

MIN_BAR_INTERVAL_S = 60
MIN_BARS = 3
MAX_FAILURES_IN_A_ROW = 5


@dataclass(frozen=True)
class Held:
    target: Target
    entry_px: Decimal | None
    bars: int


class AgentStrategy:
    """`name` and `on_bar`, and a `halt` the runtime reads when the budget is spent."""

    name = "agent-structure-lead"
    min_bar_interval_s = MIN_BAR_INTERVAL_S

    def __init__(
        self,
        completer: Completer,
        *,
        mechanism: str,
        lookback_bars: int = 60,
        min_confidence: float = 0.5,
        decide_every_bars: int = 1,
        max_failures_in_a_row: int = MAX_FAILURES_IN_A_ROW,
    ) -> None:
        if not mechanism.strip():
            raise ValueError("the registered mechanism is what the analyst reasons from")
        if lookback_bars < MIN_BARS:
            raise ValueError(f"lookback_bars must be at least {MIN_BARS}")
        if not 0 <= min_confidence <= 1:
            raise ValueError("min_confidence is in [0, 1]")
        if decide_every_bars < 1:
            raise ValueError("decide_every_bars is at least 1")
        self.decide_every_bars = decide_every_bars
        self._seen = 0
        if max_failures_in_a_row < 1:
            raise ValueError("max_failures_in_a_row is at least 1")
        self.max_failures_in_a_row = max_failures_in_a_row
        self._failures_in_a_row = 0
        self.completer = completer
        self.mechanism = mechanism.strip()
        self.lookback_bars = lookback_bars
        self.min_confidence = min_confidence
        self._bars: deque[Bar] = deque(maxlen=lookback_bars)
        self._held = Held(Target.FLAT, None, 0)
        self.halt: str | None = None
        self.calls = 0
        self.failures = 0
        self.below_confidence = 0
        self.no_setup = 0
        self.uncited = 0
        self.last_reason: str | None = None
        self._figures: dict[str, str] = {}

    def on_bar(self, bar: Bar) -> Signal | None:
        self._bars.append(bar)
        if self._held.target is not Target.FLAT:
            self._held = Held(self._held.target, self._held.entry_px, self._held.bars + 1)
        self._seen += 1
        if self.halt is not None or bar.empty or len(self._bars) < MIN_BARS:
            return None
        # The exits run on every bar in the runtime; the model is asked on a cadence.
        if (self._seen - MIN_BARS) % self.decide_every_bars != 0:
            return None
        try:
            done = self.completer.complete(system=prompt(ROLE), user=self._user(), schema=SCHEMA)
        except BudgetSpentError as exc:
            self.halt = str(exc)
            return None
        except (RoleError, RuntimeError, OSError, TimeoutError, ValueError) as exc:
            self._failed(exc)
            return None
        self.calls += 1
        try:
            answer = parse(done.text)
        except RoleError as exc:
            self._failed(exc)
            return None
        self._failures_in_a_row = 0
        self.last_reason = answer.reason
        if answer.confidence < self.min_confidence:
            self.below_confidence += 1
            return None
        wanted = Target(answer.target)
        if wanted is not Target.FLAT:
            # The same two rules the cycle applies, for the same reason: a direction has to have a
            # named shape behind it, and it has to point at a figure this bar actually carried.
            if answer.setup == "no_setup":
                self.no_setup += 1
                return None
            kept, _ = survived(answer.figures_used, self._figures)
            if not kept:
                self.uncited += 1
                return None
        if wanted is self._held.target:
            return None
        if wanted is Target.FLAT:
            self._held = Held(Target.FLAT, None, 0)
        else:
            self._held = Held(wanted, bar.close, 0)
        return Signal(
            wanted, f"{answer.setup}: {answer.reason} (confidence {answer.confidence:.2f})"
        )

    def _failed(self, exc: Exception) -> None:
        self.failures += 1
        self._failures_in_a_row += 1
        if self._failures_in_a_row >= self.max_failures_in_a_row:
            self.halt = (
                f"structure_lead: {self._failures_in_a_row} failed calls in a row; "
                f"last: {exc.__class__.__name__}"
            )

    def observe_flat(self) -> None:
        self._held = Held(Target.FLAT, None, 0)

    def _user(self) -> str:
        # Compact rows, not JSON: the bars are most of every call, and a call is paid per token.
        computed = features(list(self._bars))
        self._figures = {} if computed is None else figures_of(computed.record())
        lines = [
            f"mechanism: {self.mechanism}",
            f"position: target={self._held.target.value} "
            f"entry_px={'-' if self._held.entry_px is None else self._held.entry_px} "
            f"bars_held={self._held.bars}",
        ]
        if computed is not None:
            row = computed.record()
            lines.append(
                "features over the window (computed from closed bars): "
                + ", ".join(f"{k}={v}" for k, v in row.items() if k not in ("bars", "close"))
            )
        lines.append("bars (start_utc,open,high,low,close,volume,trades), oldest first:")
        for b in self._bars:
            if b.empty:
                continue
            start = datetime.fromtimestamp(b.start_ns / 1e9, tz=UTC).strftime("%Y-%m-%dT%H:%MZ")
            lines.append(f"{start},{b.open},{b.high},{b.low},{b.close},{b.volume},{b.trades}")
        return "\n".join(lines)
