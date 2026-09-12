from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from trading_research_agent.data import FetchError, fetch, fetch_checksum


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    root = tmp_path / "www"
    root.mkdir()
    body = b"timestamp,symbol\n1,BTCUSDT\n" * 100
    (root / "BTCUSDT2025-01-01.csv.gz").write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    (root / "BTCUSDT2025-01-01.csv.gz.CHECKSUM").write_text(
        f"{digest}  BTCUSDT2025-01-01.csv.gz\n", encoding="ascii"
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", root
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_writes_the_file_and_reports_its_hash(
    served: tuple[str, Path], tmp_path: Path
) -> None:
    base, root = served
    dest = tmp_path / "out" / "BTCUSDT2025-01-01.csv.gz"
    source = fetch(f"{base}/BTCUSDT2025-01-01.csv.gz", dest)
    assert dest.read_bytes() == (root / "BTCUSDT2025-01-01.csv.gz").read_bytes()
    assert source.size == dest.stat().st_size
    assert source.sha256 == hashlib.sha256(dest.read_bytes()).hexdigest()
    assert not dest.with_name(dest.name + ".part").exists()


def test_fetch_verifies_against_a_published_checksum(
    served: tuple[str, Path], tmp_path: Path
) -> None:
    base, _ = served
    published = fetch_checksum(
        f"{base}/BTCUSDT2025-01-01.csv.gz.CHECKSUM", "BTCUSDT2025-01-01.csv.gz"
    )
    dest = tmp_path / "BTCUSDT2025-01-01.csv.gz"
    source = fetch(f"{base}/BTCUSDT2025-01-01.csv.gz", dest, expected_sha256=published)
    assert source.sha256 == published

    with pytest.raises(FetchError, match="does not match published"):
        fetch(f"{base}/BTCUSDT2025-01-01.csv.gz", tmp_path / "bad.gz", expected_sha256="0" * 64)
    assert not (tmp_path / "bad.gz").exists()
    assert not (tmp_path / "bad.gz.part").exists()


def test_fetch_fails_closed_on_missing_files_and_bad_sidecars(
    served: tuple[str, Path], tmp_path: Path
) -> None:
    base, _ = served
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch(f"{base}/nope.gz", tmp_path / "nope.gz")
    assert not (tmp_path / "nope.gz").exists()
    with pytest.raises(FetchError, match="does not describe"):
        fetch_checksum(f"{base}/BTCUSDT2025-01-01.csv.gz.CHECKSUM", "other.gz")


def test_only_https_or_loopback_http_is_fetched(tmp_path: Path) -> None:
    with pytest.raises(FetchError, match="only https"):
        fetch("http://public.bybit.com/trading/x.gz", tmp_path / "x.gz")
    with pytest.raises(FetchError, match="only https"):
        fetch("ftp://127.0.0.1/x.gz", tmp_path / "x.gz")
