"""`[judge]` in `data.toml`: the typed judge that reads the archived text, pinned, and its cache.

```toml
[judge]
provider = "typesafe"
model = "jev-1.14.0"            # a version, never an alias that moves
key_env = "TYPESAFE_API_KEY"    # the key comes from the environment, never from this file
min_confidence = 0.5            # below this a kind is absent, not a guess
cache = "record"                # record | replay | off
```

No table means no judge, and the text features say so rather than read as a quiet day.
"""

from __future__ import annotations

import difflib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trading_research_agents.llm.cache import CacheMode
from trading_research_agents.llm.judge import CachedJudge, Judge
from trading_research_agents.llm.providers.typesafe import BASE_URL, KEY_ENV, NAME, TypeSafeJudge

PROVIDERS = (NAME,)
KEYS = ("provider", "model", "key_env", "base_url", "min_confidence", "cache", "timeout_s")
SECRET_WORDS = ("key", "secret", "token", "password")


class JudgeConfigError(ValueError):
    """`[judge]` as written cannot be used."""


@dataclass(frozen=True)
class JudgeConfig:
    provider: str
    model: str
    key_env: str = KEY_ENV
    base_url: str = BASE_URL
    min_confidence: float = 0.5
    cache: CacheMode = CacheMode.RECORD
    timeout_s: float = 3.0


def load_judge_config(path: Path) -> JudgeConfig | None:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise JudgeConfigError(f"{path}: {exc}") from exc
    table = raw.get("judge")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise JudgeConfigError("[judge] is a table")
    return judge_config(table)


def judge_config(table: Mapping[str, Any]) -> JudgeConfig:
    _known_keys(table)
    provider = str(table.get("provider", ""))
    if provider not in PROVIDERS:
        raise JudgeConfigError(
            f"[judge].provider {provider!r} is not a judge this package has; have {PROVIDERS}"
        )
    model = str(table.get("model", "")).strip()
    if not model:
        raise JudgeConfigError("[judge].model names a pinned version, e.g. jev-1.14.0")
    if model.endswith(("-latest", "-preview")):
        raise JudgeConfigError(f"[judge].model {model!r} moves; name a pinned version")
    floor = float(table.get("min_confidence", 0.5))
    if not 0 <= floor <= 1:
        raise JudgeConfigError("[judge].min_confidence is between 0 and 1")
    try:
        cache = CacheMode(str(table.get("cache", CacheMode.RECORD)))
    except ValueError:
        raise JudgeConfigError(f"[judge].cache is one of {[m.value for m in CacheMode]}") from None
    return JudgeConfig(
        provider=provider,
        model=model,
        key_env=str(table.get("key_env", KEY_ENV)),
        base_url=str(table.get("base_url", BASE_URL)),
        min_confidence=floor,
        cache=cache,
        timeout_s=float(table.get("timeout_s", 3.0)),
    )


def _known_keys(table: Mapping[str, Any]) -> None:
    for key in table:
        if key in KEYS:
            continue
        if any(word in key.lower() for word in SECRET_WORDS) and key != "key_env":
            raise JudgeConfigError(
                f"[judge].{key}: a credential is read from the environment, never from this file; "
                "name the variable in key_env"
            )
        close = difflib.get_close_matches(key, KEYS, n=1)
        hint = f" (did you mean {close[0]}?)" if close else ""
        raise JudgeConfigError(f"[judge].{key} is not a setting{hint}; have {list(KEYS)}")


def build_judge(
    cfg: JudgeConfig, cache_root: Path, *, env: Mapping[str, str] | None = None
) -> Judge:
    """The pinned judge behind its cache. The key is read when the first question is asked."""
    inner = TypeSafeJudge(
        cfg.model,
        base_url=cfg.base_url,
        key_env=cfg.key_env,
        timeout_s=cfg.timeout_s,
        env=env,
    )
    return CachedJudge(inner, cache_root, mode=cfg.cache)


__all__ = ["JudgeConfig", "JudgeConfigError", "build_judge", "judge_config", "load_judge_config"]
