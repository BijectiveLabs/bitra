from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from trading_research_agents.llm.cache import (
    CachedCompleter,
    CacheError,
    CacheMissError,
    CacheMode,
    CallIdentity,
    ResponseCache,
    Sampling,
    brief_sha256,
    cache_key,
)
from trading_research_agents.llm.completer import FakeCompleter

ID = CallIdentity("strategist", "fake", "fake", "1", Sampling(temperature=0.7, seed=3))
SCHEMA = {"type": "object"}


def ask(completer: CachedCompleter, user: str = "u") -> str:
    return completer.complete(system="s", user=user, schema=SCHEMA).text


def test_record_then_replay_gives_the_same_answer(tmp_path: Path) -> None:
    inner = FakeCompleter(['{"side": "long"}'])
    cache = ResponseCache(tmp_path)
    first = ask(CachedCompleter(inner, cache, ID, mode=CacheMode.RECORD))
    again = ask(CachedCompleter(FakeCompleter([]), cache, ID, mode=CacheMode.REPLAY))
    assert first == again == '{"side": "long"}' and len(inner.calls) == 1
    key = cache_key(ID, brief_sha256("s", "u", SCHEMA))
    assert (tmp_path / key[:2] / f"{key}.json").is_file()


def test_replay_never_calls_the_model(tmp_path: Path) -> None:
    inner = FakeCompleter(["x"])
    replay = CachedCompleter(inner, ResponseCache(tmp_path), ID, mode=CacheMode.REPLAY)
    with pytest.raises(CacheMissError, match="replay never calls a model"):
        ask(replay)
    assert inner.calls == []


def test_record_keeps_the_answer_it_already_holds(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    ask(CachedCompleter(FakeCompleter(["first"]), cache, ID, mode=CacheMode.RECORD))
    second = FakeCompleter(["second"])
    assert ask(CachedCompleter(second, cache, ID, mode=CacheMode.RECORD)) == "first"
    assert second.calls == []


def test_off_always_calls_and_keeps_nothing(tmp_path: Path) -> None:
    off = CachedCompleter(
        FakeCompleter(["a", "b"]), ResponseCache(tmp_path), ID, mode=CacheMode.OFF
    )
    assert [ask(off), ask(off)] == ["a", "b"] and not any(tmp_path.iterdir())


def test_every_part_of_the_identity_changes_the_key() -> None:
    brief = brief_sha256("s", "u", SCHEMA)
    variants = [
        replace(ID, role="critic"),
        replace(ID, model="other"),
        replace(ID, prompt_version="2"),
        replace(ID, sample_index=1),
        replace(ID, sampling=Sampling(temperature=0.2, seed=3)),
    ]
    keys = {cache_key(ID, brief), *(cache_key(v, brief) for v in variants)}
    assert len(keys) == 6
    assert cache_key(ID, brief_sha256("s", "other", SCHEMA)) != cache_key(ID, brief)


def test_an_entry_that_no_longer_matches_its_key_is_refused(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    ask(CachedCompleter(FakeCompleter(["a"]), cache, ID, mode=CacheMode.RECORD))
    key = cache_key(ID, brief_sha256("s", "u", SCHEMA))
    path = cache.path(key)
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["request"]["user"] = "edited"
    path.write_text(json.dumps(entry), encoding="utf-8")
    with pytest.raises(CacheError, match="does not match its key"):
        cache.get(key)
