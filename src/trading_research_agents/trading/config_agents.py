"""[agents]: the models a desk asks, the team, the readings and the roles, as declared."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from trading_research_agents.execution.confluence import STANCES
from trading_research_agents.trading.config_values import (
    ConfigError,
    _choice,
    _dec,
    _int,
    _no_secrets,
)


@dataclass(frozen=True)
class RoleModel:
    """One role's model, after the defaults in [agents] and its own overrides are settled."""

    role: str
    provider: str
    model: str | None
    base_url: str | None
    max_output_tokens: int | None
    call_timeout_s: int
    max_retries: int
    fallback: tuple[str, ...] = ()
    # How hard this role is asked to think, where the vendor takes such a thing. Undeclared means
    # the vendor's own default, which is what every run did before this existed.
    effort: str | None = None


@dataclass(frozen=True)
class AgentSettings:
    """[agents]: which model decides, how often it may be asked, and what that may cost."""

    role: str
    provider: str
    model: str | None
    base_url: str | None
    mechanism: str
    # Every ceiling below is optional, and undeclared means counted rather than capped. A role cut
    # off mid-answer costs the call and returns a worse decision, so a ceiling is something an
    # operator asks for deliberately, not something this software assumes on their behalf.
    token_budget: int | None = None
    # Per UTC day, across runs and restarts, every role and both auditors: past either one the desk
    # asks no model until the next day, and open positions keep their exits.
    daily_token_budget: int | None = None
    daily_cost_budget: Decimal | None = None
    max_calls_per_hour: int | None = None
    min_interval_s: int = 0
    call_timeout_s: int = 60
    lookback_bars: int = 60
    min_confidence: float = 0.5
    pricing: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    decide_every_bars: int = 1
    max_output_tokens: int | None = None
    max_retries: int = 2
    effort: str | None = None
    magnitude_bps: Decimal | None = None
    screen: dict[str, float] = field(default_factory=dict)
    # [agents.roles.<role>]: a cheaper model for the roles that only read and label, a stronger
    # one where the reasoning is on numbers. Anything a role does not override it inherits.
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    # [agents].readings: the readings to read, when the operator wants to say it outright. Empty
    # means every reading with a declared source.
    readings: tuple[str, ...] = ()
    # [agents].team: who answers beside the structure lead: a blind peer on its brief, and a
    # flow specialist on the book, funding and open interest. Empty is the analyst alone.
    team: tuple[str, ...] = ()
    # [agents].reviewers: the stances asked about a proposal once there is one. Each stance is one
    # more call, so this is empty until an operator asks for it.
    reviewers: tuple[str, ...] = ()
    # [agents].fallback: `provider:model` entries every role inherits, tried in order when the
    # provider above cannot be reached. A rate limit is a dependency failing, not a reason to stop.
    fallback: tuple[str, ...] = ()
    # A declared reviewer that no model answered: refuse the trade (a check you asked for did not
    # happen) or continue on the votes that did come back.
    on_missing_reviewer: str = "refuse"
    # How long a role waits for a provider that said "try again in N seconds" before the next link.
    max_retry_wait_s: float = 15.0

    def for_role(self, role: str) -> RoleModel:
        """What this role is asked with: its own model where declared, otherwise the default."""
        table = self.roles.get(role, {})
        return RoleModel(
            role=role,
            provider=str(table.get("provider", self.provider)),
            model=(str(table["model"]) if table.get("model") else self.model),
            base_url=(str(table["base_url"]) if table.get("base_url") else self.base_url),
            max_output_tokens=(
                int(table["max_output_tokens"])
                if table.get("max_output_tokens") is not None
                else self.max_output_tokens
            ),
            call_timeout_s=int(table.get("call_timeout_s", self.call_timeout_s)),
            max_retries=int(table.get("max_retries", self.max_retries)),
            fallback=tuple(str(name) for name in (table.get("fallback") or self.fallback)),
            effort=(
                str(table["effort"])
                if table.get("effort")
                # A role that reads a table code computed does not need to think long. Only where
                # an effort is declared at all: a model with no reasoning setting refuses the field.
                else (LOW_EFFORT if role in TABLE_READERS and self.effort else self.effort)
            ),
        )


