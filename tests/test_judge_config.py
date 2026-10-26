from __future__ import annotations

from pathlib import Path

import pytest

from trading_research_agents.llm.cache import CacheMode
from trading_research_agents.llm.judge import CachedJudge
from trading_research_agents.perception.config import (
    JudgeConfig,
    JudgeConfigError,
    build_judge,
    load_judge_config,
)


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "data.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_no_judge_table_means_no_judge(tmp_path: Path) -> None:
    assert load_judge_config(write(tmp_path, '[data.ohlcv]\nchain = ["ccxt"]\n')) is None


def test_a_judge_is_a_pinned_model_with_its_floor_and_its_cache(tmp_path: Path) -> None:
    cfg = load_judge_config(
        write(tmp_path, '[judge]\nprovider = "typesafe"\nmodel = "jev-1.14.0"\ncache = "replay"\n')
    )
    assert cfg == JudgeConfig(provider="typesafe", model="jev-1.14.0", cache=CacheMode.REPLAY)
    assert cfg.min_confidence == 0.5 and cfg.key_env == "TYPESAFE_API_KEY"
    judge = build_judge(cfg, tmp_path / "cache", env={})
    assert isinstance(judge, CachedJudge) and judge.model == "jev-1.14.0"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[judge]\nprovider = "typesafe"\n', "model names a pinned version"),
        ('[judge]\nprovider = "oracle"\nmodel = "x-1.0.0"\n', "is not a judge this package has"),
        ('[judge]\nprovider = "typesafe"\nmodel = "jev-latest"\n', "pinned"),
        ('[judge]\nprovider = "typesafe"\nmodel = "jev-1.14.0"\nmin_confidence = 2\n', "0 and 1"),
        ('[judge]\nprovider = "typesafe"\nmodel = "jev-1.14.0"\ncache = "maybe"\n', "cache is"),
        ('[judge]\nprovider = "typesafe"\nmodel = "jev-1.14.0"\nmodle = "x"\n', "did you mean"),
        ('[judge]\nprovider = "typesafe"\nmodel = "jev-1.14.0"\napi_key = "x"\n', "environment"),
    ],
)
def test_a_judge_table_that_cannot_be_used_is_refused(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(JudgeConfigError, match=message):
        load_judge_config(write(tmp_path, body))
