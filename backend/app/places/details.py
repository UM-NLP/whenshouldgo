"""Places API (New) Place Details by place ID, minimal field mask, rendered by the server only.

DISPLAY-ONLY. The result never reaches a model (decision 17 and 23: no Evidence, nothing in a
prompt), a store, a replay record, a log or telemetry. Coordinates are not requested and so are
never held. Photos and reviews are not requested. A result lives in memory for ``CACHE_TTL``
(5 minutes, decision 27) per tenant and is dropped on expiry.
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..discovery.text import short_field_problem
from ..domain import MAX_TITLE_CHARS
from .errors import MalformedPlacesResult, PlacesInvalidRequest
from .transport import (
    DETAILS_FIELD_MASK,
    MASK_HEADER,
    PLACES_HOST,
    PlacesRequest,
    PlacesTransport,
    credential_from,
    exchange,
)

MAX_PLACE_IDS = 3
CACHE_TTL = timedelta(minutes=5)
MAX_CACHE_ENTRIES = 256
MAX_ADDRESS_CHARS = 300
_PLACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,300}$")


def validate_place_ids(place_ids: object) -> tuple[str, ...]:
    """At most 3 distinct, well-formed ids (duplicates are dropped: they would be billed twice)."""
    if not isinstance(place_ids, (list, tuple)) or not 1 <= len(place_ids) <= MAX_PLACE_IDS * 4:
        raise PlacesInvalidRequest()
    seen: dict[str, None] = {}
    for item in place_ids:
        if not isinstance(item, str) or not _PLACE_ID.fullmatch(item):
            raise PlacesInvalidRequest()
        seen[item] = None
    if len(seen) > MAX_PLACE_IDS:
        raise PlacesInvalidRequest()
    return tuple(seen)


@dataclass(frozen=True, repr=False)
class ParsedPlace:
    """The display fields of one place. No coordinates, no URLs, no hours text."""

    place_id: str
    display_name: str
    formatted_address: str | None
    open_now: bool | None
    rating: float | None
    fetched_at: datetime

    def __repr__(self) -> str:
        return "ParsedPlace(<redacted>)"

    __str__ = __repr__


def _plain(text: object, limit: int) -> str | None:
    if (not isinstance(text, str) or not text.strip() or len(text) > limit
            or short_field_problem(text)):
        return None
    return text


def parse_place(body: dict[str, Any], expected_id: str, fetched_at: datetime) -> ParsedPlace:
    """Validate one Place Details body. Unknown fields are ignored and dropped (the mask asks for
    none of them); a wrong id, a missing or unusable name is a malformed result."""
    name_obj = body.get("displayName")
    name = _plain(name_obj.get("text") if isinstance(name_obj, dict) else None, MAX_TITLE_CHARS)
    if body.get("id") != expected_id or name is None:
        raise MalformedPlacesResult(calls=1)
    address = _plain(body.get("formattedAddress"), MAX_ADDRESS_CHARS)  # unusable: omitted
    hours = body.get("currentOpeningHours")
    open_now = hours.get("openNow") if isinstance(hours, dict) else None
    rating = body.get("rating")
    ok_rating = (isinstance(rating, (int, float)) and not isinstance(rating, bool)
                 and 0 <= rating <= 5)
    return ParsedPlace(
        place_id=expected_id, display_name=name, formatted_address=address,
        open_now=open_now if isinstance(open_now, bool) else None,
        rating=float(rating) if ok_rating else None, fetched_at=fetched_at)


class PlaceDetailsCache:
    """In-memory, per tenant, 5-minute TTL, bounded. Never persisted."""

    def __init__(self, now: Callable[[], datetime], *, ttl: timedelta = CACHE_TTL,
                 max_entries: int = MAX_CACHE_ENTRIES) -> None:
        self._now = now
        self._ttl = ttl
        self._max = max_entries
        self._items: OrderedDict[tuple[str, str], ParsedPlace] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, tenant: str, place_id: str) -> ParsedPlace | None:
        with self._lock:
            item = self._items.get((tenant, place_id))
            if item is None:
                return None
            if item.fetched_at + self._ttl <= self._now():  # stale: never served, dropped
                del self._items[(tenant, place_id)]
                return None
            return item

    def put(self, tenant: str, item: ParsedPlace) -> None:
        with self._lock:
            self._items[(tenant, item.place_id)] = item
            self._items.move_to_end((tenant, item.place_id))
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class PlaceDetailsClient:
    def __init__(
        self, transport: PlacesTransport, secret_provider: Callable[[], str], *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC), timeout_seconds: float = 5.0,
    ) -> None:
        self._transport = transport
        self._secret_provider = secret_provider
        self._now = now
        self._timeout = timeout_seconds

    def fetch(self, place_id: str, timeout_seconds: float | None = None) -> ParsedPlace:
        if not _PLACE_ID.fullmatch(place_id):
            raise PlacesInvalidRequest()
        credential = credential_from(self._secret_provider)
        timeout = self._timeout if timeout_seconds is None else min(self._timeout, timeout_seconds)
        request = PlacesRequest(
            method="GET", url=f"https://{PLACES_HOST}/v1/places/{place_id}",
            headers={"Accept": "application/json", MASK_HEADER: DETAILS_FIELD_MASK},
            timeout_seconds=timeout)
        body = exchange(self._transport, request, credential)
        return parse_place(body, place_id, self._now())

