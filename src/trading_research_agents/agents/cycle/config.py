"""What one cycle is given: the instrument, the venue, the chains, the gates, the clock."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents.agents.progress import (
    Watch,
    silent,
)
from trading_research_agents.data.schema import Direction
from trading_research_agents.data.sources.chain import SourceChain
from trading_research_agents.envelope.model import Channel, Envelope
from trading_research_agents.execution.confluence import STANCES, Matrix
from trading_research_agents.execution.conviction import (
    Conviction,
)
from trading_research_agents.execution.desk import Desk, Open
from trading_research_agents.execution.kelly import SizingRule
from trading_research_agents.execution.mandate import MODES as MANDATE_MODES
from trading_research_agents.execution.regime import RegimeRule
from trading_research_agents.execution.stops import VolatilityExit
from trading_research_agents.execution.targets import (
    Targets,
)
from trading_research_agents.execution.trigger import (
    Trigger,
)
from trading_research_agents.llm.completer import Completer
from trading_research_agents.llm.judge import Judge
from trading_research_agents.llm.usage import UsageLedger
from trading_research_agents.readings import READINGS
from trading_research_agents.trading.market import Market

# The third mode sends orders, and it only exists when the caller hands the cycle something to
# send them with. That hand is built by the CLI, and only after the mandate has cleared.
MODES = MANDATE_MODES


TARGETS = {"long": Direction.LONG, "short": Direction.SHORT}
# Who may join the structure lead: a blind second read of its own brief, and a reader of the
# flow figures that then leave the structure brief.
TEAM = ("blind_confirmer", "flow_specialist")


# What a contract market has and a spot pair does not.
CONTRACT_KINDS = ("funding", "open_interest")


class CycleError(ValueError):
    """The cycle cannot run as configured, or cannot run at all on what the engine returned."""


@dataclass(frozen=True)
class CycleConfig:
    """Everything one cycle needs, and nothing it should decide for itself."""

    instrument: str
    envelope: Envelope
    venue_key: str
    chains: Mapping[str, SourceChain]
    completer_for: Callable[[str], Completer]
    usage: UsageLedger
    root: Path
    mechanism: str
    magnitude_bps: Decimal
    # The base position, in base units. A desk that watches several instruments says it in quote
    # currency instead - 0.01 is eight hundred dollars of one and twenty-seven of another - and the
    # cycle converts it at its own mid.
    size: Decimal = Decimal(0)
    size_quote: Decimal | None = None
    bar_interval_s: int = 60
    lookback_bars: int = 60
    interval_s: int = 60
    settlement_interval_s: int = 28_800
    latency_slippage_bps: Decimal = Decimal(0)
    margin_bps: Decimal = Decimal(0)
    min_confidence: float = 0.6
    mode: str = "sandbox"
    # Two gates the operator declares: whether the context agrees with the direction, and whether
    # the trade is the shape this desk said it wanted. Both are applied by code, never by a model.
    matrix: Matrix = field(default_factory=Matrix)
    targets: Targets = field(default_factory=Targets)
    # How much goes behind the idea, from the same declarations the matrix reads. Left undeclared,
    # every admitted cycle trades the operator's base size, which is what it did before.
    conviction: Conviction = field(default_factory=Conviction)
    max_position: Decimal | None = None
    # The position cap in quote currency, converted at this cycle's mid; the smaller cap applies.
    max_position_quote: Decimal | None = None
    equity: Decimal | None = None
    size_step: Decimal = Decimal("0.000001")
    reward_to_risk: Decimal | None = None
    # [trigger]: what has to have happened before anyone is paid to have an opinion. Undeclared,
    # every scheduled cycle asks the whole roster, which is what it did before.
    trigger: Trigger = field(default_factory=Trigger)
    # [controls].max_risk_per_trade_bps, applied to the size the conviction ladder arrived at. The
    # session runtime has always enforced this; a cycle that places its own orders has to as well.
    max_risk_per_trade_bps: Decimal | None = None
    # [desk]: what the whole book may carry at once, and where to look for what is already open.
    # The caller decides where to look, because only the caller knows: the venue in venue mode, the
    # intents on disk everywhere else.
    desk: Desk = field(default_factory=Desk)
    held: Callable[[], tuple[tuple[Open, ...], str]] | None = None
    # Venue mode without adopt_open_position: an instrument already open at the venue waits for its
    # exits rather than being decided on again, since the venue hand would refuse the entry.
    wait_while_open: bool = False
    # Venue mode: what the account actually holds, read before anyone is asked. Sizing and risk use
    # the smaller of this and [run].equity, and the day's loss is measured from it.
    account: Callable[[], Any] | None = None
    daily_loss_limit_bps: Decimal | None = None
    # [controls].max_drawdown_bps, from the desk's highest equity; with on_drawdown = "flatten"
    # the kill switch file is written, and the next pass flattens and stops the run.
    max_drawdown_bps: Decimal | None = None
    drawdown_kill: Path | None = None
    # Hard limits the risk gate holds every intent to, and the switch files that hold the desk.
    max_notional: Decimal | None = None
    max_trades_per_day: int | None = None
    switches: tuple[Path, ...] = ()
    # [sizing]: the desk's own round trips may lower the risk per trade, never raise it.
    sizing: SizingRule = field(default_factory=SizingRule)
    # What the models have already cost since this desk last wrote an intent. A day of refusals is
    # paid for by whatever trade the day produces, so the caller carries the figure across cycles.
    carried_spend: Decimal = Decimal(0)
    # `tra run --now`: ask the roster whatever the trigger would have said. An override belongs to
    # the operator, and it is written into the cycle rather than left to be inferred.
    forced: bool = False
    # The exit rule as declared, so the cycle can price the stop and the take profit for the venue.
    # Code computes them from the operator's own fractions; no model is asked where a stop goes.
    stop_frac: Decimal | None = None
    take_frac: Decimal | None = None
    # [exit].mode = "volatility": each trade's stop measured from the decision timeframe's range.
    volatility_exit: VolatilityExit | None = None
    # [agents].validators: the stances to ask once there is a proposal worth arguing about. Each
    # stance is one more call, so an operator turns them on deliberately.
    validators: tuple[str, ...] = ()
    # refuse: a declared stance that no model answered stops the trade. continue: trade on the votes
    # that came back. A lost check never adds size either way.
    on_missing_validator: str = "refuse"
    # Higher timeframes read as context, in seconds; the decision stays on interval_s.
    context_intervals: tuple[int, ...] = ()
    # How long a position is kept: the analyst's horizon, or [exit].timeout_bars. The cost gate
    # prices funding over the longer of what the desk will hold, not the shorter.
    hold_limit: str = "horizon"
    timeout_bars: int | None = None
    regime: RegimeRule = field(default_factory=RegimeRule)
    # What actually sends an order, when the operator is in venue mode and the mandate cleared.
    place: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    # The instrument is a spot market on the venue orders go to. A short there needs a borrow, so it
    # is refused by code before it is priced; funding and open interest belong to contracts, so
    # they are not asked for, rather than asked for and refused every cycle.
    spot: bool = False
    # What the underlying is, from the venue's own market fields. An equity's exchange keeps
    # hours: with equity_session "regular" an entry waits for the session and a hold may not run
    # past its close; on-chain figures do not bear on a share, so that reading is not read.
    asset_class: str = "crypto"
    # The market itself, as the venue describes it. A prediction outcome is decided by the event
    # analyst on a probability, priced to resolution, and sized by its stake.
    market: Market | None = None
    equity_session: str = "any"
    equity_closed: tuple[date, ...] = ()
    # A reading is read only when the roster names it. `tra run` names the ones with a declared
    # source; an operator who wants one read anyway, on notes alone, names it in [agents].readings.
    data_options: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    series: Mapping[str, tuple[Path, ...]] = field(default_factory=dict)
    news: tuple[Path, ...] = ()
    readings: tuple[str, ...] = ()
    # [agents].team: who answers beside the structure lead. Empty is the analyst alone, on the
    # whole brief, as before.
    team: tuple[str, ...] = ()
    # The typed judge, when declared: each structure read's reason checked against the measured
    # labels. Absent, that check is not made and the record says nothing was asked.
    judge: Judge | None = None
    max_workers: int = 4
    # Progress, on its own wire. A watcher sees the steps as they happen and can change none of
    # them; the default is nobody watching.
    watch: Watch = silent
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.instrument.strip():
            raise CycleError("a cycle names its instrument")
        unknown = sorted(set(self.readings) - set(READINGS))
        if unknown:
            raise CycleError(f"{unknown} is not a reading; have {list(READINGS)}")
        stray = sorted(set(self.series) - set(READINGS))
        if stray:
            raise CycleError(f"series declared for {stray}, which is not a reading")
        strangers = sorted(set(self.team) - set(TEAM))
        if strangers or len(set(self.team)) != len(self.team):
            raise CycleError(f"the team is each of {list(TEAM)} at most once, not {self.team}")
        bad_stance = sorted(set(self.validators) - set(STANCES))
        if bad_stance:
            raise CycleError(f"{bad_stance} is not a stance; have {list(STANCES)}")
        if len(set(self.validators)) != len(self.validators):
            raise CycleError("each stance is asked once")
        if self.mode not in MODES:
            raise CycleError(f"mode is one of {list(MODES)}")
        if self.mode == "venue" and self.place is None:
            raise CycleError(
                "venue mode needs something to send with; the CLI builds it from [execution] "
                "once the mandate, the ladder and the keys are all in place"
            )
        if self.size < 0 or (self.size_quote is not None and self.size_quote <= 0):
            raise CycleError("[run].size and [run].size_quote are positive when declared")
        declared = [
            name
            for name, value in (("size", self.size), ("size_quote", self.size_quote))
            if value is not None and value > 0
        ]
        if len(declared) != 1:
            raise CycleError(
                "a cycle is sized by [run].size in base units or [run].size_quote in quote "
                f"currency, and exactly one of them: {declared or 'neither'} is declared"
            )
        if not isinstance(self.magnitude_bps, Decimal) or self.magnitude_bps <= 0:
            raise CycleError("magnitude_bps is the edge the operator claims, in bps, positive")
        if self.channel is None:
            raise CycleError(f"venue_key {self.venue_key!r} is not a channel in the envelope")

    @property
    def channel(self) -> Channel | None:
        found = next((c for c in self.envelope.channels if c.key == self.venue_key), None)
        return found.for_market("spot") if found is not None and self.spot else found
