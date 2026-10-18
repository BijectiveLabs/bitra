"""The venues' own public positioning figures, as `fetch` presets for a derivatives series.

Free and without a key, in five-minute steps: who is long against who is short, taker buying
against taker selling, open interest over time. `id` is the venue's endpoint, `params` its query
and `value` the field to read.

    binance-futures  globalLongShortAccountRatio, takerlongshortRatio, openInterestHist
    bybit            account-ratio, open-interest
    okx              contracts/long-short-account-ratio, taker-volume (rows are arrays: the
                     time is field "0", and the value is named by its position)
"""

from __future__ import annotations

from typing import Any

PRESETS: dict[str, dict[str, Any]] = {
    "binance-futures": {
        "url": "https://fapi.binance.com/futures/data/{id}",
        "rows": "",
        "ts": "timestamp",
        "ts_unit": "ms",
    },
    "bybit": {
        "url": "https://api.bybit.com/v5/market/{id}",
        "rows": "result.list",
        "ts": "timestamp",
        "ts_unit": "ms",
    },
    "okx": {
        "url": "https://www.okx.com/api/v5/rubik/stat/{id}",
        "rows": "data",
        "ts": "0",
        "ts_unit": "ms",
    },
}
# Published as it happens, worth an hour at most; positioning moves inside the hour, so a desk on
# fifteen-minute bars reads it every five minutes.
TIMING: dict[str, tuple[str, str]] = {name: ("0s", "1h") for name in PRESETS}
REFRESH: dict[str, str] = {name: "5m" for name in PRESETS}
