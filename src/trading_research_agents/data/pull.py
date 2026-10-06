"""Refresh a declared series file from an HTTP JSON endpoint, so the analysts read today's figure.

The analysts still read only the local file; this writes it. A vendor is a `fetch` table in the
series declaration, so a free endpoint and a paid one are declared the same way.
"""

from __future__ import annotations

import csv
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trading_research_agents.data.series import (
    NS_PER_S,
    TIME_UNITS,
    SeriesError,
    SeriesSpec,
    moment,
    read_series,
)

# Endpoints checked by hand against their public documentation. Anything else is `from = "http"`.
PRESETS: dict[str, dict[str, Any]] = {
    # Crypto Fear & Greed Index, daily, free, no key.
    "alternative.me": {
        "url": "https://api.alternative.me/fng/?limit=90&format=json",
        "rows": "data",
        "ts": "timestamp",
        "ts_unit": "s",
        "value": "value",
    },
    # Federal Reserve Economic Data, free with a key from fred.stlouisfed.org.
    "fred": {
        "url": (
            "https://api.stlouisfed.org/fred/series/observations?file_type=json&series_id={id}"
            "&sort_order=desc&limit=500"
        ),
        "rows": "observations",
        "ts": "date",
        "ts_unit": "iso8601",
        "value": "value",
        "key_env": "FRED_API_KEY",
        "key_param": "api_key",
    },
    # Blockchain.com charts, Bitcoin only, daily, free, no key. `id` is the chart name, such as
    # n-unique-addresses, n-transactions or hash-rate.
    "blockchain.info": {
        "url": "https://api.blockchain.info/charts/{id}?timespan=180days&format=json",
        "rows": "values",
        "ts": "x",
        "ts_unit": "s",
        "value": "y",
    },
    # Coin Metrics community API, daily, free, no key, many assets. `id` is the metric, such as
    # AdrActCnt or FlowInExNtv; `asset` defaults to btc; `minus` subtracts a second metric, which
    # is how exchange inflow less outflow becomes one net-flow series.
    "coinmetrics": {
        "url": (
            "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
            "?assets={asset}&metrics={metrics}&frequency=1d&paging_from=end&page_size=180"
        ),
        "rows": "data",
        "ts": "time",
        "ts_unit": "iso8601",
        "value": "{id}",
        "asset": "btc",
    },
}
FETCH_KEYS = (
    "from",
    "id",
    "asset",
    "minus",
    "url",
    "rows",
    "ts",
    "ts_unit",
    "value",
    "key_env",
    "key_param",
    "key_header",
    "key_prefix",
)
TIMEOUT_S = 20
MAX_BYTES = 20_000_000

Getter = Callable[[str, Mapping[str, str]], bytes]


class PullError(ValueError):
    """The endpoint could not be read, or answered with something that is not the series."""


@dataclass(frozen=True)
class Pulled:
    series: str
    added: int
    rows: int
    last: str | None


def plan(fetch: Mapping[str, Any], name: str) -> dict[str, Any]:
    """The preset, with the operator's own keys over it, checked before anything is requested."""
    unknown = sorted(set(fetch) - set(FETCH_KEYS))
    if unknown:
        raise PullError(f"{name}: fetch has unknown {unknown}; have {list(FETCH_KEYS)}")
    origin = str(fetch["from"])
    if origin != "http" and origin not in PRESETS:
        raise PullError(f"{name}: fetch.from is 'http' or one of {sorted(PRESETS)}, not {origin!r}")
    out = {**PRESETS.get(origin, {}), **{k: v for k, v in fetch.items() if k != "from"}}
    wanted = [str(out["id"])] if out.get("id") else []
    if out.get("minus"):
        wanted.append(str(out["minus"]))
    fills = {
        "{id}": str(out.get("id", "")),
        "{asset}": str(out.get("asset", "")),
        "{metrics}": ",".join(wanted),
    }
    for hole, value in fills.items():
        if hole in str(out.get("url", "")) or hole == out.get("value"):
            if not value:
                raise PullError(f"{name}: fetch from {origin} needs {hole.strip('{}')}")
            out["url"] = str(out["url"]).replace(hole, urllib.parse.quote(value, safe=","))
            if out.get("value") == hole:
                out["value"] = value
    url = str(out.get("url", ""))
    if not url.startswith("https://"):
        # A key in the clear, or a figure anyone on the path could change, is not worth reading.
        raise PullError(f"{name}: fetch.url is an https address")
    for key in ("rows", "ts", "value"):
        out.setdefault(key, {"rows": "", "ts": "ts", "value": "value"}[key])
    out.setdefault("ts_unit", "iso8601")
    if out["ts_unit"] not in (*TIME_UNITS, "iso8601"):
        raise PullError(f"{name}: fetch.ts_unit is s, ms, us, ns or iso8601")
    return out


