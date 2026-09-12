from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_research_agent.envelope import Envelope, load_envelope

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
def envelope() -> Envelope:
    return load_envelope(EXAMPLES / "envelope.toml")
