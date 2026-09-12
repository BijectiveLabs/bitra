"""The run: an idea through layers 0, 2 and 3, with every step written down.

Two kinds of rejection, kept apart on purpose. A defect of form goes back to the model with
the gate's own error text, up to a fixed number of revisions. A fact about the idea, that its
bound is below the cost floor or that nobody can be named who is forced to lose, is final.
Returning the second kind would invite the model to raise a number until it clears.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agent.agent.completer import Completer
from trading_research_agent.agent.roles import (
    BOUND_SCHEMA,
    CRITIQUE_SCHEMA,
    MEMO_SCHEMA,
    REGISTRATION_SCHEMA,
    Bound,
    BoundBasis,
    Critique,
    Memo,
    RoleError,
    parse_bound,
    parse_critique,
    parse_memo,
    parse_registration,
    prompt,
)
from trading_research_agent.agent.state import Exchange, Outcome, Refusal, RunResult
from trading_research_agent.chain.floor import cost_floor
from trading_research_agent.chain.preregistration import (
    Preregistration,
    to_record,
    validate_mechanism,
)
from trading_research_agent.envelope.model import Envelope
from trading_research_agent.ledger.entry import LedgerEntry
from trading_research_agent.ledger.store import Ledger
from trading_research_agent.ledger.vocabulary import Axis, Verdict

SUBJECT_LENGTH = 120


@dataclass(frozen=True)
class RunConfig:
    envelope: Envelope
    completer: Completer
    venue: str | None = None
    magnitude_bps: Decimal | None = None
    max_revisions: int = 2
    ledger: Ledger | None = None
    refusals: Path | None = None
    runs_dir: Path | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    run_id: str | None = None

    def __post_init__(self) -> None:
        if self.max_revisions < 0:
            raise ValueError("max_revisions cannot be negative")
        if self.magnitude_bps is not None and self.magnitude_bps <= 0:
            raise ValueError("an operator magnitude is a positive number of basis points")


class _Run:
    def __init__(self, idea: str, config: RunConfig) -> None:
        if not idea.strip():
            raise ValueError("an idea is required")
        self.idea = idea.strip()
        self.config = config
        self.started_at = config.clock()
        self.run_id = config.run_id or (f"{self.started_at:%Y%m%dT%H%M%S}-{secrets.token_hex(3)}")
        self.exchanges: list[Exchange] = []

    def ask(self, role: str, user: str, schema: dict[str, Any]) -> str:
        system = prompt(role)
        completion = self.config.completer.complete(system=system, user=user, schema=schema)
        self.exchanges.append(
            Exchange(
                sequence=len(self.exchanges) + 1,
                role=role,
                system=system,
                user=user,
                schema=schema,
                response=completion.text,
                model=completion.model,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                at=self.config.clock(),
            )
        )
        return completion.text

    # Layer 0

    def bound(self) -> Bound | Refusal:
        if self.config.magnitude_bps is not None:
            return Bound(self.config.magnitude_bps, BoundBasis.DECLARED, "declared by the operator")
        feedback = ""
        last = "no bound was given"
        for _ in range(self.config.max_revisions + 1):
            user = f"Idea:\n{self.idea}\n" + (
                f"\nRevise. Previous attempt: {feedback}\n" if feedback else ""
            )
            try:
                bound = parse_bound(self.ask("bound", user, BOUND_SCHEMA))
            except RoleError as exc:
                feedback, last = str(exc), str(exc)
                continue
            if bound.usable:
                return bound
            feedback = f"no basis given ({bound.explanation}); use one of the three sources"
            last = f"no bound could be given: {bound.explanation}"
        return self.refuse(layer=0, revisions=self.config.max_revisions, reason=last)

    # Layer 2

    def memo(self) -> Memo | Refusal:
        feedback = ""
        last = "no memo was produced"
        for _ in range(self.config.max_revisions + 1):
            user = f"Idea:\n{self.idea}\n" + (
                f"\nRevise. Objection: {feedback}\n" if feedback else ""
            )
            try:
                memo = parse_memo(self.ask("memo", user, MEMO_SCHEMA))
                validate_mechanism(memo.mechanism)
            except (RoleError, ValueError) as exc:
                feedback, last = str(exc), str(exc)
                continue
            critique = self.critique(memo)
            if critique.accepted:
                return memo
            feedback, last = critique.objection, critique.objection
        return self.refuse(layer=2, revisions=self.config.max_revisions, reason=last)

    def critique(self, memo: Memo) -> Critique:
        user = (
            f"Idea:\n{self.idea}\n\nMechanism memo:\n{memo.mechanism}\n"
            f"Forced loser: {memo.forced_loser}\nForcing structure: {memo.forcing_structure}\n"
        )
        try:
            return parse_critique(self.ask("critique", user, CRITIQUE_SCHEMA))
        except RoleError as exc:
            # A critic that cannot answer the schema does not get to accept by default.
            return Critique(accepted=False, objection=f"critique unreadable: {exc}")

    # Layer 3

    def register(self, memo: Memo) -> Preregistration | Refusal:
        feedback = ""
        last = "no registration was produced"
        for _ in range(self.config.max_revisions + 1):
            user = f"Idea:\n{self.idea}\n\nAccepted mechanism:\n{memo.mechanism}\n" + (
                f"\nRevise. The registration was refused: {feedback}\n" if feedback else ""
            )
            try:
                return parse_registration(
                    self.ask("registration", user, REGISTRATION_SCHEMA),
                    mechanism=memo.mechanism,
                    registered_at=self.config.clock(),
                )
            except RoleError as exc:
                feedback, last = str(exc), str(exc)
        return self.refuse(layer=3, revisions=self.config.max_revisions, reason=last)

    # Recording

    def refuse(self, *, layer: int, revisions: int, reason: str) -> Refusal:
        refusal = Refusal(
            run_id=self.run_id,
            at=self.config.clock(),
            layer=layer,
            revisions=revisions,
            reason=reason,
            idea=self.idea,
        )
        if self.config.refusals is not None:
            self.config.refusals.parent.mkdir(parents=True, exist_ok=True)
            with self.config.refusals.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_refusal_record(refusal), sort_keys=True) + "\n")
        return refusal

    def finish(
        self,
        outcome: Outcome,
        *,
        floor: Any = None,
        ledger_entry_id: str | None = None,
        preregistration: Preregistration | None = None,
        refusal: Refusal | None = None,
    ) -> RunResult:
        result = RunResult(
            run_id=self.run_id,
            outcome=outcome,
            idea=self.idea,
            started_at=self.started_at,
            finished_at=self.config.clock(),
            exchanges=tuple(self.exchanges),
            floor=floor,
            ledger_entry_id=ledger_entry_id,
            preregistration=preregistration,
            refusal=refusal,
        )
        if self.config.runs_dir is not None:
            _write_run(self.config.runs_dir / self.run_id, result)
        return result


def propose(idea: str, config: RunConfig) -> RunResult:
    """Drive one idea through layers 0, 2 and 3. Returns a record; never raises for a gate's no."""
    run = _Run(idea, config)

    bound = run.bound()
    if isinstance(bound, Refusal):
        return run.finish(Outcome.REFUSED_LAYER_0, refusal=bound)
    if bound.magnitude_bps is None:  # pragma: no cover - usable() guarantees this
        raise RuntimeError("usable bound without a magnitude")

    check = cost_floor(config.envelope, bound.magnitude_bps, venue=config.venue)
    if check.stopped:
        entry_id = None
        if config.ledger is not None:
            entry = check.to_entry(
                entry_id=run.run_id,
                subject=run.idea[:SUBJECT_LENGTH],
                recorded_at=config.clock(),
                envelope=config.envelope,
            )
            entry = replace(
                entry, note=f"{entry.note}; bound basis {bound.basis.value}: {bound.explanation}"
            )
            config.ledger.append(entry)
            entry_id = entry.entry_id
        return run.finish(Outcome.STOPPED_AT_FLOOR, floor=check, ledger_entry_id=entry_id)

    memo = run.memo()
    if isinstance(memo, Refusal):
        return run.finish(Outcome.REFUSED_LAYER_2, floor=check, refusal=memo)

    registration = run.register(memo)
    if isinstance(registration, Refusal):
        return run.finish(Outcome.REFUSED_LAYER_3, floor=check, refusal=registration)

    entry_id = None
    if config.ledger is not None:
        seal = registration.seal()
        entry = LedgerEntry(
            entry_id=run.run_id,
            subject=registration.name,
            verdict=Verdict.OPEN,
            recorded_at=config.clock(),
            layer=3,
            blocking_axis=Axis.POWER,
            evidence=(f"preregistration:{seal}",),
            envelope=config.envelope.snapshot(),
            note=(
                f"registered; 0 of {registration.power.required_sample} samples; "
                f"bound {bound.magnitude_bps} bps ({bound.basis.value})"
            ),
        )
        config.ledger.append(entry)
        entry_id = entry.entry_id
    return run.finish(
        Outcome.REGISTERED, floor=check, ledger_entry_id=entry_id, preregistration=registration
    )


