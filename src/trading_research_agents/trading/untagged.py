"""Keeps ccxt's own broker and channel tags off the requests this package sends with a user's keys.

ccxt identifies itself to several venues so that they can pay its maintainers a share of the fees
on the orders it sends. The user did not choose that and it is not ours to hand over silently. An
option cannot switch it off: ccxt reads an empty value as absent and falls back to its own. So the
request is checked where it leaves, in `fetch`, after ccxt has built and signed it.

- A header that carries ccxt's identity is removed. Headers are not part of what a signature
  covers on the venues that use one, so removing it changes nothing the venue verifies.
- A tag inside the signed content (the url or the body) cannot be removed without breaking the
  signature. The request is refused instead of sent, as a definite rejection that never reached the
  venue. The cause is an order without its own client order id, which this package always sends.

Nothing here adds a tag. See `docs/opensource/venues.md`, Broker tags.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Header values that are ccxt's identity, compared without regard to case. "p4sve" is a channel
# code ccxt sends on one venue; the docs name the venue, since the core stays venue-neutral.
HEADER_TAG_VALUES = frozenset({"ccxt", "p4sve"})
# Partner headers travel together on the venue that uses them: removing one and keeping the others
# would send a partner signature with no partner.
PARTNER_HEADER_PREFIX = "kc-api-partner"
# ccxt's default client order id prefixes, one per venue family and market type.
EMBEDDED_TAGS = ("x-TKT5PX2F", "x-cvBPrNm9", "x-xcKtGhcu", "6b9ad766b55dBCDE", "AA03022abc")
# ccxt's own name as a whole word, as in a body field `"broker":"CCXT"` or a builder code.
CCXT_WORD = re.compile(r"\bCCXT\b")


def _tag_refused() -> type[Exception]:
    """A rejection class that ccxt callers already read as 'the venue said no'."""
    import ccxt

    class TagRefused(ccxt.BadRequest):  # type: ignore[misc]  # ccxt is untyped
        """The request carried a ccxt tag that could not be removed, so it was not sent."""

    return TagRefused


def without_ccxt_headers(headers: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """`headers` without the ones that carry ccxt's identity; `None` stays `None`."""
    if headers is None:
        return None
    partner = any(
        str(name).lower().startswith(PARTNER_HEADER_PREFIX)
        and str(value).lower() in HEADER_TAG_VALUES
        for name, value in headers.items()
    )
    kept: dict[str, Any] = {}
    for name, value in headers.items():
        if value is not None and str(value).lower() in HEADER_TAG_VALUES:
            continue
        if partner and str(name).lower().startswith(PARTNER_HEADER_PREFIX):
            continue
        kept[name] = value
    return kept


def embedded_tags(url: str, body: Any) -> list[str]:
    """The ccxt tags found in the signed content of a request, empty when there are none."""
    text = f"{url}\n{body if body is not None else ''}"
    found = [tag for tag in EMBEDDED_TAGS if tag in text]
    if CCXT_WORD.search(text):
        found.append("CCXT")
    return found


def untagged(client: Any) -> Any:
    """`client`, its requests cleaned of ccxt's tags on the way out. Returns the same client."""
    send = client.fetch
    refused = _tag_refused()
    # Some venues keep the tag in the client's default headers, which `fetch` merges in after the
    # request is built, so they are cleaned where they are kept.
    client.headers = without_ccxt_headers(getattr(client, "headers", None)) or {}

    def fetch(url: str, method: str = "GET", headers: Any = None, body: Any = None) -> Any:
        found = embedded_tags(url, body)
        if found:
            raise refused(
                f"ccxt added {', '.join(found)} to a request inside what the venue verifies, "
                "so it was not sent. Send the order with its own client order id."
            )
        return send(url, method, without_ccxt_headers(headers), body)

    client.fetch = fetch
    return client
