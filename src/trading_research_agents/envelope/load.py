"""Read an envelope from TOML.

Numbers are parsed straight into Decimal. Going through a float first would turn a quoted
5.5 bps into something that is not 5.5, and the whole point of the envelope is that a reader
can substitute their own figure and get the same arithmetic we got.
"""

from __future__ import annotations

import tomllib
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trading_research_agents.envelope.model import Channel, Envelope, EnvelopeError


def load_envelope(path: Path | str) -> Envelope:
    text = Path(path).read_text(encoding="utf-8")
    return parse_envelope(text)


def parse_envelope(text: str) -> Envelope:
    try:
        raw = tomllib.loads(text, parse_float=Decimal)
    except tomllib.TOMLDecodeError as exc:
        raise EnvelopeError(f"envelope is not valid TOML: {exc}") from exc

    channels_raw = raw.get("channel")
    if not isinstance(channels_raw, list) or not channels_raw:
        raise EnvelopeError("envelope declares no [[channel]] tables")

    channels = tuple(_channel(i, c) for i, c in enumerate(channels_raw))

    latency = None
    header = raw.get("envelope", {})
    if not isinstance(header, dict):
        raise EnvelopeError("[envelope] must be a table")
    if "order_latency_ms" in header:
        latency = _decimal("envelope.order_latency_ms", header["order_latency_ms"])

    return Envelope(channels=channels, order_latency_ms=latency)


def _channel(index: int, raw: Any) -> Channel:
    where = f"channel[{index}]"
    if not isinstance(raw, dict):
        raise EnvelopeError(f"{where} must be a table")
    for required in ("venue", "name", "entry_fee_bps", "exit_fee_bps"):
        if required not in raw:
            raise EnvelopeError(f"{where} is missing {required!r}")
    measured = raw.get("measured_round_trip_bps")
    return Channel(
        venue=str(raw["venue"]),
        name=str(raw["name"]),
        entry_fee_bps=_decimal(f"{where}.entry_fee_bps", raw["entry_fee_bps"]),
        exit_fee_bps=_decimal(f"{where}.exit_fee_bps", raw["exit_fee_bps"]),
        measured_round_trip_bps=(
            None if measured is None else _decimal(f"{where}.measured_round_trip_bps", measured)
        ),
    )


def _decimal(where: str, value: Any) -> Decimal:
    if isinstance(value, bool):
        raise EnvelopeError(f"{where} must be a number")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise EnvelopeError(f"{where} is not a number: {value!r}") from exc
    raise EnvelopeError(f"{where} must be a number, got {type(value).__name__}")
