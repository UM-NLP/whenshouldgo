"""Routes API computeRoutes for ONE finalist, rendered by the server only.

DISPLAY-ONLY and never shown to a model. DRIVE by default; WALK exists behind a constructor flag
(off by default) and is always labelled beta. Not requested, so never held: polyline or any
geometry, legs, steps, traffic data (TRAFFIC_AWARE would change the SKU; see pricing.py). The
origin is ONLY the consented, rounded, foreground location passed in by the route handler; the
destination is the place ID (Google resolves it, so no destination coordinate is ever held here).
Result: whole minutes (rounded up) and distance rounded to 100 m: no precision promise (decision 15).
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..domain import RouteMode
from ..location import RoundedLocation
from .errors import (
    LocationRequired,
    MalformedPlacesResult,
    PlacesInvalidRequest,
    PlacesNotFound,
)
from .transport import (
    MASK_HEADER,
    ROUTES_FIELD_MASK,
    ROUTES_HOST,
    PlacesRequest,
    PlacesTransport,
    credential_from,
    exchange,
)

_PLACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,300}$")
_DURATION = re.compile(r"^(\d{1,9})(?:\.\d{1,9})?s$")  # protobuf Duration, e.g. "1234s"
DISTANCE_ROUNDING_METERS = 100
MAX_DURATION_SECONDS = 100_000 * 60
MAX_DISTANCE_METERS = 100_000_000


@dataclass(frozen=True)
class ParsedRoute:
    duration_minutes: int
    distance_meters: int


def parse_route(body: dict[str, Any]) -> ParsedRoute:
    routes = body.get("routes")
    if routes is None or routes == []:
        raise PlacesNotFound("no route found", calls=1)  # Google answers {} when none exists
    if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
        raise MalformedPlacesResult(calls=1)
    route = routes[0]
    duration = route.get("duration")
    distance = route.get("distanceMeters", 0)  # omitted by the API when 0
    match = _DURATION.fullmatch(duration) if isinstance(duration, str) else None
    if (match is None or isinstance(distance, bool) or not isinstance(distance, int)
            or not 0 <= distance <= MAX_DISTANCE_METERS):
        raise MalformedPlacesResult(calls=1)
    seconds = float(duration[:-1])
    if seconds > MAX_DURATION_SECONDS:
        raise MalformedPlacesResult(calls=1)
    minutes = max(1, math.ceil(seconds / 60))
    rounded = int(round(distance / DISTANCE_ROUNDING_METERS)) * DISTANCE_ROUNDING_METERS
    return ParsedRoute(duration_minutes=minutes, distance_meters=rounded)


class RouteCheckClient:
    def __init__(
        self, transport: PlacesTransport, secret_provider: Callable[[], str], *,
        walk_enabled: bool = False, timeout_seconds: float = 5.0,
    ) -> None:
        self._transport = transport
        self._secret_provider = secret_provider
        self.walk_enabled = walk_enabled
        self._timeout = timeout_seconds

    def check_request(self, place_id: str, mode: RouteMode, origin: RoundedLocation | None) -> None:
        """Validation that needs no network (the service runs it before it reserves anything)."""
        if origin is None:
            raise LocationRequired()  # a fixed prompt; an origin is never guessed
        if not isinstance(place_id, str) or not _PLACE_ID.fullmatch(place_id):
            raise PlacesInvalidRequest()
        if mode == RouteMode.WALK and not self.walk_enabled:
            raise PlacesInvalidRequest("walking route checks are not available")
        if mode not in (RouteMode.DRIVE, RouteMode.WALK):
            raise PlacesInvalidRequest()

    def compute(
        self, place_id: str, mode: RouteMode, origin: RoundedLocation | None,
        timeout_seconds: float | None = None,
    ) -> ParsedRoute:
        self.check_request(place_id, mode, origin)
        assert origin is not None
        credential = credential_from(self._secret_provider)
        timeout = self._timeout if timeout_seconds is None else min(self._timeout, timeout_seconds)
        request = PlacesRequest(
            method="POST", url=f"https://{ROUTES_HOST}/directions/v2:computeRoutes",
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     MASK_HEADER: ROUTES_FIELD_MASK},
            body={"origin": {"location": {"latLng": {
                      "latitude": origin.latitude, "longitude": origin.longitude}}},
                  "destination": {"placeId": place_id},
                  "travelMode": mode.value},
            timeout_seconds=timeout)
        return parse_route(exchange(self._transport, request, credential))
