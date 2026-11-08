"""The event guard on a rule's new entry: the typed judge reads the headlines, the system decides.

The judge is asked only closed questions over one headline at a time, which it is built for. The
system carries every part the vendor lists as a weak point: it chooses which headlines are recent
by when it first saw them, so the judge never compares dates; it sends the headline and its
publisher and nothing else; it asks the kind in two orders against a first-option bias; it screens
for instructions addressed to a machine; and it does the arithmetic that turns probabilities into
a weight and compares the weight with the operator's threshold.

Every headline read is archived as first seen, so the reading can be replayed without a key.
"""

from __future__ import annotations

from typing import Any

from trading_research_agents.agents.briefs import base_of
from trading_research_agents.agents.cycle.briefing import _calendar
from trading_research_agents.agents.cycle.record import _finish
from trading_research_agents.agents.cycle.state import Pass
from trading_research_agents.agents.progress import DONE, REFUSED, announce
from trading_research_agents.llm.judge import JudgeUnauthorizedError
from trading_research_agents.perception.guard import first_seen_within, heaviest
from trading_research_agents.perception.text_events import GUARD, perceive

FEED = "desk"
NS_PER_S = 1_000_000_000
STEP = "events"


def guard_the_entry(p: Pass) -> bool:
    """Record the guard's reading; in hold mode, an adverse event or an unread headline holds."""
    cfg, result = p.cfg, p.result
    guard = cfg.event_guard
    if guard is None:
        return True
    now_ns = int(cfg.clock().timestamp() * NS_PER_S)
    recent = _recent(p, now_ns, guard.window_h)
    said: dict[str, Any] = {
        **guard.record(),
        "min_confidence": cfg.judge_min_confidence,
        "instructions_at": GUARD,
        "headlines": len(recent),
    }
    asset = base_of(cfg.instrument)
    reason = ""
    if cfg.judge is None:
        reason = "no judge declared in data.toml [judge], so the headlines were not read"
    elif recent:
        try:
            seen = perceive(
                cfg.judge, recent, assets=[asset], min_confidence=cfg.judge_min_confidence
            )
        except JudgeUnauthorizedError as exc:
            seen = None
            reason = f"the judge refused the key: {exc}"
        if seen is not None:
            said.update(_reading(seen, asset, guard.kinds))
            unread = len(seen.absent)
            if unread:
                reason = f"{unread} of {len(recent)} headline(s) the judge could not read"
    said["would_hold"] = float(said.get("weight", 0.0)) >= guard.hold_at
    said["model"] = getattr(cfg.judge, "model", None)
    if said["would_hold"]:
        reason = (
            f"{said['kind']} at {said['weight']:.2f} against hold_at {guard.hold_at}: "
            f"{said['headline']}"
        )
    elif reason:
        said["unread"] = reason
    result.briefs["event_guard"] = said
    if guard.holds and (said["would_hold"] or said.get("unread")):
        # A guard that is declared to hold and could not read is not a guard that saw nothing.
        result.refusals.append(f"events: {reason}")
        announce(cfg.watch, STEP, REFUSED, reason)
        _finish(cfg, p.where, result)
        return False
    detail = f"{len(recent)} headline(s); " + (
        f"would hold: {reason}" if said["would_hold"] else "nothing that holds"
    )
    announce(cfg.watch, STEP, DONE, detail)
    return True


def _recent(p: Pass, now_ns: int, window_h: int) -> list[Any]:
    events = _calendar(p.cfg, p.result)
    return first_seen_within(p.cfg.text_root, FEED, events, now_ns=now_ns, window_h=window_h)


def _reading(seen: Any, asset: str, kinds: tuple[str, ...]) -> dict[str, Any]:
    found = heaviest(seen.events, asset, kinds)
    return {
        "judged": len(seen.events),
        "absent": len(seen.absent),
        "dropped_as_instructions": seen.guarded,
        "kinds_disagreed": seen.disagreed,
        "weight": round(found.weight, 4),
        "kind": found.kind,
        "headline": None if found.event is None else found.event.text,
    }
