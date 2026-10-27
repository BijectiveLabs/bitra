"""Writing a cycle down, and sending its intent in venue mode."""

from __future__ import annotations

from pathlib import Path

from trading_research_agents.agents.cycle.config import CycleConfig
from trading_research_agents.agents.cycle.result import CycleResult
from trading_research_agents.agents.progress import (
    DONE,
    FAILED,
    REFUSED,
    RUNNING,
    announce,
)
from trading_research_agents.llm.usage import BudgetSpentError
from trading_research_agents.records import (
    append_index,
    build_index,
    has_index,
    index_row,
    sha256_of,
    write_gzip_json,
    write_json,
)
from trading_research_agents.store.cycles import SUMMARY, CycleDir
from trading_research_agents.store.snapshot import (
    INPUTS,
    inputs_record,
)


def _send(cfg: CycleConfig, where: CycleDir, result: CycleResult) -> CycleResult:
    """Send the intent already on disk, in venue mode, and write the cycle down either way."""
    if cfg.mode == "venue" and cfg.place is not None and result.intent is not None:
        # The intent is already on disk. Whatever the venue does next, the record of what this
        # cycle decided survives it.
        announce(cfg.watch, "venue", RUNNING)
        try:
            result.placed = dict(cfg.place(result.intent))
        except Exception as exc:
            result.placed = {"sent": False, "error": f"{exc.__class__.__name__}: {exc}"}
            result.refusals.append(f"venue: {exc.__class__.__name__}: {exc}")
            announce(cfg.watch, "venue", FAILED, exc.__class__.__name__)
        else:
            if result.placed.get("sent"):
                announce(
                    cfg.watch, "venue", DONE, f"order {result.placed.get('order_id') or 'sent'}"
                )
            else:
                said = str(result.placed.get("refused") or result.placed.get("error") or "not sent")
                result.refusals.append(f"venue: not sent, {said}")
                announce(cfg.watch, "venue", REFUSED, said)
            write_json(where.path.joinpath("placed.json"), result.placed, durable=True)
            if result.placed.get("interrupted"):
                result.stopped_by = f"interrupted at the venue: {result.placed.get('note')}"
                _finish(cfg, where, result)
                # The record is on disk and the order is off the book; now the operator gets the
                # stop they asked for, instead of a scheduler that carries on.
                raise KeyboardInterrupt(result.stopped_by)
    _finish(cfg, where, result)
    return result


def _stop_before_asking(
    cfg: CycleConfig, where: CycleDir, result: CycleResult, why: str, step: str = "size"
) -> CycleResult:
    result.refusals.append(why)
    result.stopped_by = why
    announce(cfg.watch, step, REFUSED, why)
    _finish(cfg, where, result)
    return result


def _finish(cfg: CycleConfig, where: CycleDir, result: CycleResult) -> None:
    """Write the cycle down: raw inputs, a hash of every file, the summary, then its index line.

    In that order, so a summary never names a file that is not there yet and an index line never
    points at a summary that was not finished.
    """
    write_gzip_json(
        where.path / INPUTS,
        inputs_record(
            result.snapshot,
            result.frames,
            {
                "interval_s": cfg.interval_s,
                "lookback_bars": cfg.lookback_bars,
                "context_intervals": list(cfg.context_intervals),
                "regime": cfg.regime.record(),
                "mechanism": cfg.mechanism,
            },
        ),
    )
    payload = result.record()
    payload["tokens"] = cfg.usage.tokens
    payload["cost_declared"] = str(cfg.usage.cost)
    payload["finished_at"] = cfg.clock().isoformat()
    payload["files"] = manifest(where.path)
    summary = where.write_summary(payload)
    if not has_index(cfg.root):
        # A desk that ran before the index existed gets one built from its history first, so
        # the counts that read it never start from zero.
        build_index(cfg.root)
    oi = result.snapshot.values.get("open_interest")
    append_index(
        cfg.root,
        index_row(
            payload,
            open_interest=None if oi is None else oi.record(),
            summary_sha256=sha256_of(summary),
        ),
    )


def manifest(folder: Path) -> dict[str, str]:
    """A sha256 per file a cycle wrote, by its path inside the cycle; the summary hashes itself
    into the index instead."""
    return {
        path.relative_to(folder).as_posix(): sha256_of(path)
        for path in sorted(folder.rglob("*"))
        if path.is_file() and path.name != SUMMARY and not path.name.startswith(".")
    }


def budget_spent(error: str) -> bool:
    """Whether a failure was the budget refusing, which ends a cycle rather than an agent."""
    return BudgetSpentError.__name__ in error
