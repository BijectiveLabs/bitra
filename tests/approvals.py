"""Intents approved by the real risk gate, for tests of what the order hand does after it.

The approval does not expire within a test, so a test about the venue side of a send states its
intent and nothing else. Tests of the gate itself are in test_risk_gate.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trading_research_agents.risk.approval import KEY, sign


def approved(intent: Mapping[str, Any]) -> dict[str, Any]:
    """The intent with the gate's real signature, valid from the epoch onward, and no checks run:
    a test of the hand states a malformed intent on purpose, to see the hand refuse it."""
    return {**intent, KEY: sign(intent, checks=(), now=0.0, valid_for_s=1e12)}
