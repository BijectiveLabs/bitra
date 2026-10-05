"""Appends that survive a crash, whole files that are never seen half written, and their hashes."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CHUNK = 1 << 20


def append_line(path: Path, row: Mapping[str, Any], *, durable: bool = False) -> None:
    """One JSON object as one line at the end of `path`.

    A crash can leave the last line torn. Without the newline guard, the next record would be glued
    onto it and both would be unreadable; with it, only the torn one is lost, and readers skip it.
    `durable` waits for the disk, for records of orders and money.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(row), sort_keys=True, default=str).encode("utf-8") + b"\n"
    with path.open("a+b") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() > 0:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                line = b"\n" + line
            fh.seek(0, os.SEEK_END)
        fh.write(line)
        fh.flush()
        if durable:
            os.fsync(fh.fileno())


def read_lines(path: Path, *, having: bytes = b"") -> list[dict[str, Any]]:
    """Every whole JSON object in a lines file. A torn or foreign line is skipped, never fatal.

    `having` skips, before parsing, a line that does not contain those bytes.
    """
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for raw in path.read_bytes().splitlines():
        if not raw.strip() or having not in raw:
            continue
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def write_json(path: Path, payload: Any, *, durable: bool = False) -> Path:
    """The whole file or none of it: written beside, then renamed over the old one."""
    text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    return _replace(path, text.encode("utf-8"), durable=durable)


def write_gzip_json(path: Path, payload: Any) -> Path:
    # mtime=0 keeps the bytes, and so the hash, a function of the content alone.
    body = gzip.compress(json.dumps(payload, sort_keys=True, default=str).encode("utf-8"), mtime=0)
    return _replace(path, body, durable=False)


def read_gzip_json(path: Path) -> Any:
    return json.loads(gzip.decompress(path.read_bytes()))


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _replace(path: Path, body: bytes, *, durable: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part")
    with temporary.open("wb") as fh:
        fh.write(body)
        fh.flush()
        if durable:
            os.fsync(fh.fileno())
    os.replace(temporary, path)
    return path
