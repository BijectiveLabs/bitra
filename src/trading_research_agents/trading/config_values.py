"""Reading one value from a configuration table: numbers, counts, names, flags, refused secrets."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

# Keys never live in a config file. A setting whose name reads like a credential is refused by
# name - but only after the table's own settings are accounted for, because `key_ip_unbound_ok` is
# a flag about a key rather than a key.
SECRET_LIKE = ("api_key", "api_secret", "secret", "key", "token", "password", "passphrase")


def _no_secrets(unknown: Sequence[str], where: str) -> None:
    """Refuse a setting nobody declared that reads like a credential, before anything else."""
    leaked = [k for k in unknown if any(word in k.lower() for word in SECRET_LIKE)]
    if leaked:
        raise ConfigError(f"{where} carries {leaked}; keys come from the environment, never a file")


class ConfigError(ValueError):
    """The run configuration is incomplete or contradicts itself."""


def _choice(
    table: dict[str, Any], key: str, allowed: tuple[str, ...], default: str, where: str
) -> str:
    value = str(table.get(key, default))
    if value not in allowed:
        raise ConfigError(f"{where}.{key} is one of {list(allowed)}")
    return value


def _names(raw: Any, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ConfigError(f"[confluence].{key} is a list of names")
    return tuple(str(name) for name in raw)


def _count(table: dict[str, Any], key: str) -> int:
    value = table.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{key} is a non-negative integer")
    return value


def _flag(table: dict[str, Any], key: str) -> bool:
    value = table.get(key, False)
    if not isinstance(value, bool):
        raise ConfigError(f"[live].{key} is true or false")
    return value


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] table is required")
    return value


def _or_dec(table: dict[str, Any], key: str, default: str | None) -> Decimal:
    """The declared figure, or the default; no default means the figure is required."""
    if key in table or default is None:
        return _dec(table, key)
    return Decimal(default)


def _four_hours(run_raw: dict[str, Any]) -> int:
    # The default hold is a span of time, so it is the same four hours on any decision timeframe.
    interval = _or_int(run_raw, "bar_interval_s", 60)
    return max(1, 14_400 // max(interval, 1))


def _or_int(table: dict[str, Any], key: str, default: int | None) -> int:
    if key in table or default is None:
        return _int(table, key)
    return default


def _dec(table: dict[str, Any], key: str) -> Decimal:
    if key not in table:
        raise ConfigError(f"missing {key}")
    value = table[key]
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a number")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        try:
            return Decimal(str(value))
        except ArithmeticError as exc:
            raise ConfigError(f"{key} is not a number: {value!r}") from exc
    raise ConfigError(f"{key} must be a number")


def _int(table: dict[str, Any], key: str) -> int:
    if key not in table:
        raise ConfigError(f"missing {key}")
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    return value
