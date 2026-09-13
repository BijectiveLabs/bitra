"""Download a public archive, hash it as it arrives, and refuse to keep it if the hash is wrong.

The file is written to a `.part` path and renamed only after the size and the checksum have
been checked, so a half-downloaded archive is never mistaken for a complete one. Some venues
publish a checksum sidecar per file; when the venue plugin names one it is verified, which is
the cross-source check the data integrity protocol asks for before a file is trusted.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from trading_research_agent import __version__
from trading_research_agent.data.manifest import Source

USER_AGENT = f"trading-research-agent/{__version__}"
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class FetchError(RuntimeError):
    """The archive could not be fetched, or what arrived is not what was published."""


def _checked_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme == "https":
        return url
    if parts.scheme == "http" and parts.hostname in LOOPBACK:
        # Plain HTTP is only ever a local test server, never a venue.
        return url
    raise FetchError(f"refusing to fetch {url!r}: only https, or http on loopback, is allowed")


def fetch(
    url: str,
    dest: Path,
    *,
    expected_sha256: str | None = None,
    timeout: float = 60.0,
    chunk_size: int = 1 << 20,
) -> Source:
    url = _checked_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            if response.status != 200:
                raise FetchError(f"{url}: HTTP {response.status}")
            declared = response.headers.get("Content-Length")
            with part.open("wb") as fh:
                for block in iter(lambda: response.read(chunk_size), b""):
                    fh.write(block)
                    digest.update(block)
                    size += len(block)
                fh.flush()
                os.fsync(fh.fileno())
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise FetchError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise FetchError(f"{url}: {exc}") from exc

    if declared is not None and declared.isdigit() and int(declared) != size:
        part.unlink(missing_ok=True)
        raise FetchError(f"{url}: received {size} bytes, server declared {declared}")
    actual = digest.hexdigest()
    if expected_sha256 is not None and actual != expected_sha256.lower():
        part.unlink(missing_ok=True)
        raise FetchError(f"{dest.name}: sha256 {actual} does not match published {expected_sha256}")
    os.replace(part, dest)
    return Source(dest.name, actual, size)


def fetch_checksum(url: str, filename: str, *, timeout: float = 30.0) -> str:
    """Read a `<sha256>  <filename>` sidecar and return the hash it publishes for `filename`."""
    url = _checked_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            text: str = response.read(4096).decode("ascii", "replace")
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"{url}: {exc}") from exc
    fields = text.split()
    if len(fields) < 2 or len(fields[0]) != 64 or fields[1] != filename:
        raise FetchError(
            f"{url}: checksum sidecar does not describe {filename!r}: {text.strip()!r}"
        )
    return fields[0].lower()


def fetch_json(url: str, *, timeout: float = 30.0, max_bytes: int = 1 << 22) -> Any:
    """One GET, parsed as JSON. Bounded in size; never a venue over plain http."""
    url = _checked_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw: bytes = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"{url}: {exc}") from exc
    if len(raw) > max_bytes:
        raise FetchError(f"{url}: response larger than {max_bytes} bytes")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FetchError(f"{url}: not JSON: {exc}") from exc
