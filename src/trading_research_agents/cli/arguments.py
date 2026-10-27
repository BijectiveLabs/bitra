"""Argument types the command line reads: decimals, durations, the Kelly fraction."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation


def parse_decimal(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None


def parse_duration_s(text: str) -> float:
    """`30s`, `5m`, `2h` to seconds, for a flag. The same spellings data.toml already takes."""
    from trading_research_agents.data.sources import parse_duration

    seconds = parse_duration(text)
    if seconds < 5:
        raise argparse.ArgumentTypeError("an interval below five seconds is not a trading cadence")
    return seconds


def _kelly_fraction(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except ArithmeticError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not value.is_finite() or not Decimal(0) < value <= 1:
        raise argparse.ArgumentTypeError("the Kelly fraction is above 0 and at most 1")
    return value