# Roles whose brief is a table of figures code already computed.
TABLE_READERS = ("reviewer",)


# The readings: code reads the operator's series against the operator's thresholds. The `readings`
# package owns these names; they are repeated here because the gates never import it.
READINGS = ("macro", "onchain", "sentiment")


# What the readings were called until 0.11, when each was an analyst role asking a model whose
# label no gate read. A desk written before then runs unchanged: the old name is read as the
# reading it became, and `tra check` names the new one.
RETIRED_READERS = {
    "macro_analyst": "macro",
    "onchain_analyst": "onchain",
    "sentiment_analyst": "sentiment",
}


# What the deciding seats were called until 0.12, when each was named for what it answers for rather
# than for a school of analysis. A desk written before then runs unchanged: the old name is read as
# the seat it became, and `tra check` names the new one.
RENAMED_SEATS = {
    "technical_analyst": "structure_lead",
    "technical_peer": "blind_confirmer",
    "flow_analyst": "flow_specialist",
    "event_analyst": "outcome_assessor",
    "validator": "reviewer",
}
# Seats retired in 0.12 because a written rule does their job, with what does it now. A desk that
# still configures one runs unchanged; `tra check` names the rule.
RETIRED_SEATS = {
    "market_screener": "code orders the universe by the room its paths expect over the spread",
    "risk_manager": "the risk gate and the controls hold the risk in the system",
    "executor": "a research session splits its allocation by an equal-share rule",
}
# Keys renamed with the seat they configure. Read under either name, never both.
RENAMED_KEYS = {
    "validators": "reviewers",
    "on_missing_validator": "on_missing_reviewer",
    "validator_weight": "reviewer_weight",
}
ALIASES = {**RETIRED_READERS, **RENAMED_SEATS}


# Who may join the structure lead. The agents package owns these names; repeated here as the
# readings are.
TEAM = ("blind_confirmer", "flow_specialist")


# The voices conviction can weigh: the seat that proposes, the flow specialist, and the readings.
VOICES = ("structure_lead", "flow_specialist", *READINGS)


LOW_EFFORT = "low"


KNOWN_ROLES = (
    "structure_lead",
    "blind_confirmer",
    "flow_specialist",
    "auditor",
    # One stance is one call, so a reviewer may run on its own model like any other role.
    "reviewer",
    # The deciding role on a prediction market outcome.
    "outcome_assessor",
)


# The llm layer owns this vocabulary; it is repeated here because the gates never import it, and
# repeated in one place so a bad spelling is refused while reading the file rather than mid-cycle.
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")


ROLE_KEYS = (
    "provider",
    "model",
    "base_url",
    "max_output_tokens",
    "call_timeout_s",
    "max_retries",
    # `provider:model` entries tried in order when the one above cannot be reached.
    "fallback",
    # minimal | low | medium | high | xhigh | max, mapped onto whatever the vendor takes.
    "effort",
)


def _known(names: Any, allowed: tuple[str, ...], where: str) -> tuple[str, ...]:
    """Names from `allowed`, a name from before 0.11 or 0.12 read as what it became."""
    named = tuple(ALIASES.get(str(name), str(name)) for name in names)
    unknown = sorted(set(named) - set(allowed))
    if unknown:
        raise ConfigError(f"{where}: {unknown} is not one of {list(allowed)}")
    for name in named:
        if named.count(name) > 1:
            raise ConfigError(f"{where} names {name} twice")
    return named