def _refusal_record(r: Refusal) -> dict[str, Any]:
    return {
        "run_id": r.run_id,
        "at": r.at.isoformat(),
        "layer": r.layer,
        "revisions": r.revisions,
        "reason": r.reason,
        "idea": r.idea,
    }


def read_refusals(path: Path) -> list[Refusal]:
    if not path.exists():
        return []
    out: list[Refusal] = []
    with path.open("r", encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                out.append(
                    Refusal(
                        run_id=d["run_id"],
                        at=datetime.fromisoformat(d["at"]),
                        layer=int(d["layer"]),
                        revisions=int(d["revisions"]),
                        reason=d["reason"],
                        idea=d["idea"],
                    )
                )
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                raise ValueError(f"{path}:{number}: unreadable refusal: {exc}") from exc
    return out


def _write_run(directory: Path, result: RunResult) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for e in result.exchanges:
        (directory / f"{e.sequence:02d}-{e.role}.json").write_text(
            json.dumps(
                {
                    "role": e.role,
                    "system": e.system,
                    "user": e.user,
                    "schema": e.schema,
                    "response": e.response,
                    "model": e.model,
                    "input_tokens": e.input_tokens,
                    "output_tokens": e.output_tokens,
                    "at": e.at.isoformat(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    summary: dict[str, Any] = {
        "run_id": result.run_id,
        "outcome": result.outcome.value,
        "idea": result.idea,
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
        "exchanges": len(result.exchanges),
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "models": sorted({e.model for e in result.exchanges}),
        "ledger_entry_id": result.ledger_entry_id,
        "refusal": None if result.refusal is None else _refusal_record(result.refusal),
        "preregistration": None,
    }
    if result.preregistration is not None:
        (directory / "preregistration.json").write_text(
            json.dumps(to_record(result.preregistration), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        summary["preregistration"] = str(directory / "preregistration.json")
    (directory / "run.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