def pull(
    spec: SeriesSpec,
    base: Path,
    *,
    env: Mapping[str, str] | None = None,
    get: Getter | None = None,
) -> Pulled:
    """Fetch, merge with what the file holds, and write it back. A failure leaves the file alone."""
    if spec.fetch is None:
        raise PullError(f"{spec.name}: no fetch declared; the file is kept by hand")
    if not spec.has_header or spec.file.lower().endswith(".json"):
        raise PullError(f"{spec.name}: a fetched series is written as a CSV with a header row")
    how = plan(spec.fetch, spec.name)
    held = os.environ if env is None else env
    url, headers = str(how["url"]), {"User-Agent": "trading-research-agents", "Accept": "*/*"}
    secret = ""
    if how.get("key_env"):
        secret = held.get(str(how["key_env"]), "")
        if not secret:
            raise PullError(
                f"{spec.name}: {how['key_env']} is not in the environment; set it in the shell "
                "or in the .env beside this desk's strategy.toml (a .env anywhere else is not read)"
            )
        if how.get("key_header"):
            headers[str(how["key_header"])] = f"{how.get('key_prefix', '')}{secret}"
        else:
            joiner = "&" if "?" in url else "?"
            param = urllib.parse.quote(str(how.get("key_param") or "api_key"))
            url = f"{url}{joiner}{param}={urllib.parse.quote(secret, safe='')}"
    try:
        body = (get or _get)(url, headers)
    except (OSError, ValueError) as exc:
        raise PullError(_hide(f"{spec.name}: {exc.__class__.__name__}: {exc}", secret)) from None
    fresh = _points(body, how, spec.name)
    path = (base / spec.file) if not Path(spec.file).is_absolute() else Path(spec.file)
    known: dict[int, Decimal] = {}
    if path.is_file():
        try:
            known = {p.observed_ns: p.value for p in read_series(spec, base).points}
        except SeriesError as exc:
            raise PullError(
                f"{spec.name}: the file on disk cannot be read, so it is not merged: {exc}"
            ) from exc
    before = len(known)
    known.update(fresh)
    _write(path, spec, known)
    last = max(known) if known else None
    return Pulled(
        series=spec.name,
        added=len(known) - before,
        rows=len(known),
        last=None if last is None else moment(last).isoformat(),
    )


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        # A redirect off https would carry the key, or the figure, in the clear.
        if not newurl.startswith("https://"):
            raise ValueError(
                f"refused a redirect off https to {urllib.parse.urlsplit(newurl).hostname}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _get(url: str, headers: Mapping[str, str]) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("only https is read")
    request = urllib.request.Request(url, headers=dict(headers))  # noqa: S310 - checked above
    with urllib.request.build_opener(_HttpsOnly()).open(request, timeout=TIMEOUT_S) as response:
        body: bytes = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError(f"the answer is larger than {MAX_BYTES} bytes")
    return body


def _points(body: bytes, how: Mapping[str, Any], name: str) -> dict[int, Decimal]:
    try:
        loaded: Any = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PullError(f"{name}: the answer is not JSON: {exc}") from None
    for step in [s for s in str(how["rows"]).split(".") if s]:
        if not isinstance(loaded, Mapping) or step not in loaded:
            raise PullError(f"{name}: the answer has no {how['rows']!r}")
        loaded = loaded[step]
    if not isinstance(loaded, list):
        raise PullError(f"{name}: {how['rows'] or 'the answer'} is not a list of rows")
    out: dict[int, Decimal] = {}
    for row in loaded:
        if not isinstance(row, Mapping) or how["ts"] not in row or how["value"] not in row:
            continue
        try:
            value = Decimal(str(row[how["value"]]).strip())
            if how.get("minus"):
                value -= Decimal(str(row[how["minus"]]).strip())
            when = _when(str(row[how["ts"]]).strip(), str(how["ts_unit"]))
        except (InvalidOperation, ValueError, OverflowError, KeyError):
            # Vendors mark a missing day in their own way (FRED writes "."); a row that is not a
            # number is not a figure, and it is skipped rather than guessed.
            continue
        if value.is_finite():
            out[when] = value
    if not out:
        raise PullError(f"{name}: no row carried both {how['ts']!r} and a numeric {how['value']!r}")
    return out


def _when(text: str, unit: str) -> int:
    if unit == "iso8601":
        # Some vendors stamp nanoseconds; Python 3.11 reads at most microseconds.
        head, dot, rest = text.replace("Z", "+00:00").partition(".")
        if dot:
            digits = len(rest) - len(rest.lstrip("0123456789"))
            rest = rest[:6] + rest[digits:] if digits > 6 else rest
        parsed = datetime.fromisoformat(head + dot + rest)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * NS_PER_S)
    return int(Decimal(text) * TIME_UNITS[unit])


