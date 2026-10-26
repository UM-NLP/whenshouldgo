"""Google Maps handoff URL (decision 27): built at confirm time from the place ID only.

The origin is omitted so Maps uses the device's own position; the URL carries no coordinates and no
name. The host is pinned. The result is returned to the client and never stored or logged by the
caller (``ConfirmActionResponse.handoff_url`` is also excluded from ``repr``).
"""
from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

MAPS_SCHEME = "https"
MAPS_HOST = "www.google.com"
MAPS_PATH = "/maps/search/"
_PLACE_ID = re.compile(r"[A-Za-z0-9_-]{1,300}")


class HandoffError(ValueError):
    """The place ID is not usable. Static text: never the rejected value."""


def build_maps_url(place_id: str) -> str:
    """The Maps URLs ``search`` form: ``query`` is required by Google, ``query_place_id`` selects."""
    if not isinstance(place_id, str) or _PLACE_ID.fullmatch(place_id) is None:
        raise HandoffError("invalid place id")
    url = (f"{MAPS_SCHEME}://{MAPS_HOST}{MAPS_PATH}?api=1&query=Google"
           f"&query_place_id={quote(place_id, safe='')}")
    parts = urlsplit(url)
    if (parts.scheme, parts.netloc, parts.path) != (MAPS_SCHEME, MAPS_HOST, MAPS_PATH):
        raise HandoffError("invalid handoff url")  # defence in depth: the host stays pinned
    return url
