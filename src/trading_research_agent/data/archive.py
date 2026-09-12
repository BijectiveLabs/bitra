"""Where the public archives live, and how to read one without unpacking it to disk."""

from __future__ import annotations

import gzip
import hashlib
import io
import zipfile
from collections.abc import Iterator
from datetime import date
from pathlib import Path

BYBIT_BASE = "https://public.bybit.com/trading"
BINANCE_BASE = "https://data.binance.vision/data/spot/monthly/trades"


class ArchiveError(ValueError):
    """The archive on disk is not one file of one CSV."""


def bybit_trades_url(instrument: str, day: date) -> str:
    return f"{BYBIT_BASE}/{instrument}/{instrument}{day.isoformat()}.csv.gz"


def binance_spot_trades_url(instrument: str, year: int, month: int) -> str:
    return f"{BINANCE_BASE}/{instrument}/{instrument}-trades-{year:04d}-{month:02d}.zip"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_archive_lines(path: Path) -> Iterator[str]:
    """Yield text lines from a .csv, a .csv.gz, or a .zip holding exactly one CSV."""
    suffix = path.suffix.lower()
    if suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            yield from fh
        return
    if suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = [m for m in archive.namelist() if not m.endswith("/")]
            if len(members) != 1:
                raise ArchiveError(f"{path.name} holds {len(members)} files; expected one CSV")
            with archive.open(members[0]) as raw:
                yield from io.TextIOWrapper(raw, encoding="utf-8", newline="")
        return
    if suffix == ".csv":
        with path.open("r", encoding="utf-8", newline="") as fh:
            yield from fh
        return
    raise ArchiveError(f"{path.name}: unsupported archive type {suffix!r}")
