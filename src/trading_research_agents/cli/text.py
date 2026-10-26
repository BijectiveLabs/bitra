"""`tra text judge`: the archived headlines of a range of days, read by the declared typed judge.

What it prints is what the features would read: how many headlines were judged, how many were
dropped as instructions to a machine, how many the judge could not answer, and how many of each
kind of event were reported at a probability of at least one half. Verdicts go through the judge's
cache under `<out>/judge`, so a replay of the same days asks nothing.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from trading_research_agents.agents.briefs import base_of
from trading_research_agents.llm.cache import CacheMissError, CacheMode
from trading_research_agents.llm.judge import Judge, JudgeUnauthorizedError
from trading_research_agents.market_data.archive import TEXT
from trading_research_agents.market_data.bars import MarketDataError
from trading_research_agents.market_data.record_config import ALL, load_record_config
from trading_research_agents.market_data.text import TEXT_VENUE, read_text_range
from trading_research_agents.perception.config import (
    JudgeConfig,
    JudgeConfigError,
    build_judge,
    load_judge_config,
)
from trading_research_agents.perception.text_events import Perceived, perceive

Report = tuple[dict[str, Any], list[str], int]
Build = Callable[[JudgeConfig, Path], Judge]
REPORTED = 0.5


def add_text_command(top: Any) -> None:
    cmd = top.add_parser("text", help="the archived headlines, read by the declared typed judge")
    sub = cmd.add_subparsers(dest="command", required=True)
    judge = sub.add_parser("judge", help="judge the archived headlines of a range of days")
    judge.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    judge.add_argument("--to", dest="end", type=date.fromisoformat, default=None)
    judge.add_argument("--feed", action="append", default=None, help="a feed; all when left out")
    judge.add_argument("--data-config", type=Path, default=Path("data.toml"))
    judge.add_argument("--out", type=Path, default=Path("data"), help="the data directory")
    judge.add_argument("--json", action="store_true")


def text_judge(args: argparse.Namespace, *, judge: Build = build_judge) -> Report:
    try:
        cfg = load_judge_config(args.data_config)
    except JudgeConfigError as exc:
        return {"error": str(exc)}, [str(exc)], 1
    if cfg is None:
        why = f"{args.data_config}: no [judge] table, so no headline is judged"
        return {"error": why}, [why], 1
    try:
        record = load_record_config(args.data_config)
    except MarketDataError as exc:
        return {"error": str(exc)}, [str(exc)], 1
    assets = list(dict.fromkeys(base_of(name) for name in record.markets))
    feeds = args.feed or _feeds(args.out, record.text)
    items = [
        item
        for feed in feeds
        for item in read_text_range(args.out, feed, args.start, args.end or args.start)
    ]
    try:
        seen = perceive(
            judge(cfg, args.out / "judge"),
            items,
            assets=assets,
            min_confidence=cfg.min_confidence,
        )
    except (JudgeUnauthorizedError, CacheMissError) as exc:
        return {"error": str(exc)}, [f"stopped: {exc}"], 1
    return _report(cfg, assets, feeds, seen)


def judge_lines(path: Path, env: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """What `tra check` says about the judge: which one, pinned, and whether it can answer."""
    try:
        cfg = load_judge_config(path)
    except JudgeConfigError as exc:
        return [], [f"judge: {exc}"]
    if cfg is None:
        return [], []
    if cfg.cache is not CacheMode.REPLAY and not env.get(cfg.key_env):
        return [], [f"judge: {cfg.key_env} is not set; the text features are absent until it is"]
    named = f"{cfg.provider} {cfg.model}, cache {cfg.cache.value}"
    return [f"judge: {named}; text features read its verdicts"], []


def _feeds(out: Path, declared: Sequence[str]) -> list[str]:
    if declared and declared != (ALL,):
        return list(declared)
    folder = out / "market" / TEXT_VENUE
    return (
        sorted(p.name for p in folder.iterdir() if (p / TEXT).is_dir()) if folder.is_dir() else []
    )


def _report(cfg: JudgeConfig, assets: list[str], feeds: list[str], seen: Perceived) -> Report:
    kinds = Counter(
        kind
        for event in seen.events
        for kind, p in event.kinds.items()
        if kind != "none" and p >= REPORTED
    )
    payload = {
        "model": cfg.model,
        "assets": assets,
        "feeds": feeds,
        "judged": len(seen.events),
        "guarded": seen.guarded,
        "absent": list(seen.absent),
        "kinds": dict(sorted(kinds.items())),
    }
    lines = [
        f"{cfg.model}: {len(seen.events)} judged, {seen.guarded} dropped as instructions to a "
        f"machine, {len(seen.absent)} not answered; assets {', '.join(assets) or 'none'}",
        *(f"  {kind:<12} {count}" for kind, count in sorted(kinds.items())),
        *(f"  not answered: {why}" for why in seen.absent),
    ]
    return payload, lines, 0


__all__ = ["add_text_command", "judge_lines", "text_judge"]
