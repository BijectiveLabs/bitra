from __future__ import annotations

import pytest

from trading_research_agents.features.catalog import DEFAULT_FEATURES
from trading_research_agents.features.core import Absent, Inputs, LookAheadError
from trading_research_agents.features.text import KINDS, TextEvent, text_count_24, text_kind_p_24

M = 60_000_000_000
HOUR = 60 * M
AT = 1_751_328_000_000_000_000 + 30 * HOUR


def event(seen_ns: int, kind: str, p: float, **concerns: float) -> TextEvent:
    probabilities = dict.fromkeys(KINDS, 0.0)
    probabilities[kind] = p
    return TextEvent(
        feed="coindesk",
        first_seen_ns=seen_ns,
        kinds=probabilities,
        concerns=concerns,
        model="jev-1.14.0",
    )


def inputs(text: list[TextEvent] | None, asset: str = "BTC") -> Inputs:
    return Inputs(at_ns=AT, interval_ns=HOUR, bars=(), text=text, asset=asset)


def test_no_judged_text_is_absent_not_a_quiet_day() -> None:
    assert isinstance(text_count_24(inputs(None)), Absent)
    assert isinstance(text_kind_p_24("hack")(inputs(None)), Absent)
    assert text_count_24(inputs([])) == 0
    assert text_kind_p_24("hack")(inputs([])) == 0.0


def test_the_strongest_relevant_report_in_the_window_is_what_counts() -> None:
    text = [
        event(AT - 2 * HOUR, "hack", 0.9, BTC=0.95, market=0.1),
        event(AT - 3 * HOUR, "hack", 0.99, ETH=0.9, market=0.2),
        event(AT - 30 * HOUR, "hack", 0.99, BTC=1.0),
    ]
    # BTC's own report: 0.9 x 0.95. Another asset's counts only as far as it concerns the market,
    # and a report older than 24 bars is outside the window.
    assert text_kind_p_24("hack")(inputs(text)) == pytest.approx(0.855)
    assert text_kind_p_24("listing")(inputs(text)) == 0.0
    assert text_count_24(inputs(text)) == 1
    assert text_kind_p_24("hack")(inputs(text, asset="ETH")) == pytest.approx(0.891)


def test_a_report_without_a_confident_kind_counts_but_carries_no_kind() -> None:
    unsure = TextEvent("coindesk", AT - HOUR, kinds={}, concerns={"BTC": 0.9}, model="m")
    assert text_count_24(inputs([unsure])) == 1
    assert text_kind_p_24("hack")(inputs([unsure])) == 0.0


def test_a_report_seen_after_the_decision_is_a_defect() -> None:
    with pytest.raises(LookAheadError, match="text"):
        inputs([event(AT + 1, "hack", 0.9, BTC=1.0)])


def test_the_catalog_carries_the_text_features() -> None:
    names = {f.spec.name for f in DEFAULT_FEATURES}
    assert {"text_count_24", *(f"text_{k}_p_24" for k in KINDS if k != "none")} <= names
