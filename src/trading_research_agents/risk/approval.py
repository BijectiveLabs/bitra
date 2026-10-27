"""What the gate signs, and how the order hand tells a signed intent from any other.

The signature is an HMAC over the fields that make the order: instrument, side, size, prices,
channel, mode and the cycle it came from. Its key is drawn when the process starts and never
leaves it, so an approval cannot be written by hand, carried over from another run, or replayed
from an intent on disk. It guards against wiring faults, not against code that runs in this
process with intent to forge.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from typing import Any

KEY = "risk_gate"
BOUND = (
    "instrument",
    "target",
    "size",
    "mid",
    "stop_price",
    "take_price",
    "channel",
    "mode",
    "at",
)
_SECRET = secrets.token_bytes(32)


def _signature(intent: Mapping[str, Any], approved_at: float, expires_at: float) -> str:
    bound = {name: None if intent.get(name) is None else str(intent[name]) for name in BOUND}
    message = json.dumps(
        {"intent": bound, "approved_at": approved_at, "expires_at": expires_at}, sort_keys=True
    ).encode("utf-8")
    return hmac.new(_SECRET, message, hashlib.sha256).hexdigest()


def sign(
    intent: Mapping[str, Any], *, checks: tuple[str, ...], now: float, valid_for_s: float
) -> dict[str, Any]:
    expires_at = now + valid_for_s
    return {
        "approved": True,
        "checks": list(checks),
        "approved_at": now,
        "expires_at": expires_at,
        "signature": _signature(intent, now, expires_at),
    }


def approval_problem(intent: Mapping[str, Any], *, now: float) -> str:
    """Why this intent may not be sent as a new order, or empty when the gate approved it."""
    approval = intent.get(KEY)
    if not isinstance(approval, Mapping) or approval.get("approved") is not True:
        return "no approval from the risk gate; a new order goes out only after it"
    try:
        approved_at = float(approval["approved_at"])
        expires_at = float(approval["expires_at"])
        signed = str(approval["signature"])
    except (KeyError, TypeError, ValueError):
        return "the risk gate's approval is incomplete"
    if not hmac.compare_digest(signed, _signature(intent, approved_at, expires_at)):
        return "the risk gate's approval does not match this intent, or was not made in this run"
    if now > expires_at:
        return f"the risk gate's approval expired {now - expires_at:.0f}s ago"
    return ""
