"""What an agent can reach: the files a tool opens, the calls a run makes, what a record keeps."""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_research_agents.agents.react import run_tool_loop
from trading_research_agents.agents.validator import role as validator_role
from trading_research_agents.llm.completer import FakeCompleter, ProviderError, redact
from trading_research_agents.llm.form import FormError
from trading_research_agents.llm.usage import (
    BudgetLimits,
    BudgetSpentError,
    MeteredCompleter,
    RateWindow,
    UsageLedger,
    UsageRecord,
)
from trading_research_agents.surface.confine import (
    OutsideRootError,
    confine_all,
    confined,
    inside,
    roots_of,
)
from trading_research_agents.trading.dispatch import VenueHand


def read_ledger(ledger_path: str) -> dict[str, Any]:
    """Read a ledger file."""
    return {"text": Path(ledger_path).read_text(encoding="utf-8")}


def test_a_path_resolves_inside_a_root_or_is_refused(tmp_path: Path) -> None:
    roots = roots_of([tmp_path / "project"])
    (tmp_path / "project" / "runs").mkdir(parents=True)
    assert inside(str(tmp_path / "project" / "runs"), roots).name == "runs"
    with pytest.raises(OutsideRootError, match="outside"):
        inside(str(tmp_path / "elsewhere.json"), roots)
    # Climbing out with .. resolves first, so it is outside like any other path.
    with pytest.raises(OutsideRootError, match="outside"):
        inside(str(tmp_path / "project" / ".." / "elsewhere.json"), roots)
    for hidden in (".env", ".git/config", "runs/.tra-history"):
        with pytest.raises(OutsideRootError, match="hidden"):
            inside(str(tmp_path / "project" / hidden), roots)


