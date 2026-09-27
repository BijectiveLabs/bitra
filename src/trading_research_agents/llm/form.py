"""What every role response has to be before anyone reads it: valid JSON, the schema's shape."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_research_agent.agent.roles import RoleError


def parse_object(text: str, schema: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RoleError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RoleError("response must be a JSON object")
    missing = [k for k in schema.get("required", ()) if k not in data]
    if missing:
        raise RoleError(f"response lacks {', '.join(missing)}")
    extra = [k for k in data if k not in schema.get("properties", {})]
    if extra:
        raise RoleError(f"response carries fields outside the schema: {', '.join(extra)}")
    return data


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoleError(f"{label} must be a non-empty string")
    return value.strip()


def _enum(value: Any, label: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise RoleError(f"{label} must be one of {', '.join(allowed)}; got {value!r}")
    return str(value)


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise RoleError(f"{label} must be a list")
    return value


def _dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RoleError(f"{label} must be an object")
    return value


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise RoleError(f"{label} must be a decimal string")
    try:
        out = Decimal(str(value))
    except InvalidOperation as exc:
        raise RoleError(f"{label} is not a number: {value!r}") from exc
    if not out.is_finite():
        raise RoleError(f"{label} must be finite")
    return out


def _unit(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise RoleError(f"{label} must be a number in [0, 1]")
    return float(value)


def _posint(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RoleError(f"{label} must be a positive integer")
    return value
