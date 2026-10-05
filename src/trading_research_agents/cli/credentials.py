"""Whether every key this desk depends on is there and works, without any of them being shown.

An operator on a server has one question before a run and no way to answer it today: are the
credentials this configuration will actually reach in a position to work? Not the one provider named
at the top of the file - every provider a role may fall through to, and the venue that would take
the order. A key that is present and wrong looks exactly like a key that is right until the moment
it matters, which on a desk is the middle of a cycle.

Three things are reported per credential and a fourth on request:

  present      the variable is set and not empty
  fingerprint  eight hex characters of its SHA-256, so two keys can be told apart and neither read
  installed    the SDK that speaks to it is importable
  verified     one free authenticated call - a model list, a venue preflight - actually answered

The value itself is never printed, never logged and never returned. What a vendor says back is
scrubbed of any secret this process knows before it is shown, because an error message is written by
somebody else and this is the last place to catch it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# Key material this process has seen. Anything printed is checked against it first.
REDACTED = "<redacted>"
MIN_SECRET = 8


def fingerprint(value: str) -> str:
    """Eight hex characters of the value's SHA-256. One way, and enough to tell two keys apart."""
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def scrub(text: str, secrets: Sequence[str]) -> str:
    """Remove anything this process knows to be secret from a message somebody else wrote."""
    out = text
    for secret in secrets:
        if secret and len(secret) >= MIN_SECRET:
            out = out.replace(secret, REDACTED)
    return out


@dataclass(frozen=True)
class Credential:
    """One thing a run needs a key for, and how ready it is."""

    what: str
    env: tuple[str, ...]
    present: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    fingerprints: tuple[str, ...] = ()
    installed: bool = True
    needs: tuple[str, str] = ("", "")
    note: str = ""
    verified: str | None = None

    @property
    def ready(self) -> bool:
        return not self.missing and self.installed and (self.verified in (None, ""))

    @property
    def verdict(self) -> str:
        if self.missing:
            return f"{', '.join(self.missing)} is not set"
        if not self.installed:
            module, extra = self.needs
            return (
                f"the {module} SDK is not installed; pip install 'trading-research-agents[{extra}]'"
            )
        if self.verified is None:
            return "set, not verified"
        if self.verified == "":
            return "verified"
        return self.verified

    def record(self) -> dict[str, Any]:
        return {
            "what": self.what,
            "env": list(self.env),
            "present": list(self.present),
            "missing": list(self.missing),
            "fingerprints": list(self.fingerprints),
            "installed": self.installed,
            "verified": self.verified,
            "ready": self.ready,
            "verdict": self.verdict,
        }


@dataclass
class Readiness:
    """Every credential this configuration would reach, in the order it would reach them."""

    rows: list[Credential] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return all(row.ready for row in self.rows)

    def record(self) -> dict[str, Any]:
        return {"ready": self.ready, "credentials": [row.record() for row in self.rows]}


def _held(env: Mapping[str, str], names: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    present = tuple(name for name in names if env.get(name))
    return present, tuple(name for name in names if not env.get(name))


def provider_row(
    provider: Any, env: Mapping[str, str], *, label: str, verify: bool = False
) -> Credential:
    """One model provider: its key, its SDK, and on request one free authenticated call."""
    names = (provider.key_env,) if provider.key_env else ()
    present, missing = _held(env, names)
    row = Credential(
        what=label,
        env=names,
        present=present,
        missing=missing,
        fingerprints=tuple(fingerprint(env[name]) for name in present),
        installed=provider.installed,
        needs=provider.needs,
        note=provider.note,
    )
    if not verify or not row.installed or row.missing:
        return row
    if provider.verify is None:
        return Credential(**{**vars(row), "verified": None})
    said = provider.verify(None, None)
    return Credential(**{**vars(row), "verified": scrub(said, [env[name] for name in present])})


def venue_row(
    venue: Any, env: Mapping[str, str], *, label: str, preflight: Any | None = None
) -> Credential:
    """The venue that would take the order: its keys, and on request its own preflight."""
    names = tuple(getattr(venue, "env_keys", ()))
    present, missing = _held(env, names)
    row = Credential(
        what=label,
        env=names,
        present=present,
        missing=missing,
        fingerprints=tuple(fingerprint(env[name]) for name in present),
    )
    if preflight is None or row.missing:
        return row
    secrets = [env[name] for name in present]
    try:
        preflight()
    except Exception as exc:
        return Credential(
            **{**vars(row), "verified": scrub(f"{exc.__class__.__name__}: {exc}", secrets)[:240]}
        )
    return Credential(**{**vars(row), "verified": ""})


def in_play(agents: Any) -> list[tuple[str, str]]:
    """Every provider this configuration can reach, as (provider, why it is reachable).

    The declared one, whatever a role was given instead, and every link of every fallback chain: a
    spare nobody checked is a spare that fails on the day it is needed.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(name: str, why: str) -> None:
        if name and name not in seen:
            seen.add(name)
            out.append((name, why))

    add(agents.provider, "[agents].provider")
    for role, table in sorted(agents.roles.items()):
        if table.get("provider"):
            add(str(table["provider"]), f"[agents.roles.{role}].provider")
    for name in agents.fallback:
        add(name.partition(":")[0], "[agents].fallback")
    for role, table in sorted(agents.roles.items()):
        for name in table.get("fallback") or ():
            add(str(name).partition(":")[0], f"[agents.roles.{role}].fallback")
    return out