def test_a_confined_tool_checks_positional_and_named_paths(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("{}\n", encoding="utf-8")
    tool = confined(read_ledger, roots_of([tmp_path]))
    assert tool(str(ledger)) == {"text": "{}\n"}
    assert tool(ledger_path=str(ledger)) == {"text": "{}\n"}
    with pytest.raises(OutsideRootError):
        tool(str(tmp_path.parent / "other.jsonl"))


def test_a_model_in_the_tool_loop_cannot_read_the_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GROQ_API_KEY=gsk_not_for_a_vendor\n", encoding="utf-8")
    model = FakeCompleter(
        [
            {
                "action": "call",
                "tool": "read_ledger",
                "arguments": {"ledger_path": str(tmp_path / ".env")},
                "answer": {},
                "thought": "",
            },
            {"action": "answer", "tool": "", "arguments": {}, "answer": {}, "thought": ""},
        ]
    )
    loop = run_tool_loop(
        completer=model,
        system="audit",
        question={},
        tools=confine_all({"read_ledger": read_ledger}, roots_of([tmp_path])),
        answer_schema={"type": "object"},
        max_steps=3,
    )
    assert loop.steps[0].error is not None and "hidden" in loop.steps[0].error
    # Nothing from the file reached the model's next prompt.
    assert "gsk_not_for_a_vendor" not in model.calls[1].user


def test_the_hourly_cap_is_the_desks_not_each_roles(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.jsonl")
    limits = BudgetLimits(max_calls_per_hour=2)
    window = RateWindow()
    roles = [
        MeteredCompleter(
            FakeCompleter(["{}"] * 3),
            role=role,
            provider="fake",
            limits=limits,
            ledger=ledger,
            clock=lambda: 1000.0,
            window=window,
        )
        for role in ("technical_analyst", "macro_analyst", "validator")
    ]
    roles[0].complete(system="", user="", schema={})
    roles[1].complete(system="", user="", schema={})
    with pytest.raises(BudgetSpentError, match="2 calls this hour"):
        roles[2].complete(system="", user="", schema={})


def test_usage_lines_from_many_threads_stay_whole(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.jsonl")

    def write(role: str) -> None:
        for n in range(50):
            ledger.append(
                UsageRecord(
                    at=float(n),
                    role=role,
                    provider="fake",
                    model="m" * 200,
                    input_tokens=n,
                    output_tokens=n,
                    latency_ms=1,
                    cost=Decimal(0),
                    outcome="ok",
                )
            )

    threads = [threading.Thread(target=write, args=(f"role-{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = UsageLedger.read(tmp_path / "usage.jsonl")
    assert len(rows) == 300 and len(ledger.records) == 300


def test_a_vendor_error_never_carries_a_key_into_the_record() -> None:
    env = {"GROQ_API_KEY": "gsk_live_1234567890", "HOME": "/home/desk"}
    said = "AuthenticationError: Incorrect API key provided: gsk_live_1234567890"
    assert "gsk_live_1234567890" not in redact(said, env)
    assert "/home/desk" in redact("cache at /home/desk", env)


def test_provider_errors_are_redacted_from_the_live_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSy-very-secret-value")
    error = ProviderError.from_vendor(RuntimeError("bad key AIzaSy-very-secret-value"))
    assert "very-secret" not in str(error) and "[redacted]" in str(error)


def test_a_venue_message_loses_the_hands_own_key() -> None:
    class Refusing:
        has: dict[str, bool] = {}

        def fetch_position(self, symbol: str) -> object:
            raise RuntimeError("signature mismatch for key abcd1234efgh")

    hand = VenueHand(
        venue="bybit",
        symbol="BTC/USDT:USDT",
        max_notional=Decimal(100),
        keys=("BYBIT_API_KEY", "BYBIT_API_SECRET"),
        client=Refusing(),
        env={"BYBIT_API_KEY": "abcd1234efgh", "BYBIT_API_SECRET": "s" * 16},
    )
    said = hand.ready()
    assert "abcd1234efgh" not in said and "[redacted]" in said


def test_a_malformed_validator_answer_is_a_form_error() -> None:
    answer = {"stance": "pro", "claims": "none", "verdict": "support", "reason": "r"}
    with pytest.raises(FormError, match="claims must be a list"):
        validator_role.parse(json.dumps(answer))


def test_a_short_retry_after_is_waited_for_once_on_the_same_provider() -> None:
    from trading_research_agents.llm.chain import CompleterChain, Link
    from trading_research_agents.llm.completer import Completion, retry_after_of

    said = (
        "RateLimitError: Error code: 429 - Rate limit reached on tokens per minute. "
        "Please try again in 10.192499999s. Need more tokens?"
    )
    assert retry_after_of(RuntimeError(said)) == 10.192499999
    assert retry_after_of(RuntimeError("try again in 592.5ms")) == 0.5925
    assert retry_after_of(RuntimeError("503 UNAVAILABLE")) is None

    class Limited:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *, system: str, user: str, schema: Any) -> Completion:
            self.calls += 1
            if self.calls == 1:
                raise ProviderError.from_vendor(RuntimeError(said))
            return Completion(text="{}", model="primary", input_tokens=1, output_tokens=1)

    class Spare:
        calls = 0

        def complete(self, *, system: str, user: str, schema: Any) -> Completion:
            Spare.calls += 1
            return Completion(text="{}", model="spare", input_tokens=1, output_tokens=1)

    slept: list[float] = []
    primary = Limited()
    chain = CompleterChain(
        role="validator",
        links=[Link("groq:m", lambda: primary), Link("google:m", Spare)],
        max_wait_s=15,
        sleep=slept.append,
    )
    assert chain.complete(system="", user="", schema={}).model == "primary"
    assert primary.calls == 2 and Spare.calls == 0 and slept == [pytest.approx(10.6925)]

    # Asked to wait longer than the declared bound, it moves to the next link at once.
    impatient = CompleterChain(
        role="validator",
        links=[Link("groq:m", Limited), Link("google:m", Spare)],
        max_wait_s=5,
        sleep=slept.append,
    )
    assert impatient.complete(system="", user="", schema={}).model == "spare"


def test_a_spent_quota_rests_the_link_and_answers_name_who_gave_them() -> None:
    from trading_research_agents.llm import chain as chains
    from trading_research_agents.llm.chain import CompleterChain, Link
    from trading_research_agents.llm.completer import Completion

    chains._resting.clear()
    asked: list[str] = []

    class Spent:
        def complete(self, *, system: str, user: str, schema: Any) -> Completion:
            asked.append("google")
            raise ProviderError.from_vendor(
                RuntimeError("429 RESOURCE_EXHAUSTED. You exceeded your current quota")
            )

    class Spare:
        def complete(self, *, system: str, user: str, schema: Any) -> Completion:
            asked.append("groq")
            return Completion(
                text="{}", model="openai/gpt-oss-120b", input_tokens=1, output_tokens=1
            )

    def ask() -> Completion:
        return CompleterChain(
            role="validator",
            links=[
                Link("google:gemini-flash-latest", Spent),
                Link("groq:openai/gpt-oss-120b", Spare),
            ],
            max_wait_s=15,
            sleep=lambda _: None,
        ).complete(system="", user="", schema={})

    first = ask()
    second = ask()
    # The spent link was asked once; the next cycle went straight to the one that answers.
    assert asked == ["google", "groq", "groq"]
    assert first.provider == "groq" and second.model == "openai/gpt-oss-120b"
    chains._resting.clear()