def _write(path: Path, spec: SeriesSpec, points: Mapping[int, Decimal]) -> None:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=spec.delimiter, lineterminator="\n")
    writer.writerow([spec.ts, spec.value])
    for when in sorted(points):
        if spec.ts_unit == "iso8601":
            stamp = moment(when).isoformat()
        else:
            stamp = str(when // TIME_UNITS[spec.ts_unit])
        writer.writerow([stamp, str(points[when])])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(out.getvalue(), encoding="utf-8")
    temporary.replace(path)


def _hide(text: str, secret: str) -> str:
    if secret:
        text = text.replace(secret, "***").replace(urllib.parse.quote(secret, safe=""), "***")
    return text


def where_from(spec: SeriesSpec) -> str:
    """Where this file's figures come from, in the words a person checks against a vendor."""
    if spec.fetch is None:
        return f"kept by hand in {spec.file}"
    origin = str(spec.fetch.get("from"))
    named = f" {spec.fetch['id']}" if spec.fetch.get("id") else ""
    if spec.fetch.get("minus"):
        named += f" less {spec.fetch['minus']}"
    if origin == "http":
        host = urllib.parse.urlsplit(str(spec.fetch.get("url", ""))).hostname or "?"
        return f"fetched from {host} every {_every(spec)}"
    return f"fetched from {origin}{named} every {_every(spec)}"


def describe(spec: SeriesSpec, base: Path, now: datetime) -> dict[str, Any]:
    """The last value on disk, how old it is, and whether a cycle would count it."""
    out: dict[str, Any] = {
        "series": spec.name,
        "from": where_from(spec),
        "source": spec.source,
        "max_age": _span(spec.max_age.total_seconds()),
    }
    try:
        latest = read_series(spec, base).latest()
    except SeriesError as exc:
        return {**out, "error": str(exc), "counted": False}
    if latest is None:
        return {**out, "error": "the file has no rows", "counted": False}
    age = now.timestamp() - latest.observed_ns / NS_PER_S
    return {
        **out,
        "last": moment(latest.observed_ns).isoformat(),
        "value": str(latest.value),
        "age_h": round(age / 3600, 1),
        "counted": age <= spec.max_age.total_seconds(),
    }


def due(spec: SeriesSpec, base: Path, now: datetime) -> bool:
    """A fetched series whose file is missing or older than its refresh interval."""
    if spec.fetch is None:
        return False
    path = (base / spec.file) if not Path(spec.file).is_absolute() else Path(spec.file)
    if not path.is_file():
        return True
    return now.timestamp() - path.stat().st_mtime >= spec.refresh.total_seconds()


def _every(spec: SeriesSpec) -> str:
    return _span(spec.refresh.total_seconds())


def _span(seconds: float) -> str:
    whole = int(seconds)
    for size, unit in ((86_400, "d"), (3_600, "h"), (60, "m")):
        if whole and whole % size == 0:
            return f"{whole // size}{unit}"
    return f"{whole}s"