def current_keys(table: Mapping[str, Any], where: str) -> dict[str, Any]:
    """The table with every key under its current name; one setting written twice is refused."""
    out: dict[str, Any] = {}
    for key, value in table.items():
        named = RENAMED_KEYS.get(key, key)
        if named in out:
            raise ConfigError(f"{where} names {named} twice, once as {key}")
        out[named] = value
    return out


def _readings(table: Mapping[str, Any]) -> tuple[str, ...]:
    """`[agents].readings`, or the `[agents].analysts` a desk wrote before 0.11, when named."""
    if "analysts" in table and "readings" in table:
        raise ConfigError("[agents] names the readings in readings or in analysts, one of the two")
    raw = table.get("readings", table.get("analysts"))
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ConfigError("[agents].readings is a list of reading names")
    # The roster before 0.11 also named the analyst that is now always asked.
    named = [name for name in raw if ALIASES.get(name, name) != "structure_lead"]
    return _known(named, READINGS, "[agents].readings")


def _team(raw: Any) -> tuple[str, ...]:
    """`[agents].team`: who answers beside the structure lead."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ConfigError("[agents].team is a list of role names")
    return _known(raw, TEAM, "[agents].team")


def renamed_in(raw: Mapping[str, Any]) -> tuple[str, ...]:
    """What a desk written for an earlier release still says, and what to write instead; empty
    when current."""
    agents: dict[str, Any] = raw["agents"] if isinstance(raw.get("agents"), dict) else {}
    notes: list[str] = []
    seat = (raw.get("strategy") or {}).get("agent")
    if seat in RENAMED_SEATS:
        notes.append(f"[strategy].agent = {seat!r} is {RENAMED_SEATS[seat]!r} since 0.12")
    if "analysts" in agents:
        notes.append("[agents].analysts is [agents].readings since 0.11")
    if "reuse_reading_for" in agents:
        notes.append("[agents].reuse_reading_for does nothing since 0.11: no reading asks a model")
    roles: dict[str, Any] = agents["roles"] if isinstance(agents.get("roles"), dict) else {}
    notes.extend(
        f"[agents.roles.{role}] does nothing since 0.11: the {RETIRED_READERS[role]} reading asks "
        "no model"
        for role in roles
        if role in RETIRED_READERS
    )
    notes.extend(
        f"[agents.roles.{role}] is [agents.roles.{RENAMED_SEATS[role]}] since 0.12"
        for role in roles
        if role in RENAMED_SEATS
    )
    notes.extend(
        f"[agents.roles.{role}] does nothing since 0.12: {RETIRED_SEATS[role]}"
        for role in roles
        if role in RETIRED_SEATS
    )
    notes.extend(_retired_flags(raw))
    named: list[tuple[str, Any]] = [
        ("[agents].readings", agents.get("readings") or agents.get("analysts")),
        ("[agents].team", agents.get("team")),
        ("[confluence].required", (raw.get("confluence") or {}).get("required")),
        ("[conviction].weights", (raw.get("conviction") or {}).get("weights")),
    ]
    for where, names in named:
        notes.extend(_renamed_names(where, names or ()))
    tables = (("[agents]", agents), ("[conviction]", raw.get("conviction") or {}))
    notes.extend(
        f"{where}.{key} is {where}.{RENAMED_KEYS[key]} since 0.12"
        for where, table in tables
        for key in table
        if key in RENAMED_KEYS
    )
    return tuple(notes)


def _retired_flags(raw: Mapping[str, Any]) -> list[str]:
    said = []
    if (raw.get("universe") or {}).get("screener"):
        said.append(
            f"[universe].screener does nothing since 0.12: {RETIRED_SEATS['market_screener']}"
        )
    if "risk_manager" in (raw.get("audit") or {}):
        said.append(
            f"[audit].risk_manager does nothing since 0.12: {RETIRED_SEATS['risk_manager']}"
        )
    return said


def _renamed_names(where: str, names: Any) -> list[str]:
    said = []
    for name in names:
        if name in RETIRED_READERS:
            said.append(f"{where}: {name} is {RETIRED_READERS[name]} since 0.11")
        elif name in RENAMED_SEATS:
            said.append(f"{where}: {name} is {RENAMED_SEATS[name]} since 0.12")
    return said


def _roles(raw: Any) -> dict[str, dict[str, Any]]:
    """`[agents.roles.<role>]` tables, checked against the roles this package actually asks."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("[agents.roles] holds one table per role")
    out: dict[str, dict[str, Any]] = {}
    for named, table in raw.items():
        if named in RETIRED_READERS or named in RETIRED_SEATS:
            # The reading asks no model, so the table configures nothing; `tra check` says so.
            continue
        role = RENAMED_SEATS.get(named, named)
        if role in out:
            raise ConfigError(f"[agents.roles] names {role} twice, once as {named}")
        if role not in KNOWN_ROLES:
            raise ConfigError(f"[agents.roles.{role}] is not a role; have {list(KNOWN_ROLES)}")
        if not isinstance(table, dict):
            raise ConfigError(f"[agents.roles.{role}] is a table")
        unknown = sorted(set(table) - set(ROLE_KEYS))
        if unknown:
            raise ConfigError(
                f"[agents.roles.{role}]: {unknown} is not settable; {list(ROLE_KEYS)}"
            )
        _effort(table.get("effort"))
        out[role] = dict(table)
    return out


def _effort(raw: Any) -> str | None:
    """`effort`, where a vendor takes one. Undeclared leaves the vendor's own default alone."""
    if raw is None:
        return None
    if not isinstance(raw, str) or raw not in EFFORTS:
        raise ConfigError(f"effort is one of {list(EFFORTS)}")
    return raw


AGENTS_KEYS = (
    "provider",
    "model",
    "base_url",
    "mechanism",
    "pricing",
    "min_confidence",
    "token_budget",
    "daily_token_budget",
    "daily_cost_budget",
    "max_calls_per_hour",
    "min_interval_s",
    "call_timeout_s",
    "lookback_bars",
    "decide_every_bars",
    "max_output_tokens",
    "max_retries",
    "max_retry_wait_s",
    "effort",
    "magnitude_bps",
    "screen",
    "roles",
    "readings",
    "team",
    # What a desk wrote before 0.11; read, and named by `tra check`.
    "analysts",
    "reuse_reading_for",
    "reviewers",
    "on_missing_reviewer",
    "fallback",
)


def _agents(raw: dict[str, Any], strategy_raw: dict[str, Any]) -> AgentSettings | None:
    role = strategy_raw.get("agent")
    if role is None:
        return None
    if RENAMED_SEATS.get(str(role), role) != "structure_lead":
        raise ConfigError("[strategy].agent names a decision role; only structure_lead decides")
    if strategy_raw.get("file") or strategy_raw.get("import"):
        raise ConfigError("[strategy] names either a scripted strategy or an agent, not both")
    table = raw.get("agents")
    if not isinstance(table, dict):
        raise ConfigError("[agents] is required when [strategy].agent is set")
    table = current_keys(table, "[agents]")
    # A misspelt budget is a budget that is not there; it is refused rather than ignored.
    unknown = sorted(set(table) - set(AGENTS_KEYS))
    _no_secrets(unknown, "[agents]")
    if unknown:
        raise ConfigError(f"[agents]: unknown {unknown}; have {list(AGENTS_KEYS)}")
    provider = str(table.get("provider", ""))
    if not provider:
        raise ConfigError("[agents].provider names the model provider")
    mechanism = str(table.get("mechanism", "")).strip()
    if not mechanism:
        raise ConfigError("[agents].mechanism states what the analyst reasons from")
    pricing: dict[str, tuple[Decimal, Decimal]] = {}
    for model_name, row in (table.get("pricing") or {}).items():
        if not isinstance(row, dict):
            raise ConfigError("[agents.pricing.<model>] is a table")
        pricing[str(model_name)] = (_dec(row, "input_per_million"), _dec(row, "output_per_million"))
    confidence = table.get("min_confidence", 0.5)
    if isinstance(confidence, bool) or not isinstance(confidence, int | float | Decimal):
        raise ConfigError("[agents].min_confidence is a number in [0, 1]")
    return AgentSettings(
        role=str(role),
        provider=provider,
        model=str(table["model"]) if table.get("model") else None,
        base_url=str(table["base_url"]) if table.get("base_url") else None,
        mechanism=mechanism,
        token_budget=_int(table, "token_budget") if "token_budget" in table else None,
        daily_token_budget=(
            _int(table, "daily_token_budget") if "daily_token_budget" in table else None
        ),
        daily_cost_budget=(
            _dec(table, "daily_cost_budget") if "daily_cost_budget" in table else None
        ),
        max_calls_per_hour=(
            _int(table, "max_calls_per_hour") if "max_calls_per_hour" in table else None
        ),
        min_interval_s=int(table.get("min_interval_s", 0)),
        call_timeout_s=int(table.get("call_timeout_s", 60)),
        lookback_bars=int(table.get("lookback_bars", 60)),
        min_confidence=float(confidence),
        pricing=pricing,
        decide_every_bars=int(table.get("decide_every_bars", 1)),
        max_output_tokens=(
            int(table["max_output_tokens"]) if table.get("max_output_tokens") is not None else None
        ),
        max_retries=int(table.get("max_retries", 2)),
        effort=_effort(table.get("effort")),
        magnitude_bps=_dec(table, "magnitude_bps") if "magnitude_bps" in table else None,
        screen=_screen(table.get("screen")),
        roles=_roles(table.get("roles")),
        readings=_readings(table),
        team=_team(table.get("team")),
        reviewers=_reviewers(table.get("reviewers")),
        on_missing_reviewer=_choice(
            table, "on_missing_reviewer", ("refuse", "continue"), "refuse", "[agents]"
        ),
        max_retry_wait_s=float(table.get("max_retry_wait_s", 15.0)),
        fallback=_fallback(table.get("fallback")),
    )


def _fallback(raw: Any) -> tuple[str, ...]:
    """`fallback = ["groq:openai/gpt-oss-20b", ...]`: other models, in the order to try them."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ConfigError("fallback is a list of provider:model strings")
    names = [str(name) for name in raw]
    for name in names:
        # The shape only: this layer knows nothing about which providers exist, and the CLI checks
        # the names against the registry where both are in reach.
        provider_name, _, model = name.partition(":")
        if not provider_name.strip() or not model.strip():
            raise ConfigError(f"{name!r} is not a fallback; write it as provider:model")
    if len(set(names)) != len(names):
        raise ConfigError("a fallback names each model once")
    return tuple(names)


def _reviewers(raw: Any) -> tuple[str, ...]:
    """`[agents].reviewers`: the stances that argue about a proposal before the votes are read."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ConfigError("[agents].reviewers is a list of stances")
    names = [str(name) for name in raw]
    unknown = sorted(set(names) - set(STANCES))
    if unknown:
        raise ConfigError(f"[agents].reviewers: {unknown} is not a stance; have {list(STANCES)}")
    if len(set(names)) != len(names):
        raise ConfigError("[agents].reviewers asks each stance once")
    return tuple(names)


def _screen(raw: Any) -> dict[str, float]:
    """[agents.screen]: the screener's two thresholds, both the operator's."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("[agents.screen] is a table")
    out: dict[str, float] = {}
    for key in ("min_prints_per_minute", "volatile_above_bps"):
        if key in raw:
            out[key] = float(_dec(raw, key))
    unknown = sorted(set(raw) - set(out))
    if unknown:
        raise ConfigError(f"[agents.screen]: unknown {unknown}")
    return out
